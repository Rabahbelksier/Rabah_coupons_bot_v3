"""No live Telegram or Meta requests. PostgreSQL tests use an isolated local DB."""

import concurrent.futures
import importlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

for name in ("APP_KEY", "APP_SECRET", "TRACKING_ID", "TELEGRAM_TOKEN"):
    os.environ.setdefault(name, "test")

import psycopg2
import requests
from flask import Flask

from core import meta_store
from handlers.meta_redirect import meta_redirect
from services import meta_tracking as tracking


SETTINGS = tracking.Settings(
    enabled=True, pixel="123456789", source="campaign_test",
    token="fake-test-credential", test_code="", version="v25.0",
    ttl=86400, proxy_hops=0,
)


class MetaRedirectTests(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__)
        app.register_blueprint(meta_redirect)
        app.testing = True
        self.client = app.test_client()
        self.patches = [
            patch("handlers.meta_redirect.settings", return_value=SETTINGS),
            patch("handlers.meta_redirect.meta_store.create_visit"),
            patch("handlers.meta_redirect.meta_store.enrich_visit"),
            patch.object(tracking.config, "DATABASE_URL", "offline-test"),
        ]
        self.settings, self.create, self.enrich, _ = [p.start() for p in self.patches]
        self.addCleanup(lambda: [p.stop() for p in self.patches])

    def test_redirect_uses_exact_configured_source_and_only_two_browser_events(self):
        response = self.client.get(
            "/bot?fbclid=real_click", headers={"User-Agent": "test-browser"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.headers["X-Robots-Tag"], "noindex, nofollow")
        html = response.get_data(as_text=True)
        self.assertIn("}, 800)", html)
        self.assertIn("fbq('track', 'PageView')", html)
        self.assertIn("fbq('trackCustom', 'TelegramClick')", html)
        self.assertNotIn("TelegramBotStart", html)
        self.assertNotIn(SETTINGS.token, html)
        self.assertIn(
            'const target = "https://t.me/rabahcoupons1bot?start=campaign_test";', html,
        )
        self.assertNotIn("?start=mv_", html)
        self.assertNotIn("sendBeacon", html)
        self.create.assert_not_called()

    def test_disabled_and_privacy_optout_are_organic(self):
        for current, headers in [
            (replace(SETTINGS, enabled=False), {}),
            (SETTINGS, {"Sec-GPC": "1"}),
            (SETTINGS, {"DNT": "1"}),
        ]:
            self.settings.return_value = current
            response = self.client.get("/bot?fbclid=private", headers=headers)
            html = response.get_data(as_text=True)
            self.assertNotIn("fbq(", html)
            self.assertNotIn("?start=", html)
        self.create.assert_not_called()

    def test_storage_failure_preserves_campaign_redirect(self):
        self.create.side_effect = RuntimeError("sensitive storage details")
        html = self.client.get("/bot").get_data(as_text=True)
        self.assertIn("?start=campaign_test", html)
        self.assertNotIn("sensitive storage details", html)
        self.create.assert_not_called()

    def test_redirect_source_does_not_depend_on_capi_token_or_pixel(self):
        for current in [
            replace(SETTINGS, token=""),
            replace(SETTINGS, pixel=""),
            replace(SETTINGS, source="another_source"),
        ]:
            self.settings.return_value = current
            html = self.client.get("/bot").get_data(as_text=True)
            self.assertIn(
                f'const target = "{tracking.BOT_URL}?start={current.source}";', html,
            )
        self.create.assert_not_called()

    def test_cookie_enrichment_is_validated_and_no_arbitrary_data_is_saved(self):
        response = self.client.post("/bot/visit", json={
            "token": "mv_" + "a" * 32, "fbp": "fb.1.1700000000000.12345",
            "email": "not-collected@example.invalid", "fbc": "fb.1.1700000000000.old",
        })
        self.assertEqual(response.status_code, 204)
        self.enrich.assert_called_once_with(
            "mv_" + "a" * 32, {"fbp": "fb.1.1700000000000.12345"},
        )

    def test_invalid_large_cross_origin_and_opted_out_beacons(self):
        self.assertEqual(self.client.post(
            "/bot/visit", json={"token": "invalid"},
        ).status_code, 400)
        self.assertEqual(self.client.post(
            "/bot/visit", data="x" * 2049, content_type="application/json",
        ).status_code, 413)
        self.assertEqual(self.client.post(
            "/bot/visit", json={"token": "mv_" + "a" * 32, "fbp": "bad"},
            headers={"Sec-Fetch-Site": "cross-site"},
        ).status_code, 204)
        self.client.post(
            "/bot/visit", json={"token": "mv_" + "a" * 32},
            headers={"Sec-GPC": "1"},
        )
        self.client.post(
            "/bot/visit", json={
                "token": "mv_" + "a" * 32, "fbp": "fb.1.1700000000000.12345",
            }, headers={"Origin": "https://untrusted.invalid"},
        )
        self.enrich.assert_not_called()

    def test_ip_headers_require_explicit_proxy_trust(self):
        app = self.client.application
        with app.test_request_context(
            "/bot", headers={"X-Forwarded-For": "198.51.100.7, 10.0.0.1"},
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
        ):
            from flask import request
            direct = tracking.request_user_data(request, SETTINGS)
            proxy = tracking.request_user_data(request, replace(SETTINGS, proxy_hops=2))
        self.assertEqual(direct["client_ip_address"], "127.0.0.1")
        self.assertEqual(proxy["client_ip_address"], "198.51.100.7")


class MetaSettingsAndDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.patch = patch.object(tracking, "settings", return_value=SETTINGS)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    @patch.object(tracking.config, "DATABASE_URL", "fake-test-db")
    def test_settings_payload_validation_and_no_secret_repr(self):
        self.assertTrue(SETTINGS.ready)
        self.assertNotIn(SETTINGS.token, repr(SETTINGS))
        self.assertTrue(replace(SETTINGS, source="mv_campaign").ready)
        self.assertFalse(replace(SETTINGS, source="x" * 65).ready)
        self.assertFalse(replace(SETTINGS, source="bad&source").ready)
        self.assertFalse(replace(SETTINGS, token="").ready)
        for args, expected in [
            (["campaign_test"], "campaign_test"), (["unknown"], "unknown"),
            ([], ""), (["a", "b"], ""), (["bad?arg"], ""),
        ]:
            attribution = tracking.start_attribution(args, 1700000000)
            if expected:
                self.assertEqual(attribution["start"], expected)
            else:
                self.assertIsNone(attribution)
        self.assertEqual(
            tracking.start_attribution(["campaign_test"], 1700000000)["event_time"],
            1700000000,
        )

    def test_source_capture_does_not_require_meta_readiness(self):
        with patch.object(tracking, "settings", return_value=replace(SETTINGS, token="")):
            attribution = tracking.start_attribution(["other_source"])
        self.assertEqual(attribution["start"], "other_source")
        self.assertFalse(attribution["eligible"])

    @patch.object(tracking.config, "DATABASE_URL", "fake-test-db")
    @patch.object(meta_store, "finish_event")
    @patch.object(meta_store, "claim_event")
    @patch.object(tracking.requests, "post")
    def test_delivery_statuses_and_stable_retry_body(self, post, claim, finish):
        body = {"data": [{"event_id": "fixed-event", "event_time": 1700000000}]}
        claim.return_value = ("fixed-event", "987654321", body, 1)
        cases = [
            (200, {"events_received": 1}, "sent"),
            (429, {"error": {"code": 4}}, "pending"),
            (500, {}, "pending"),
            (400, {"error": {"code": 2, "is_transient": True}}, "pending"),
            (400, {"error": {"code": 100, "message": SETTINGS.token}}, "failed"),
            (401, {"error": {"code": 190}}, "failed"),
            (200, {}, "pending"),
        ]
        worker = tracking.MetaDeliveryWorker()
        for status_code, result, status in cases:
            with self.subTest(status=status_code, result=result):
                post.return_value = SimpleNamespace(
                    status_code=status_code, json=lambda: result,
                )
                self.assertTrue(worker.deliver_one())
                self.assertEqual(finish.call_args.args[2], status)
                self.assertNotIn(SETTINGS.token, str(finish.call_args))
                self.assertEqual(post.call_args.kwargs["json"], body)
                self.assertIn("/987654321/events", post.call_args.args[0])
                self.assertNotIn(SETTINGS.token, post.call_args.args[0])
                self.assertFalse(post.call_args.kwargs["allow_redirects"])
        post.side_effect = requests.Timeout("must not log " + SETTINGS.token)
        worker.deliver_one()
        self.assertEqual(finish.call_args.args[2:4], ("pending", "transport"))
        claim.return_value = ("fixed-event", "987654321", body, 20)
        worker.deliver_one()
        self.assertEqual(finish.call_args.args[2], "failed")
        claim.return_value = None
        self.assertFalse(worker.deliver_one())


class MetaStartHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_registration_failure_does_not_change_bot_reply(self):
        from handlers.start import start
        from handlers.help import START_TEXT
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=42, first_name="Test", last_name=None),
            message=SimpleNamespace(reply_text=AsyncMock()),
        )
        context = SimpleNamespace(user_data={}, args=["campaign_test"])
        with patch("handlers.start.start_attribution", return_value={"test": 1}), patch(
            "handlers.start.save_user", return_value=False,
        ) as save:
            await start(update, context)
        save.assert_called_once_with(42, "Test", "", meta_start={"test": 1})
        self.assertEqual(update.message.reply_text.await_args.args[0], START_TEXT)


class MetaMainWiringTests(unittest.TestCase):
    def test_existing_flask_app_registers_redirect_without_live_startup_calls(self):
        # Import the real entry point without initializing the external DB,
        # changing Telegram webhooks, or starting another Telegram worker.
        with (
            patch("core.db.init_db"),
            patch("utils.error_logging.install_error_log_handler"),
            patch("services.telegram_runtime.TelegramRuntime.ensure_ready"),
            patch.object(tracking.config, "PUBLIC_URL", ""),
            patch.object(tracking.config, "META_TRACKING_ENABLED", False),
            patch("requests.post") as post,
        ):
            already_loaded = "main" in sys.modules
            main = importlib.import_module("main")
            client = main.app.test_client()
            self.assertEqual(client.get("/").status_code, 200)
            response = client.get("/bot")
            self.assertEqual(response.status_code, 200)
            self.assertIn(tracking.BOT_URL, response.get_data(as_text=True))
            self.assertNotIn("fbq(", response.get_data(as_text=True))
            post.assert_not_called()
            if not already_loaded:
                sys.modules.pop("main", None)


@unittest.skipUnless(
    shutil.which("initdb") and shutil.which("pg_ctl"),
    "Local PostgreSQL binaries required; no external database is used",
)
class MetaPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="meta-test-")
        cls.data = cls.tmp.name + "/data"
        subprocess.run(
            ["initdb", "-D", cls.data, "-A", "trust", "-U", "meta_test", "--no-locale"],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        subprocess.run(
            ["pg_ctl", "-D", cls.data, "-l", cls.tmp.name + "/pg.log", "-o",
             f"-k {cls.tmp.name} -p 6543 -c listen_addresses=''", "-w", "start"],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        cls.factory = staticmethod(lambda: psycopg2.connect(
            dbname="postgres", user="meta_test", host=cls.tmp.name, port=6543,
        ))
        cls.connection_patch = patch.object(meta_store, "_connect", cls.factory)
        cls.connection_patch.start()
        with cls.factory() as conn, conn.cursor() as cursor:
            cursor.execute("""
                CREATE TABLE user_bot (
                    chat_id BIGINT PRIMARY KEY, first_name TEXT, last_name TEXT,
                    requests_today INT DEFAULT 0, requests_this_month INT DEFAULT 0,
                    requests_last_month INT DEFAULT 0
                )
            """)
            cursor.execute(meta_store.SCHEMA)
            cursor.execute(meta_store.SCHEMA)

    @classmethod
    def tearDownClass(cls):
        cls.connection_patch.stop()
        subprocess.run(
            ["pg_ctl", "-D", cls.data, "-m", "immediate", "-w", "stop"],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        cls.tmp.cleanup()

    def setUp(self):
        with meta_store.transaction() as cursor:
            cursor.execute("TRUNCATE user_bot, meta_bot_visits, meta_bot_events")
        self.attribution = {
            "source": SETTINGS.source, "pixel": SETTINGS.pixel,
            "start": SETTINGS.source, "test_code": "", "event_time": int(time.time()),
        }

    def rows(self, sql, params=()):
        with meta_store.transaction() as cursor:
            cursor.execute(sql, params)
            return cursor.fetchall()

    def register(self, user=1, **kwargs):
        return meta_store.register_start(user, "Test", "", {**self.attribution, **kwargs})

    def test_direct_start_dedup_and_existing_user_with_empty_source(self):
        self.assertTrue(self.register())
        self.assertFalse(self.register())
        self.assertEqual(self.rows("SELECT meta_source_id FROM user_bot"), [(SETTINGS.source,)])
        event = self.rows("SELECT body FROM meta_bot_events")[0][0]["data"][0]
        self.assertEqual(event["event_name"], "TelegramBotStart")
        self.assertEqual(event["action_source"], "chat")
        self.assertEqual(event["event_time"], self.attribution["event_time"])
        self.assertEqual(list(event["user_data"]), ["external_id"])
        self.assertRegex(event["user_data"]["external_id"][0], r"^[0-9a-f]{64}$")
        self.rows("INSERT INTO user_bot (chat_id) VALUES (2) RETURNING chat_id")
        self.assertTrue(self.register(2))
        self.assertFalse(self.register(2))
        self.assertEqual(self.rows("SELECT COUNT(*) FROM meta_bot_events"), [(2,)])
        self.assertEqual(self.rows(
            "SELECT meta_source_id FROM user_bot WHERE chat_id = 2",
        ), [(SETTINGS.source,)])

    def test_any_source_is_saved_but_only_matching_source_queues_event(self):
        for user, payload in [(1, ""), (2, "other_source"), (3, "mv_" + "z" * 32)]:
            self.assertFalse(self.register(user, start=payload))
            self.assertEqual(self.rows(
                "SELECT meta_source_id FROM user_bot WHERE chat_id = %s", (user,),
            ), [(payload or None,)])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM meta_bot_events"), [(0,)])
        for user in (1, 2, 3):
            self.assertTrue(self.register(user))
            self.assertFalse(self.register(user))
        self.assertEqual(self.rows("SELECT COUNT(*) FROM meta_bot_events"), [(3,)])

    def test_existing_matching_source_suppresses_event_without_outbox_receipt(self):
        self.rows(
            "INSERT INTO user_bot (chat_id, meta_source_id) VALUES (1, %s) RETURNING chat_id",
            (SETTINGS.source,),
        )
        self.assertFalse(self.register())
        self.assertEqual(self.rows("SELECT COUNT(*) FROM meta_bot_events"), [(0,)])

    def test_source_changes_are_saved_but_do_not_duplicate_prior_event(self):
        self.assertTrue(self.register())
        self.assertFalse(self.register(start="another_source"))
        self.assertEqual(self.rows("SELECT meta_source_id FROM user_bot"), [("another_source",)])
        self.assertFalse(self.register())
        self.assertEqual(self.rows("SELECT meta_source_id FROM user_bot"), [(SETTINGS.source,)])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM meta_bot_events"), [(1,)])

    def test_source_is_saved_when_meta_delivery_is_disabled(self):
        self.assertFalse(self.register(eligible=False))
        self.assertEqual(self.rows("SELECT meta_source_id FROM user_bot"), [(SETTINGS.source,)])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM meta_bot_events"), [(0,)])
        self.assertFalse(self.register())

    def test_no_browser_data_is_guessed_for_shared_source_and_test_code_is_preserved(self):
        meta_store.create_visit(
            "mv_" + "a" * 32, SETTINGS.source, SETTINGS.pixel,
            {"client_ip_address": "198.51.100.1", "fbc": "fb.1.1700000000000.real"}, 60,
        )
        self.assertTrue(self.register(test_code="TEST_FAKE"))
        body = self.rows("SELECT body FROM meta_bot_events")[0][0]
        self.assertEqual(body["test_event_code"], "TEST_FAKE")
        self.assertEqual(list(body["data"][0]["user_data"]), ["external_id"])
        self.assertEqual(self.rows(
            "SELECT meta_source_id FROM user_bot WHERE chat_id = 1",
        ), [(SETTINGS.source,)])

    def test_legacy_visit_tokens_are_nonmatching_sources_and_expired_records_are_cleaned(self):
        for user, source, pixel, ttl in [
            (1, SETTINGS.source, SETTINGS.pixel, -1),
            (2, "previous_campaign", SETTINGS.pixel, 60),
            (3, SETTINGS.source, "987654321", 60),
        ]:
            token = "mv_" + str(user) * 32
            meta_store.create_visit(token, source, pixel, {}, ttl)
            self.assertFalse(self.register(user, start=token))
        self.assertEqual(self.rows("SELECT COUNT(*) FROM meta_bot_events"), [(0,)])
        meta_store.cleanup()
        self.assertEqual(self.rows("SELECT COUNT(*) FROM meta_bot_visits"), [(2,)])

    def test_concurrent_starts_register_and_queue_once(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
            results = list(executor.map(lambda _: self.register(), range(6)))
        self.assertEqual(results.count(True), 1)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM meta_bot_events"), [(1,)])

    def test_shared_campaign_link_works_for_multiple_accounts(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(
                lambda user: self.register(user), range(1, 5),
            ))
        self.assertEqual(results.count(True), 4)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM user_bot"), [(4,)])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM meta_bot_events"), [(4,)])

    def test_concurrent_existing_user_starts_queue_once(self):
        self.rows("INSERT INTO user_bot (chat_id) VALUES (1) RETURNING chat_id")
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
            results = list(executor.map(lambda _: self.register(), range(6)))
        self.assertEqual(results.count(True), 1)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM meta_bot_events"), [(1,)])

    def test_transaction_rolls_back_registration_when_outbox_fails(self):
        with meta_store.transaction() as cursor:
            cursor.execute("""
                ALTER TABLE meta_bot_events ADD CONSTRAINT forced_failure
                CHECK (pixel_id != '123456789')
            """)
        try:
            with self.assertRaises(psycopg2.errors.CheckViolation):
                self.register()
            self.assertEqual(self.rows("SELECT COUNT(*) FROM user_bot"), [(0,)])
        finally:
            with meta_store.transaction() as cursor:
                cursor.execute("ALTER TABLE meta_bot_events DROP CONSTRAINT forced_failure")
        self.assertTrue(self.register())

    def test_lease_recovery_retry_and_terminal_payload_purge(self):
        self.register()
        first = meta_store.claim_event("worker1")
        self.assertIsNotNone(first)
        self.assertIsNone(meta_store.claim_event("worker2"))
        self.rows("""
            UPDATE meta_bot_events SET lease_until = CURRENT_TIMESTAMP - INTERVAL '1 minute'
            RETURNING event_id
        """)
        recovered = meta_store.claim_event("worker2")
        self.assertEqual(first[:3], recovered[:3])
        self.assertEqual(recovered[3], 2)
        meta_store.finish_event(first[0], "worker1", "sent")
        self.assertEqual(self.rows("SELECT status FROM meta_bot_events"), [("sending",)])
        meta_store.finish_event(first[0], "worker2", "pending", "transport", 60)
        self.assertIsNone(meta_store.claim_event("worker3"))
        self.rows("""
            UPDATE meta_bot_events SET next_attempt_at = CURRENT_TIMESTAMP
            RETURNING event_id
        """)
        self.assertIsNotNone(meta_store.claim_event("worker3"))
        meta_store.finish_event(first[0], "worker3", "sent")
        self.assertEqual(self.rows("SELECT status, body FROM meta_bot_events"), [("sent", None)])

    def test_cleanup_purges_expired_event_customer_data_but_keeps_dedup_receipt(self):
        self.register()
        self.rows("""
            UPDATE meta_bot_events SET created_at = CURRENT_TIMESTAMP - INTERVAL '7 days'
            RETURNING event_id
        """)
        self.assertIsNone(meta_store.claim_event("worker"))
        meta_store.cleanup()
        self.assertEqual(self.rows("SELECT status, body FROM meta_bot_events"), [("failed", None)])
        self.rows("DELETE FROM user_bot RETURNING chat_id")
        self.assertFalse(self.register())
        self.assertEqual(self.rows("SELECT COUNT(*) FROM meta_bot_events"), [(1,)])


if __name__ == "__main__":
    unittest.main()

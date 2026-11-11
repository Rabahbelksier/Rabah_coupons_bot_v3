"""No live Telegram or Meta requests. PostgreSQL tests use an isolated local DB."""

import concurrent.futures
import asyncio
import importlib
import os
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from html.parser import HTMLParser
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

for name in ("APP_KEY", "APP_SECRET", "TRACKING_ID", "TELEGRAM_TOKEN"):
    os.environ.setdefault(name, "test")

import psycopg2
import requests
from flask import Flask

from core import meta_store
from handlers.meta_redirect import (
    TELEGRAM_CAMPAIGN_URL_TEMPLATE,
    meta_redirect,
)
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

    def test_page_stores_campaign_and_provides_visit_button_without_auto_navigation(self):
        response = self.client.get(
            "/bot?fbclid=real_click", headers={"User-Agent": "test-browser"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.headers["X-Robots-Tag"], "noindex, nofollow")
        html = response.get_data(as_text=True)
        self.assertEqual(
            TELEGRAM_CAMPAIGN_URL_TEMPLATE,
            "https://t.me/rabahcoupons1bot?start={META_ATTRIBUTION_ID}",
        )
        self.assertIn('id="telegram-subscribe"', html)
        self.assertNotIn("window.location.replace", html)
        self.assertNotIn("http-equiv=\"refresh\"", html)
        self.assertIn("fbq('track', 'PageView')", html)
        self.assertIn("fbq('trackCustom', 'TelegramClick')", html)
        self.assertNotIn("TelegramBotStart", html)
        self.assertNotIn(SETTINGS.token, html)
        token = self.create.call_args.args[0]
        self.assertRegex(token, r"^mv_[A-Za-z0-9_-]{32}$")
        self.assertIn(f'?start={token}', html)
        self.assertEqual(self.create.call_args.args[1:3],
                         (SETTINGS.source, SETTINGS.pixel))
        self.assertEqual(self.create.call_args.args[3]["client_user_agent"], "test-browser")
        self.assertIn("fbc", self.create.call_args.args[3])
        self.assertEqual(self.create.call_args.args[4], SETTINGS.ttl)
        self.assertNotIn("?start=campaign_test", html)
        self.assertNotIn("const target", html)
        self.assertIn("fetch('/bot/visit'", html)
        self.assertIn("keepalive: true", html)
        self.client.get("/bot")
        self.assertNotEqual(token, self.create.call_args.args[0])

    def test_disabled_and_privacy_optout_preserve_button_source_without_collection(self):
        for current, headers in [
            (replace(SETTINGS, enabled=False), {}),
            (SETTINGS, {"Sec-GPC": "1"}),
            (SETTINGS, {"DNT": "1"}),
        ]:
            self.settings.return_value = current
            response = self.client.get("/bot?fbclid=private", headers=headers)
            html = response.get_data(as_text=True)
            self.assertNotIn("fbq(", html)
            self.assertIn("?start=campaign_test", html)
            self.assertIn('id="telegram-subscribe"', html)
        self.create.assert_not_called()

    def test_reference_design_social_links_do_not_replace_tracked_bot_button(self):
        class Links(HTMLParser):
            def __init__(self):
                super().__init__()
                self.anchors = []

            def handle_starttag(self, tag, attrs):
                if tag == "a":
                    self.anchors.append(dict(attrs))

        html = self.client.get("/bot").get_data(as_text=True)
        parser = Links()
        parser.feed(html)
        primary = [
            link for link in parser.anchors
            if link.get("id") == "telegram-subscribe"
        ]
        self.assertEqual(len(primary), 1)
        token = self.create.call_args.args[0]
        self.assertEqual(
            primary[0]["href"],
            f"https://t.me/rabahcoupons1bot?start={token}",
        )
        self.assertNotIn("target", primary[0])
        for href in (
            "https://www.facebook.com/share/1JfKBBcjCb/",
            "https://www.facebook.com/share/1FfezX5Hp3/",
            "https://t.me/rabahcopons",
        ):
            matches = [link for link in parser.anchors if link.get("href") == href]
            self.assertEqual(len(matches), 1)
            self.assertNotIn("onclick", matches[0])
            self.assertNotEqual(matches[0].get("id"), "telegram-subscribe")
        self.assertEqual(html.count("fbq('track', 'PageView')"), 1)
        self.assertEqual(html.count("fbq('trackCustom', 'TelegramClick')"), 1)
        for event_name in (
            "Offers365FacebookClick",
            "RabahCouponsFacebookClick",
            "TelegramChannelClick",
        ):
            self.assertEqual(html.count(f"'{event_name}'"), 1)

    def test_denied_cookie_preserves_source_without_collecting_visit(self):
        self.client.set_cookie("meta_consent", "denied")
        html = self.client.get("/bot").get_data(as_text=True)
        self.assertIn("?start=campaign_test", html)
        self.assertNotIn("fbq(", html)
        self.create.assert_not_called()

    def test_invalid_campaign_never_injects_into_button(self):
        self.settings.return_value = replace(SETTINGS, source='bad"><script>')
        html = self.client.get("/bot").get_data(as_text=True)
        self.assertIn(f'href="{tracking.BOT_URL}"', html)
        self.assertNotIn('bad"><script>', html)
        self.create.assert_not_called()

    def test_storage_failure_preserves_campaign_redirect(self):
        self.create.side_effect = RuntimeError("sensitive storage details")
        html = self.client.get("/bot").get_data(as_text=True)
        self.assertIn("?start=campaign_test", html)
        self.assertNotIn("sensitive storage details", html)
        self.assertNotIn("fetch('/bot/visit'", html)
        self.create.assert_called_once()

    def test_redirect_source_does_not_depend_on_capi_token_or_pixel(self):
        for current in [
            replace(SETTINGS, token=""),
            replace(SETTINGS, pixel=""),
            replace(SETTINGS, source="another_source"),
        ]:
            self.settings.return_value = current
            html = self.client.get("/bot").get_data(as_text=True)
            self.assertIn("?start=mv_", html)
            self.assertEqual(self.create.call_args.args[1], current.source)
        self.assertEqual(self.create.call_count, 3)

    @unittest.skipUnless(shutil.which("node"), "Node required for redirect-script checks")
    def test_events_are_click_scoped_and_native_link_survives_tracking_failures(self):
        html = self.client.get("/bot").get_data(as_text=True)
        scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
        # Exercise both real scripts without any Telegram or Meta requests.
        token = self.create.call_args.args[0]
        harness = r"""
const vm = require('node:vm');
const assert = require('node:assert/strict');
const scripts = JSON.parse(process.argv[1]);
const token = JSON.parse(process.argv[2]);
(async function() {
  for (const mode of ['ok', 'reject', 'throw', 'hang', 'missing', 'pixel-throws', 'pixel-blocked']) {
    let timers = [], redirects = [], captured, calls = [];
    const listeners = {};
    const makeAnchor = (id, href) => ({
      id, href,
      addEventListener: (name, fn) => {
        assert.equal(name, 'click');
        listeners[id] = fn;
      }
    });
    const anchors = {
      'telegram-subscribe': makeAnchor(
        'telegram-subscribe',
        'https://t.me/rabahcoupons1bot?start=' + token
      ),
      'offers365-facebook-link': makeAnchor(
        'offers365-facebook-link', 'https://www.facebook.com/share/1JfKBBcjCb/'
      ),
      'rabah-facebook-link': makeAnchor(
        'rabah-facebook-link', 'https://www.facebook.com/share/1FfezX5Hp3/'
      ),
      'rabah-channel-link': makeAnchor(
        'rabah-channel-link', 'https://t.me/rabahcopons'
      )
    };
    const ctx = {
      setTimeout: fn => timers.push(fn),
      location: {replace: url => redirects.push(url)},
      document: {
        cookie: '_fbp=fb.1.1700000000000.123; _fbc=fb.1.1700000000000.click',
        getElementById: id => anchors[id] || null,
        createElement: () => ({}),
        getElementsByTagName: () => [{parentNode: {insertBefore: () => {}}}]
      }
    };
    ctx.window = ctx;
    if (mode !== 'pixel-blocked') ctx.fbq = (...args) => {
      if (mode === 'pixel-throws') throw new Error('tracking blocked');
      calls.push(args);
    };
    if (mode !== 'missing') ctx.fetch = (url, options) => {
      assert.equal(url, '/bot/visit');
      captured = JSON.parse(options.body);
      if (mode === 'throw') throw new Error('offline');
      if (mode === 'reject') return Promise.reject(new Error('offline'));
      if (mode === 'hang') return new Promise(() => {});
      return Promise.resolve({});
    };
    vm.createContext(ctx);
    for (const source of scripts) {
      try { vm.runInContext(source, ctx); }
      catch (error) { if (mode !== 'pixel-throws') throw error; }
    }
    assert.equal(typeof listeners['telegram-subscribe'], 'function');
    for (const id of [
      'offers365-facebook-link', 'rabah-facebook-link', 'rabah-channel-link'
    ]) assert.equal(typeof listeners[id], 'function');
    const events = () => mode === 'pixel-blocked'
      ? Array.from(ctx.fbq.queue, args => Array.from(args)) : calls;
    assert.equal(events().filter(args => args[1] === 'TelegramClick').length, 0);
    assert.equal(events().filter(args => args[1] === 'PageView').length,
                 mode === 'pixel-throws' ? 0 : 1);
    timers.forEach(fn => fn());
    timers = [];
    await Promise.resolve();
    assert.deepEqual(redirects, []);
    listeners['telegram-subscribe']({preventDefault: () => {
      throw new Error('Native navigation intercepted');
    }});
    await Promise.resolve();
    assert.deepEqual(redirects, []);
    assert.equal(
      anchors['telegram-subscribe'].href,
      'https://t.me/rabahcoupons1bot?start=' + token
    );
    assert.equal(events().filter(args => args[1] === 'TelegramClick').length,
                 mode === 'pixel-throws' ? 0 : 1);
    assert.equal(events().filter(args => args[1] === 'TelegramBotStart').length, 0);
    for (const [id, eventName] of [
      ['offers365-facebook-link', 'Offers365FacebookClick'],
      ['rabah-facebook-link', 'RabahCouponsFacebookClick'],
      ['rabah-channel-link', 'TelegramChannelClick']
    ]) {
      const countBefore = events().length;
      listeners[id]({preventDefault: () => {
        throw new Error('Social navigation intercepted');
      }});
      assert.equal(events().length, countBefore + (mode === 'pixel-throws' ? 0 : 1));
      assert.equal(events().filter(args => args[1] === eventName).length,
                   mode === 'pixel-throws' ? 0 : 1);
    }
    if (captured) {
      assert.equal(captured.token, token);
      assert.equal(captured.fbp, 'fb.1.1700000000000.123');
      assert.equal(captured.fbc, 'fb.1.1700000000000.click');
    }
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        subprocess.run(
            ["node", "-e", harness, json.dumps(scripts), json.dumps(token)],
            check=True, capture_output=True, text=True,
        )

    def test_cookie_enrichment_is_validated_and_no_arbitrary_data_is_saved(self):
        response = self.client.post("/bot/visit", json={
            "token": "mv_" + "a" * 32, "fbp": "fb.1.1700000000000.12345",
            "email": "not-collected@example.invalid", "fbc": "fb.1.1700000000000.old",
        })
        self.assertEqual(response.status_code, 204)
        self.enrich.assert_called_once_with(
            "mv_" + "a" * 32, {
                "fbp": "fb.1.1700000000000.12345",
                "fbc": "fb.1.1700000000000.old",
            },
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
        attribution = {**self.attribution, **kwargs}
        if tracking.VISIT_PATTERN.fullmatch(attribution["start"]):
            attribution["visit_token"] = attribution["start"]
        return meta_store.register_start(user, "Test", "", attribution)

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
        for user, payload in [(1, ""), (2, "other_source"), (3, "mv_campaign")]:
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

    def test_expired_and_changed_campaign_or_pixel_visits_do_not_queue_events(self):
        for user, source, pixel, ttl in [
            (1, SETTINGS.source, SETTINGS.pixel, -1),
            (2, "previous_campaign", SETTINGS.pixel, 60),
            (3, SETTINGS.source, "987654321", 60),
        ]:
            token = "mv_" + str(user) * 32
            meta_store.create_visit(token, source, pixel, {}, ttl)
            self.assertFalse(self.register(user, start=token))
        self.assertEqual(self.rows("SELECT COUNT(*) FROM meta_bot_events"), [(0,)])
        self.assertEqual(self.rows(
            "SELECT chat_id, meta_source_id FROM user_bot ORDER BY chat_id",
        ), [(1, None), (2, "previous_campaign"), (3, SETTINGS.source)])
        meta_store.cleanup()
        self.assertEqual(self.rows("SELECT COUNT(*) FROM meta_bot_visits"), [(2,)])

    def test_concurrent_starts_register_and_queue_once(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
            results = list(executor.map(lambda _: self.register(), range(6)))
        self.assertEqual(results.count(True), 1)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM meta_bot_events"), [(1,)])

    def test_visit_flow_from_page_through_start_and_capi_delivery(self):
        from handlers.start import start
        app = Flask(__name__)
        app.register_blueprint(meta_redirect)
        client = app.test_client()
        client.set_cookie("_fbp", "fb.1.1700000000000.initial")
        with (
            patch("handlers.meta_redirect.settings", return_value=SETTINGS),
            patch.object(tracking, "settings", return_value=SETTINGS),
            patch.object(tracking.config, "DATABASE_URL", "isolated-test"),
        ):
            page = client.get(
                "/bot?fbclid=real_click",
                headers={"User-Agent": "visit-test-browser"},
                environ_base={"REMOTE_ADDR": "198.51.100.9"},
            )
            token = re.search(
                r"\?start=(mv_[A-Za-z0-9_-]{32})", page.get_data(as_text=True),
            )[1]
            visit = self.rows("SELECT token_hash, source_id FROM meta_bot_visits")
            self.assertEqual(visit, [(meta_store.digest(token), SETTINGS.source)])
            self.assertNotEqual(visit[0][0], token)
            response = client.post("/bot/visit", json={
                "token": token, "fbp": "fb.1.1700000000000.updated",
                "fbc": "fb.1.1700000000000.stale", "email": "ignored@example.invalid",
            })
            self.assertEqual(response.status_code, 204)
            message = SimpleNamespace(
                date=None, reply_text=AsyncMock(),
            )
            update = SimpleNamespace(
                effective_user=SimpleNamespace(id=42, first_name="Test", last_name=""),
                message=message,
            )
            context = SimpleNamespace(args=[token], user_data={})
            asyncio.run(start(update, context))
            asyncio.run(start(update, context))
            message.reply_text.assert_awaited()
            self.assertEqual(self.rows("SELECT meta_source_id FROM user_bot"),
                             [(SETTINGS.source,)])
            self.assertEqual(self.rows("SELECT COUNT(*) FROM meta_bot_events"), [(1,)])
            event = self.rows("SELECT body FROM meta_bot_events")[0][0]["data"][0]
            data = event["user_data"]
            self.assertEqual(data["fbp"], "fb.1.1700000000000.updated")
            self.assertTrue(data["fbc"].endswith(".real_click"))
            self.assertEqual(data["client_ip_address"], "198.51.100.9")
            self.assertEqual(data["client_user_agent"], "visit-test-browser")
            self.assertNotIn("email", data)
            profile = self.rows("""
                SELECT meta_source_id, meta_visit_id, meta_pixel_id, meta_fbp,
                    meta_fbc, meta_client_ip_address, meta_client_user_agent,
                    meta_visit_at IS NOT NULL
                FROM user_bot WHERE chat_id = 42
            """)
            self.assertEqual(profile, [(
                SETTINGS.source, meta_store.digest(token), SETTINGS.pixel,
                data["fbp"], data["fbc"], data["client_ip_address"],
                data["client_user_agent"], True,
            )])
            self.assertEqual(self.rows("SELECT consumed_by FROM meta_bot_visits"),
                             [(meta_store.digest("telegram:rabahcoupons1bot:42"),)])
            with patch.object(tracking.requests, "post") as post:
                post.return_value = SimpleNamespace(
                    status_code=200, json=lambda: {"events_received": 1},
                )
                self.assertTrue(tracking.MetaDeliveryWorker().deliver_one())
                self.assertEqual(post.call_args.kwargs["json"]["data"][0], event)
            self.assertEqual(self.rows("SELECT status, body FROM meta_bot_events"),
                             [("sent", None)])

    def test_visit_belongs_to_one_account_and_repeated_start_is_idempotent(self):
        token = "mv_" + "a" * 32
        meta_store.create_visit(token, SETTINGS.source, SETTINGS.pixel, {}, 60)
        self.assertTrue(self.register(start=token))
        self.assertFalse(self.register(start=token))
        self.assertFalse(self.register(2, start=token))
        self.assertEqual(self.rows(
            "SELECT chat_id, meta_source_id FROM user_bot ORDER BY chat_id",
        ), [(1, SETTINGS.source), (2, None)])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM meta_bot_events"), [(1,)])
        meta_store.enrich_visit(token, {"fbp": "fb.1.1700000000000.late"})
        self.assertEqual(self.rows("SELECT user_data FROM meta_bot_visits"), [({},)])

    def test_competing_accounts_cannot_redeem_one_visit_twice(self):
        token = "mv_" + "b" * 32
        meta_store.create_visit(token, SETTINGS.source, SETTINGS.pixel, {}, 60)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(
                lambda user: self.register(user, start=token), range(1, 5),
            ))
        self.assertEqual(results.count(True), 1)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM user_bot"), [(4,)])
        self.assertEqual(self.rows(
            "SELECT COUNT(*) FROM user_bot WHERE meta_source_id IS NOT NULL",
        ), [(1,)])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM meta_bot_events"), [(1,)])

    def test_unknown_visit_does_not_overwrite_existing_source(self):
        self.register(start="another_source")
        self.assertFalse(self.register(start="mv_" + "x" * 32))
        self.assertEqual(self.rows("SELECT meta_source_id FROM user_bot"),
                         [("another_source",)])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM meta_bot_events"), [(0,)])

    def test_nonmatching_visit_keeps_its_original_source_and_browser_columns(self):
        token = "mv_" + "d" * 32
        meta_store.create_visit(
            token, "other_campaign", SETTINGS.pixel,
            {"fbp": "fb.1.1700000000000.browser"}, 60,
        )
        self.assertFalse(self.register(start=token))
        self.assertEqual(self.rows(
            "SELECT meta_source_id, meta_visit_id, meta_fbp FROM user_bot",
        ), [("other_campaign", meta_store.digest(token), "fb.1.1700000000000.browser")])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM meta_bot_events"), [(0,)])

    def test_existing_matching_user_receives_visit_columns_without_new_event(self):
        self.rows(
            "INSERT INTO user_bot (chat_id, meta_source_id) VALUES (1, %s) RETURNING chat_id",
            (SETTINGS.source,),
        )
        token = "mv_" + "g" * 32
        meta_store.create_visit(
            token, SETTINGS.source, SETTINGS.pixel,
            {"client_user_agent": "known-browser"}, 60,
        )
        self.assertFalse(self.register(start=token))
        self.assertEqual(self.rows(
            "SELECT meta_source_id, meta_visit_id, meta_client_user_agent FROM user_bot",
        ), [(SETTINGS.source, meta_store.digest(token), "known-browser")])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM meta_bot_events"), [(0,)])

    def test_cookie_fbc_enriches_only_when_initial_visit_has_no_fbc(self):
        for token, initial in [
            ("mv_" + "e" * 32, {}),
            ("mv_" + "f" * 32, {"fbc": "fb.1.1700000000000.current_click"}),
        ]:
            meta_store.create_visit(token, SETTINGS.source, SETTINGS.pixel, initial, 60)
            meta_store.enrich_visit(token, {"fbc": "fb.1.1700000000000.cookie"})
            data = self.rows(
                "SELECT user_data FROM meta_bot_visits WHERE token_hash = %s",
                (meta_store.digest(token),),
            )[0][0]
            self.assertEqual(data["fbc"], initial.get("fbc", "fb.1.1700000000000.cookie"))

    def test_event_failure_rolls_back_visit_consumption_but_keeps_user(self):
        token = "mv_" + "c" * 32
        meta_store.create_visit(token, SETTINGS.source, SETTINGS.pixel, {}, 60)
        with meta_store.transaction() as cursor:
            cursor.execute("""
                ALTER TABLE meta_bot_events ADD CONSTRAINT forced_failure
                CHECK (pixel_id != '123456789')
            """)
        try:
            with self.assertLogs("core.meta_store", level="WARNING"):
                self.assertFalse(self.register(start=token))
            self.assertEqual(self.rows("SELECT meta_source_id FROM user_bot"), [(None,)])
            self.assertEqual(self.rows(
                "SELECT meta_visit_id, meta_fbp, meta_visit_at FROM user_bot",
            ), [(None, None, None)])
            self.assertEqual(self.rows(
                "SELECT consumed_at, consumed_by FROM meta_bot_visits",
            ), [(None, None)])
        finally:
            with meta_store.transaction() as cursor:
                cursor.execute("ALTER TABLE meta_bot_events DROP CONSTRAINT forced_failure")
        self.assertTrue(self.register(start=token))

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

    def test_outbox_failure_keeps_basic_registration_and_allows_tracking_retry(self):
        self.rows(
            "INSERT INTO user_bot (chat_id, meta_source_id) VALUES (2, %s) RETURNING chat_id",
            ("previous_source",),
        )
        with meta_store.transaction() as cursor:
            cursor.execute("""
                ALTER TABLE meta_bot_events ADD CONSTRAINT forced_failure
                CHECK (pixel_id != '123456789')
            """)
        try:
            with self.assertLogs("core.meta_store", level="WARNING") as logs:
                self.assertFalse(self.register())
                self.assertFalse(self.register(2))
            self.assertIn("CheckViolation", " ".join(logs.output))
            self.assertIn("23514", " ".join(logs.output))
            self.assertNotIn("123456789", " ".join(logs.output))
            self.assertEqual(self.rows(
                "SELECT chat_id, meta_source_id FROM user_bot ORDER BY chat_id",
            ), [(1, None), (2, "previous_source")])
            self.assertEqual(self.rows("SELECT COUNT(*) FROM meta_bot_events"), [(0,)])
        finally:
            with meta_store.transaction() as cursor:
                cursor.execute("ALTER TABLE meta_bot_events DROP CONSTRAINT forced_failure")
        self.assertTrue(self.register())
        self.assertTrue(self.register(2))

    def test_missing_source_column_does_not_remove_basic_user(self):
        with meta_store.transaction() as cursor:
            cursor.execute(
                "ALTER TABLE user_bot RENAME COLUMN meta_source_id TO unavailable_source"
            )
        try:
            with self.assertLogs("core.meta_store", level="WARNING") as logs:
                self.assertFalse(self.register())
            self.assertIn("UndefinedColumn", " ".join(logs.output))
            self.assertEqual(self.rows("SELECT chat_id FROM user_bot"), [(1,)])
        finally:
            with meta_store.transaction() as cursor:
                cursor.execute(
                    "ALTER TABLE user_bot RENAME COLUMN unavailable_source TO meta_source_id"
                )
        self.assertTrue(self.register())

    def test_tracking_connection_failure_falls_back_to_basic_registration(self):
        from core import db
        with (
            patch.object(db, "DATABASE_URL", "offline-test"),
            patch.object(db, "get_db_connection", side_effect=lambda **_: self.factory()),
            patch.object(meta_store, "register_start", side_effect=psycopg2.OperationalError(
                "sensitive connection details must not be logged"
            )),
            self.assertLogs("core.db", level="WARNING") as logs,
        ):
            db.save_user(1, "Test", "", meta_start=self.attribution)
        self.assertEqual(self.rows("SELECT chat_id FROM user_bot"), [(1,)])
        self.assertNotIn("sensitive connection", " ".join(logs.output))
        self.assertTrue(self.register())

    def test_plain_start_registration_does_not_depend_on_tracking(self):
        from core import db
        with (
            patch.object(db, "DATABASE_URL", "offline-test"),
            patch.object(db, "get_db_connection", side_effect=self.factory),
        ):
            db.save_user(1, "Test", "")
        self.assertEqual(self.rows(
            "SELECT chat_id, meta_source_id FROM user_bot",
        ), [(1, None)])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM meta_bot_events"), [(0,)])

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

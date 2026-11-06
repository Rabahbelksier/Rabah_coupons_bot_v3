import hashlib
from unittest import TestCase
from unittest.mock import MagicMock, Mock, patch

from flask import Flask
from core import db
from handlers import meta_landing
from services import meta_pixel


class MetaAttributionDatabaseTests(TestCase):
    def setUp(self):
        self.connection = MagicMock()
        self.cursor = self.connection.cursor.return_value.__enter__.return_value

    @patch.object(db, "DATABASE_URL", "postgresql://test")
    @patch.object(db, "get_db_connection")
    def test_matching_new_or_changed_source_queues_event_once(self, get_connection):
        get_connection.return_value = self.connection
        self.cursor.fetchone.return_value = (42,)
        self.cursor.fetchall.return_value = [
            (17, 123, "campaign-current", None, None),
        ]

        pending = db.save_user(
            123,
            "A",
            "B",
            attribution_id="campaign-current",
            expected_attribution_id="campaign-current",
        )

        self.assertEqual(pending, [{
            "id": 17,
            "chat_id": 123,
            "attribution_id": "campaign-current",
        }])
        sql_calls = [
            call.args[0]
            for call in self.cursor.execute.call_args_list
        ]
        self.assertIn("IS DISTINCT FROM", sql_calls[0])
        self.assertIn("INSERT INTO meta_event_outbox", sql_calls[1])
        self.connection.commit.assert_called_once()

    @patch.object(db, "DATABASE_URL", "postgresql://test")
    @patch.object(db, "get_db_connection")
    def test_click_token_transfers_browser_ids_to_queued_event(
        self,
        get_connection,
    ):
        get_connection.return_value = self.connection
        self.cursor.fetchone.side_effect = [
            (
                "campaign-current",
                "fb.1.1730000000000.browser",
                "fb.1.1730000000000.click",
            ),
            (42,),
        ]
        self.cursor.fetchall.return_value = [
            (
                17,
                123,
                "campaign-current",
                "fb.1.1730000000000.browser",
                "fb.1.1730000000000.click",
            ),
        ]

        pending = db.save_user(
            123,
            "A",
            "B",
            attribution_id="m_click-token",
            expected_attribution_id="campaign-current",
        )

        self.assertEqual(pending, [{
            "id": 17,
            "chat_id": 123,
            "attribution_id": "campaign-current",
            "fbp": "fb.1.1730000000000.browser",
            "fbc": "fb.1.1730000000000.click",
        }])
        sql_calls = [
            call.args[0]
            for call in self.cursor.execute.call_args_list
        ]
        self.assertIn("DELETE FROM meta_click_attributions", sql_calls[0])
        self.assertIn("INSERT INTO meta_event_outbox", sql_calls[2])
        self.connection.commit.assert_called_once()

    @patch.object(db, "DATABASE_URL", "postgresql://test")
    @patch.object(db, "get_db_connection")
    @patch.object(db.secrets, "token_urlsafe", return_value="random-token")
    def test_create_click_attribution_stores_only_hashed_token(
        self,
        _token_urlsafe,
        get_connection,
    ):
        get_connection.return_value = self.connection

        token = db.create_meta_click_attribution(
            "campaign-current",
            fbp="fb.1.1730000000000.browser",
            fbc="fb.1.1730000000000.click",
        )

        self.assertEqual(token, "m_random-token")
        sql_calls = self.cursor.execute.call_args_list
        self.assertIn(
            "DELETE FROM meta_click_attributions",
            sql_calls[0].args[0],
        )
        self.assertIn(
            "INSERT INTO meta_click_attributions",
            sql_calls[1].args[0],
        )
        stored_token_hash = sql_calls[1].args[1][0]
        self.assertNotEqual(stored_token_hash, token)
        self.assertEqual(len(stored_token_hash), 64)
        self.assertEqual(
            sql_calls[1].args[1][1:],
            (
                "campaign-current",
                "fb.1.1730000000000.browser",
                "fb.1.1730000000000.click",
            ),
        )
        self.connection.commit.assert_called_once()

    @patch.object(db, "DATABASE_URL", "postgresql://test")
    @patch.object(db, "get_db_connection")
    def test_same_source_does_not_create_duplicate_event(self, get_connection):
        get_connection.return_value = self.connection
        self.cursor.fetchone.return_value = None
        self.cursor.fetchall.return_value = []

        pending = db.save_user(
            123,
            "A",
            "B",
            attribution_id="campaign-current",
            expected_attribution_id="campaign-current",
        )

        self.assertEqual(pending, [])
        sql_calls = [
            call.args[0]
            for call in self.cursor.execute.call_args_list
        ]
        self.assertEqual(len(sql_calls), 2)
        self.assertNotIn("INSERT INTO meta_event_outbox", sql_calls)

    @patch.object(db, "DATABASE_URL", "postgresql://test")
    @patch.object(db, "get_db_connection")
    def test_nonmatching_source_does_not_overwrite_or_queue(self, get_connection):
        get_connection.return_value = self.connection

        pending = db.save_user(
            123,
            "A",
            "B",
            attribution_id="campaign-old",
            expected_attribution_id="campaign-current",
        )

        self.assertEqual(pending, [])
        self.cursor.execute.assert_called_once()
        self.assertIn(
            "ON CONFLICT (chat_id) DO NOTHING",
            self.cursor.execute.call_args.args[0],
        )


class MetaPixelEventTests(TestCase):
    def test_landing_page_sends_existing_browser_events_and_campaign_link(self):
        app = Flask(__name__)
        app.register_blueprint(meta_landing.meta_landing)

        with (
            patch.object(meta_landing, "META_PIXEL_ID", "123456"),
            patch.object(
                meta_landing,
                "META_ATTRIBUTION_ID",
                "campaign-current",
            ),
        ):
            response = app.test_client().get("/bot")

        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("fbq('track', 'PageView')", body)
        self.assertIn("fbq('trackCustom', 'TelegramClick')", body)
        self.assertIn("fetch('/bot/attribution'", body)
        self.assertIn("readMetaCookie('_fbp')", body)
        self.assertIn("readMetaCookie('_fbc')", body)
        self.assertEqual(body.count("window.location.replace"), 1)
        self.assertIn(
            "https://t.me/rabahcoupons1bot?start=campaign-current",
            body,
        )
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    @patch.object(meta_landing, "create_meta_click_attribution", return_value=None)
    def test_landing_endpoint_falls_back_to_campaign_without_cookies(
        self,
        create_attribution,
    ):
        app = Flask(__name__)
        app.register_blueprint(meta_landing.meta_landing)

        with (
            patch.object(meta_landing, "META_PIXEL_ID", "123456"),
            patch.object(
                meta_landing,
                "META_ATTRIBUTION_ID",
                "campaign-current",
            ),
        ):
            response = app.test_client().post(
                "/bot/attribution",
                base_url="https://bot.example",
                headers={"Origin": "https://bot.example"},
                json={},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json(),
            {"start_payload": "campaign-current"},
        )
        create_attribution.assert_called_once_with(
            "campaign-current",
            fbp=None,
            fbc=None,
        )

    def test_landing_page_rejects_invalid_telegram_payload(self):
        app = Flask(__name__)
        app.register_blueprint(meta_landing.meta_landing)

        with (
            patch.object(meta_landing, "META_PIXEL_ID", "123456"),
            patch.object(meta_landing, "META_ATTRIBUTION_ID", "not allowed"),
        ):
            response = app.test_client().get("/bot")

        self.assertEqual(response.status_code, 503)

    @patch.object(
        meta_landing,
        "create_meta_click_attribution",
        return_value="m_click-token",
    )
    def test_landing_endpoint_stores_valid_browser_ids(self, create_attribution):
        app = Flask(__name__)
        app.register_blueprint(meta_landing.meta_landing)

        with (
            patch.object(meta_landing, "META_PIXEL_ID", "123456"),
            patch.object(
                meta_landing,
                "META_ATTRIBUTION_ID",
                "campaign-current",
            ),
        ):
            response = app.test_client().post(
                "/bot/attribution",
                base_url="https://bot.example",
                headers={"Origin": "https://bot.example"},
                json={
                    "fbp": "fb.1.1730000000000.browser",
                    "fbc": "fb.1.1730000000000.click",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json(),
            {"start_payload": "m_click-token"},
        )
        create_attribution.assert_called_once_with(
            "campaign-current",
            fbp="fb.1.1730000000000.browser",
            fbc="fb.1.1730000000000.click",
        )
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    @patch.object(meta_landing, "create_meta_click_attribution")
    def test_landing_endpoint_rejects_cross_origin_request(
        self,
        create_attribution,
    ):
        app = Flask(__name__)
        app.register_blueprint(meta_landing.meta_landing)

        with (
            patch.object(meta_landing, "META_PIXEL_ID", "123456"),
            patch.object(
                meta_landing,
                "META_ATTRIBUTION_ID",
                "campaign-current",
            ),
        ):
            response = app.test_client().post(
                "/bot/attribution",
                base_url="https://bot.example",
                headers={"Origin": "https://attacker.example"},
                json={"fbp": "fb.1.1730000000000.browser"},
            )

        self.assertEqual(response.status_code, 403)
        create_attribution.assert_not_called()

    @patch.object(meta_pixel.requests, "post")
    def test_sends_custom_event_to_same_pixel_with_hashed_telegram_id(
        self,
        post,
    ):
        response = Mock()
        response.json.return_value = {"events_received": 1}
        post.return_value = response

        with (
            patch.object(meta_pixel, "META_PIXEL_ID", "123456"),
            patch.object(meta_pixel, "META_CAPI_ACCESS_TOKEN", "test-token"),
            patch.object(meta_pixel, "META_TEST_EVENT_CODE", "TEST123"),
        ):
            meta_pixel._send_telegram_start_event({
                "id": 17,
                "chat_id": 123,
                "attribution_id": "campaign-current",
            })

        post.assert_called_once()
        args, kwargs = post.call_args
        self.assertEqual(
            args[0],
            "https://graph.facebook.com/v26.0/123456/events",
        )
        self.assertEqual(kwargs["params"], {"access_token": "test-token"})
        self.assertEqual(kwargs["json"]["test_event_code"], "TEST123")
        event = kwargs["json"]["data"][0]
        self.assertEqual(event["event_name"], "TelegramBotStart")
        self.assertEqual(event["event_id"], "telegram-start-17")
        self.assertEqual(event["action_source"], "chat")
        self.assertEqual(
            event["user_data"]["external_id"],
            [hashlib.sha256(b"123").hexdigest()],
        )
        self.assertEqual(
            event["custom_data"]["attribution_id"],
            "campaign-current",
        )
        response.raise_for_status.assert_called_once()

    @patch.object(meta_pixel.requests, "post")
    def test_includes_browser_ids_in_server_user_data_when_available(self, post):
        response = Mock()
        response.json.return_value = {"events_received": 1}
        post.return_value = response

        with (
            patch.object(meta_pixel, "META_PIXEL_ID", "123456"),
            patch.object(meta_pixel, "META_CAPI_ACCESS_TOKEN", "test-token"),
            patch.object(meta_pixel, "META_TEST_EVENT_CODE", ""),
        ):
            meta_pixel._send_telegram_start_event({
                "id": 19,
                "chat_id": 789,
                "attribution_id": "campaign-current",
                "fbp": "fb.1.1730000000000.browser",
                "fbc": "fb.1.1730000000000.click",
            })

        user_data = post.call_args.kwargs["json"]["data"][0]["user_data"]
        self.assertEqual(user_data["fbp"], "fb.1.1730000000000.browser")
        self.assertEqual(user_data["fbc"], "fb.1.1730000000000.click")

    @patch.object(meta_pixel.requests, "post")
    def test_omits_test_event_code_when_not_configured(self, post):
        response = Mock()
        response.json.return_value = {"events_received": 1}
        post.return_value = response

        with (
            patch.object(meta_pixel, "META_PIXEL_ID", "123456"),
            patch.object(meta_pixel, "META_CAPI_ACCESS_TOKEN", "test-token"),
            patch.object(meta_pixel, "META_TEST_EVENT_CODE", ""),
        ):
            meta_pixel._send_telegram_start_event({
                "id": 18,
                "chat_id": 456,
                "attribution_id": "campaign-current",
            })

        payload = post.call_args.kwargs["json"]
        self.assertNotIn("test_event_code", payload)
        self.assertEqual(payload["data"][0]["event_name"], "TelegramBotStart")

    @patch.object(meta_pixel, "mark_meta_event_sent")
    @patch.object(meta_pixel, "_send_telegram_start_event")
    @patch.object(meta_pixel, "mark_meta_event_failed")
    def test_failed_event_remains_pending_for_retry(
        self,
        mark_failed,
        send,
        mark_sent,
    ):
        send.side_effect = RuntimeError("provider error")
        event = {"id": 2, "chat_id": 3, "attribution_id": "campaign"}

        meta_pixel.send_pending_telegram_start_events([event])

        mark_failed.assert_called_once()
        mark_sent.assert_not_called()

    @patch.object(meta_pixel, "mark_meta_event_sent")
    @patch.object(meta_pixel, "_send_telegram_start_event")
    @patch.object(meta_pixel, "mark_meta_event_failed")
    def test_successful_event_is_marked_sent(
        self,
        mark_failed,
        send,
        mark_sent,
    ):
        event = {"id": 2, "chat_id": 3, "attribution_id": "campaign"}

        meta_pixel.send_pending_telegram_start_events([event])

        send.assert_called_once_with(event)
        mark_sent.assert_called_once_with(2)
        mark_failed.assert_not_called()


if __name__ == "__main__":
    import unittest

    unittest.main()

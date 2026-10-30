import os
import unittest
from unittest.mock import patch

for name in ("APP_KEY", "APP_SECRET", "TRACKING_ID", "TELEGRAM_TOKEN"):
    os.environ.setdefault(name, "test")

from core import db


class ErrorLogPeriodTests(unittest.TestCase):
    @patch.object(db, "DATABASE_URL", "postgres://test")
    @patch.object(db, "get_db_connection")
    def test_get_error_logs_keeps_existing_windows_and_supports_short_windows(
        self,
        get_connection,
    ):
        cursor = get_connection.return_value.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = []

        for period, expected_interval in (
            ("today", "1 day"),
            ("week", "7 days"),
            ("hour", "1 hour"),
            ("quarter", "15 minutes"),
        ):
            with self.subTest(period=period):
                db.get_error_logs(period)
                query, params = cursor.execute.call_args.args
                self.assertIn(
                    "CURRENT_TIMESTAMP - %s::interval",
                    query,
                )
                self.assertEqual(params, (expected_interval,))


class ErrorLogCleanupTests(unittest.TestCase):
    @patch.object(db, "ERROR_LOG_RETENTION_DAYS", 30)
    @patch.object(db, "DATABASE_URL", "postgres://test")
    @patch.object(db, "get_db_connection")
    def test_cleanup_uses_configured_retention_and_commits(
        self,
        get_connection,
    ):
        connection = get_connection.return_value
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.rowcount = 4

        deleted = db.delete_expired_error_logs()

        self.assertEqual(deleted, 4)
        query, params = cursor.execute.call_args.args
        self.assertIn("DELETE FROM bot_error_logs", query)
        self.assertIn("created_at < CURRENT_TIMESTAMP", query)
        self.assertIn("INTERVAL '1 day'", query)
        self.assertEqual(params, (30,))
        connection.commit.assert_called_once_with()


class InaccessibleUserCleanupTests(unittest.TestCase):
    @patch.object(db, "DATABASE_URL", "postgres://test")
    @patch.object(db, "get_db_connection")
    def test_delete_user_chat_ids_deletes_only_supplied_ids_and_commits(
        self,
        get_connection,
    ):
        connection = get_connection.return_value
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.rowcount = 2

        deleted = db.delete_user_chat_ids([101, 202])

        self.assertEqual(deleted, 2)
        query, params = cursor.execute.call_args.args
        self.assertIn("DELETE FROM user_bot", query)
        self.assertIn("chat_id = ANY(%s)", query)
        self.assertEqual(params, ([101, 202],))
        connection.commit.assert_called_once_with()


class RegisteredUserCountTests(unittest.TestCase):
    @patch.object(db, "DATABASE_URL", "postgres://test")
    @patch.object(db, "get_db_connection")
    def test_get_total_user_count_uses_database_aggregate(self, get_connection):
        connection = get_connection.return_value
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = (17,)

        count = db.get_total_user_count()

        self.assertEqual(count, 17)
        cursor.execute.assert_called_once_with(
            "SELECT COUNT(*) FROM user_bot"
        )
        cursor.fetchall.assert_not_called()
        connection.close.assert_called_once_with()

    @patch.object(db, "DATABASE_URL", "")
    @patch.object(db, "get_db_connection")
    def test_get_total_user_count_returns_zero_without_database(
        self,
        get_connection,
    ):
        self.assertEqual(db.get_total_user_count(), 0)
        get_connection.assert_not_called()
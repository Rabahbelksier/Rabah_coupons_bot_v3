import os
import unittest
from unittest.mock import patch

for name in ("APP_KEY", "APP_SECRET", "TRACKING_ID", "TELEGRAM_TOKEN"):
    os.environ.setdefault(name, "test")

from core import db


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
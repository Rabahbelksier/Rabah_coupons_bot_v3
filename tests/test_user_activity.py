import os
import unittest
from unittest.mock import MagicMock, patch

for name in ("APP_KEY", "APP_SECRET", "TRACKING_ID", "TELEGRAM_TOKEN"):
    os.environ.setdefault(name, "test")

from core import db


class ActivityStatsTests(unittest.TestCase):
    def run_stats_query(self, period):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = (7, 2)
        with (
            patch.object(db, "DATABASE_URL", "postgresql://test"),
            patch.object(db, "get_db_connection", return_value=connection),
        ):
            result = db.get_activity_stats(period)
        return result, cursor.execute.call_args.args[0]

    def test_daily_periods_use_the_daily_activity_table(self):
        expected_filters = {
            "yesterday": "activity_day = CURRENT_DATE - 1",
            "this_week": "date_trunc('week', CURRENT_DATE)",
            "last_week": "date_trunc('week', CURRENT_DATE)::date - 7",
            "year": "date_trunc('year', CURRENT_DATE)",
            "last_year": "INTERVAL '1 year'",
            "all": "WHERE TRUE",
        }
        for period, expected_filter in expected_filters.items():
            with self.subTest(period=period):
                result, query = self.run_stats_query(period)
                self.assertEqual(result, {"requests": 7, "users": 2})
                self.assertIn("FROM user_activity_daily", query)
                self.assertIn(expected_filter, query)
                self.assertIn("COUNT(DISTINCT chat_id)", query)

    def test_existing_today_and_month_stats_keep_their_counters(self):
        for period, column in (
            ("today", "requests_today"),
            ("month", "requests_this_month"),
            ("previous", "requests_last_month"),
        ):
            with self.subTest(period=period):
                result, query = self.run_stats_query(period)
                self.assertEqual(result, {"requests": 7, "users": 2})
                self.assertIn(f"SUM({column})", query)
                self.assertIn("FROM user_bot", query)


if __name__ == "__main__":
    unittest.main()

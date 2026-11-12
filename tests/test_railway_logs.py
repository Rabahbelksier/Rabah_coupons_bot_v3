import asyncio
import json
import os
import unittest
from contextlib import ExitStack
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from services import railway_logs


class RailwayLogParsingTests(unittest.TestCase):
    def test_parses_json_lines_and_single_json_document(self):
        records = railway_logs._parse_cli_output(
            b'{"timestamp":"one"}\n{"timestamp":"two"}\n'
        )
        self.assertEqual(
            records,
            [{"timestamp": "one"}, {"timestamp": "two"}],
        )
        self.assertEqual(
            railway_logs._parse_cli_output(b'{"timestamp":"single"}'),
            [{"timestamp": "single"}],
        )
        self.assertEqual(railway_logs._parse_cli_output(b" \n"), [])

    def test_rejects_malformed_json_instead_of_silently_dropping_logs(self):
        with self.assertRaises(railway_logs.RailwayLogsError):
            railway_logs._parse_cli_output(b'{"valid":true}\nnot-json\n')

    def test_month_starts_at_midnight_in_algiers(self):
        now = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(
            railway_logs._month_start_utc(now),
            "2026-10-31T23:00:00Z",
        )


class RailwayLogFetchTests(unittest.TestCase):
    def _set_railway_settings(self):
        return (
            patch.object(railway_logs, "RAILWAY_CLI_PATH", "railway"),
            patch.object(
                railway_logs,
                "RAILWAY_LOGS_PROJECT_ID",
                "project-id",
            ),
            patch.object(
                railway_logs,
                "RAILWAY_LOGS_ENVIRONMENT_ID",
                "environment-id",
            ),
            patch.object(
                railway_logs,
                "RAILWAY_LOGS_SERVICE_ID",
                "service-id",
            ),
        )

    def test_fetches_cli_json_and_builds_valid_json_file(self):
        process = AsyncMock()
        process.returncode = 0
        process.communicate.return_value = (
            b'{"timestamp":"2026-11-12T10:00:00Z","message":"failure"}\n',
            b"",
        )

        patches = self._set_railway_settings()
        with ExitStack() as stack:
            stack.enter_context(
                patch.dict(os.environ, {"RAILWAY_TOKEN": "test-token"})
            )
            stack.enter_context(
                patch(
                    "services.railway_logs.shutil.which",
                    return_value="/bin/railway",
                )
            )
            create_process = stack.enter_context(
                patch(
                    "services.railway_logs.asyncio.create_subprocess_exec",
                    new=AsyncMock(return_value=process),
                )
            )
            for patcher in patches:
                stack.enter_context(patcher)
            log_file = asyncio.run(railway_logs.fetch_railway_log_file("15m"))

        document = json.loads(log_file.payload)
        self.assertEqual(document["period"], "15m")
        self.assertEqual(document["period_label"], "آخر 15 دقيقة")
        self.assertEqual(document["record_count"], 1)
        self.assertEqual(document["logs"][0]["message"], "failure")
        self.assertFalse(log_file.possibly_truncated)
        command = create_process.await_args.args
        self.assertEqual(command[0], "/bin/railway")
        self.assertIn("--json", command)
        self.assertIn("--since", command)
        self.assertIn("15m", command)
        self.assertIn("--project", command)
        self.assertIn("project-id", command)
        self.assertNotIn("test-token", command)

    def test_missing_token_fails_before_starting_cli(self):
        patches = self._set_railway_settings()
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {}, clear=True))
            create_process = stack.enter_context(
                patch(
                    "services.railway_logs.asyncio.create_subprocess_exec",
                    new=AsyncMock(),
                )
            )
            for patcher in patches:
                stack.enter_context(patcher)
            with self.assertRaisesRegex(
                railway_logs.RailwayLogsError,
                "RAILWAY_TOKEN",
            ):
                asyncio.run(railway_logs.fetch_railway_log_file("month"))
        create_process.assert_not_awaited()

    def test_legacy_quarter_alias_uses_fifteen_minute_period(self):
        process = AsyncMock()
        process.returncode = 0
        process.communicate.return_value = (b"", b"")
        patches = self._set_railway_settings()
        with ExitStack() as stack:
            stack.enter_context(
                patch.dict(os.environ, {"RAILWAY_TOKEN": "test-token"})
            )
            stack.enter_context(
                patch(
                    "services.railway_logs.shutil.which",
                    return_value="/bin/railway",
                )
            )
            stack.enter_context(
                patch(
                    "services.railway_logs.asyncio.create_subprocess_exec",
                    new=AsyncMock(return_value=process),
                )
            )
            for patcher in patches:
                stack.enter_context(patcher)
            log_file = asyncio.run(
                railway_logs.fetch_railway_log_file("quarter")
            )
        self.assertEqual(json.loads(log_file.payload)["period"], "15m")

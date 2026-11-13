import asyncio
import json
import os
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from services import railway_logs


class RailwayLogParsingTests(unittest.TestCase):
    def test_parses_graphql_api_data(self):
        data = railway_logs._parse_api_response(
            b'{"data":{"deploymentLogs":[{"timestamp":"one"}]}}'
        )
        self.assertEqual(
            data,
            {"deploymentLogs": [{"timestamp": "one"}]},
        )

    def test_rejects_malformed_or_failed_api_response(self):
        with self.assertRaises(railway_logs.RailwayLogsError):
            railway_logs._parse_api_response(b'{"valid":true}\nnot-json\n')
        with self.assertRaises(railway_logs.RailwayLogsError):
            railway_logs._parse_api_response(
                b'{"errors":[{"message":"denied"}]}'
            )

    def test_month_starts_at_midnight_in_algiers(self):
        now = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(
            railway_logs._month_start_utc(now),
            "2026-10-31T23:00:00Z",
        )
        start, end = railway_logs._period_range("month", now)
        self.assertEqual(start, "2026-10-31T23:00:00Z")
        self.assertEqual(end, "2026-11-12T12:00:00Z")

    def test_selects_deployments_covering_a_range_and_previous_success(self):
        start = datetime(2026, 11, 1, tzinfo=timezone.utc)
        end = datetime(2026, 11, 10, tzinfo=timezone.utc)
        deployments = [
            {
                "id": "old-success",
                "status": "SUCCESS",
                "createdAt": "2026-10-20T00:00:00Z",
            },
            {
                "id": "previous-crashed",
                "status": "CRASHED",
                "createdAt": "2026-10-30T00:00:00Z",
            },
            {
                "id": "inside-range",
                "status": "SUCCESS",
                "createdAt": "2026-11-05T00:00:00Z",
            },
            {
                "id": "after-range",
                "status": "SUCCESS",
                "createdAt": "2026-11-11T00:00:00Z",
            },
        ]

        selected = railway_logs._select_relevant_deployments(
            deployments,
            start,
            end,
        )

        self.assertEqual(
            [deployment_id for _, deployment_id, _ in selected],
            ["inside-range", "previous-crashed", "old-success"],
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
        deployment_created = (
            datetime.now(timezone.utc) - timedelta(days=2)
        ).isoformat().replace("+00:00", "Z")

        def process_for_output(stdout):
            process = unittest.mock.MagicMock()
            process.returncode = 0
            process.communicate = AsyncMock(return_value=(stdout, b""))
            return process

        async def create_subprocess(*command, **_kwargs):
            query = command[2]
            if "Deployments(" in query:
                response = {
                    "data": {
                        "deployments": {
                            "edges": [{
                                "cursor": "cursor-one",
                                "node": {
                                    "id": "deployment-current",
                                    "status": "SUCCESS",
                                    "createdAt": deployment_created,
                                },
                            }],
                            "pageInfo": {
                                "hasNextPage": False,
                                "endCursor": "cursor-one",
                            },
                        },
                    },
                }
            else:
                response = {
                    "data": {
                        "deploymentLogs": [{
                            "timestamp": (
                                datetime.now(timezone.utc)
                                - timedelta(seconds=1)
                            )
                            .isoformat()
                            .replace("+00:00", "Z"),
                            "message": "failure",
                            "severity": "error",
                        }],
                    },
                }
            return process_for_output(json.dumps(response).encode())

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
                    new=AsyncMock(side_effect=create_subprocess),
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
        self.assertEqual(
            document["logs"][0]["deploymentId"],
            "deployment-current",
        )
        self.assertFalse(log_file.possibly_truncated)
        self.assertEqual(create_process.await_count, 2)
        log_command = create_process.await_args.args
        self.assertEqual(log_command[0], "/bin/railway")
        self.assertEqual(log_command[1], "api")
        self.assertIn("DeploymentLogs(", log_command[2])
        variables = json.loads(log_command[4])
        self.assertEqual(variables["deploymentId"], "deployment-current")
        self.assertEqual(variables["startDate"][-1], "Z")
        self.assertEqual(variables["endDate"][-1], "Z")
        self.assertNotIn("test-token", log_command)

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
        deployment_created = (
            datetime.now(timezone.utc) - timedelta(days=1)
        ).isoformat().replace("+00:00", "Z")

        def process_for_output(stdout):
            process = unittest.mock.MagicMock()
            process.returncode = 0
            process.communicate = AsyncMock(return_value=(stdout, b""))
            return process

        async def create_subprocess(*command, **_kwargs):
            if "Deployments(" in command[2]:
                response = {
                    "data": {
                        "deployments": {
                            "edges": [{
                                "cursor": "cursor-one",
                                "node": {
                                    "id": "deployment-current",
                                    "status": "SUCCESS",
                                    "createdAt": deployment_created,
                                },
                            }],
                            "pageInfo": {
                                "hasNextPage": False,
                                "endCursor": "cursor-one",
                            },
                        },
                    },
                }
            else:
                response = {"data": {"deploymentLogs": []}}
            return process_for_output(json.dumps(response).encode())

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
                    new=AsyncMock(side_effect=create_subprocess),
                )
            )
            for patcher in patches:
                stack.enter_context(patcher)
            log_file = asyncio.run(
                railway_logs.fetch_railway_log_file("quarter")
            )
        self.assertEqual(json.loads(log_file.payload)["period"], "15m")

import asyncio
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from config import (
    RAILWAY_CLI_PATH,
    RAILWAY_LOGS_ENVIRONMENT_ID,
    RAILWAY_LOGS_MAX_LINES,
    RAILWAY_LOGS_PROJECT_ID,
    RAILWAY_LOGS_SERVICE_ID,
    RAILWAY_LOGS_TIMEOUT_SECONDS,
)


ALGIERS_TIMEZONE = timezone(timedelta(hours=1))
LOG_PERIODS = {
    "15m": {
        "label": "آخر 15 دقيقة",
        "filename": "last-15-minutes",
        "seconds": 15 * 60,
    },
    "hour": {
        "label": "آخر ساعة",
        "filename": "last-hour",
        "seconds": 60 * 60,
    },
    "today": {
        "label": "اليوم (آخر 24 ساعة)",
        "filename": "today",
        "seconds": 24 * 60 * 60,
    },
    "week": {
        "label": "آخر أسبوع",
        "filename": "last-week",
        "seconds": 7 * 24 * 60 * 60,
    },
    "month": {
        "label": "هذا الشهر",
        "filename": "this-month",
        "seconds": None,
    },
}
MAX_DEPLOYMENTS_TO_LIST = 1000
DEPLOYMENTS_PAGE_SIZE = 100
MAX_LOG_LINES_PER_DEPLOYMENT = 5000
RUNTIME_DEPLOYMENT_STATUSES = {
    "SUCCESS",
    "CRASHED",
    "REMOVED",
    "REMOVING",
    "SLEEPING",
}
DEPLOYMENTS_QUERY = """
query Deployments($input: DeploymentListInput!, $first: Int!, $after: String) {
  deployments(input: $input, first: $first, after: $after) {
    edges {
      cursor
      node { id status createdAt }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""
DEPLOYMENT_LOGS_QUERY = """
query DeploymentLogs(
  $deploymentId: String!,
  $limit: Int,
  $startDate: DateTime,
  $endDate: DateTime
) {
  deploymentLogs(
    deploymentId: $deploymentId,
    limit: $limit,
    startDate: $startDate,
    endDate: $endDate
  ) {
    timestamp
    message
    severity
  }
}
"""


class RailwayLogsError(RuntimeError):
    """A safe-to-display failure while retrieving Railway runtime logs."""


@dataclass(frozen=True)
class RailwayLogFile:
    payload: bytes
    count: int
    possibly_truncated: bool
    max_lines: int


def _month_start_utc(now=None):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    local_now = now.astimezone(ALGIERS_TIMEZONE)
    start_local = local_now.replace(
        day=1,
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    return start_local.astimezone(timezone.utc).isoformat().replace(
        "+00:00",
        "Z",
    )


def _period_range(period, now=None):
    period_settings = LOG_PERIODS[period]
    end = now or datetime.now(timezone.utc)
    if end.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    end = end.astimezone(timezone.utc)
    if period == "month":
        start_text = _month_start_utc(end)
        start = datetime.fromisoformat(start_text.replace("Z", "+00:00"))
    else:
        start = end - timedelta(seconds=period_settings["seconds"])
    format_utc = lambda value: value.isoformat().replace("+00:00", "Z")
    return format_utc(start), format_utc(end)


def _parse_api_response(stdout):
    text = stdout.decode("utf-8", errors="replace").strip()
    if not text:
        raise RailwayLogsError("Railway Public API returned an empty response.")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RailwayLogsError(
            "Railway CLI did not return valid API JSON."
        ) from exc
    if not isinstance(parsed, dict):
        raise RailwayLogsError(
            "Railway Public API returned an unsupported response."
        )
    if parsed.get("errors"):
        raise RailwayLogsError(
            "Railway Public API rejected the requested log query."
        )
    data = parsed.get("data")
    if not isinstance(data, dict):
        raise RailwayLogsError(
            "Railway Public API response omitted its data."
        )
    return data


def _log_timestamp(record):
    value = record.get("timestamp") or record.get("createdAt")
    if not value:
        raise RailwayLogsError(
            "Railway Public API log record omitted its timestamp."
        )
    try:
        timestamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise RailwayLogsError(
            "Railway Public API returned an invalid log timestamp."
        ) from exc
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc)


def _deployment_created_at(deployment):
    value = deployment.get("createdAt") or deployment.get("created_at")
    if not value:
        raise RailwayLogsError(
            "Railway CLI deployment list omitted a creation timestamp."
        )
    try:
        created_at = datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise RailwayLogsError(
            "Railway CLI returned an invalid deployment timestamp."
        ) from exc
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return created_at.astimezone(timezone.utc)


def _select_relevant_deployments(deployments, start, end):
    parsed = []
    for deployment in deployments:
        if not isinstance(deployment, dict):
            continue
        deployment_id = deployment.get("id") or deployment.get("deploymentId")
        if not deployment_id:
            continue
        created_at = _deployment_created_at(deployment)
        status = str(deployment.get("status", "")).upper()
        if status in RUNTIME_DEPLOYMENT_STATUSES and created_at <= end:
            parsed.append((created_at, str(deployment_id), status))

    parsed.sort(key=lambda row: row[0], reverse=True)
    within_range = [
        row for row in parsed
        if start <= row[0] <= end
    ]
    before_range = [row for row in parsed if row[0] < start]
    selected = list(within_range)
    if before_range:
        # Include the latest deployment before the range and the most recent
        # successful one, in case a later failed deploy never replaced it.
        selected.append(before_range[0])
        previous_success = next(
            (row for row in before_range if row[2] == "SUCCESS"),
            None,
        )
        if previous_success and previous_success not in selected:
            selected.append(previous_success)
    selected.sort(key=lambda row: row[0], reverse=True)
    return selected


def _required_settings():
    settings = {
        "RAILWAY_TOKEN": os.getenv("RAILWAY_TOKEN", "").strip(),
        "RAILWAY_LOGS_PROJECT_ID": RAILWAY_LOGS_PROJECT_ID,
        "RAILWAY_LOGS_ENVIRONMENT_ID": RAILWAY_LOGS_ENVIRONMENT_ID,
        "RAILWAY_LOGS_SERVICE_ID": RAILWAY_LOGS_SERVICE_ID,
    }
    missing = [name for name, value in settings.items() if not value]
    if missing:
        raise RailwayLogsError(
            "Missing Railway settings: " + ", ".join(missing)
        )
    return settings


async def _run_cli(executable, arguments, cwd, deadline):
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        raise RailwayLogsError("Railway CLI timed out.")
    try:
        process = await asyncio.create_subprocess_exec(
            executable,
            *arguments,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise RailwayLogsError("Could not start Railway CLI.") from exc

    try:
        stdout, _stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=remaining,
        )
    except asyncio.TimeoutError as exc:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise RailwayLogsError("Railway CLI timed out.") from exc

    if process.returncode:
        raise RailwayLogsError(
            f"Railway CLI exited with status {process.returncode}."
        )
    return stdout


async def fetch_railway_log_file(period):
    if period == "quarter":
        period = "15m"
    if period not in LOG_PERIODS:
        raise RailwayLogsError("Unknown log period.")

    settings = _required_settings()
    executable = shutil.which(RAILWAY_CLI_PATH)
    if not executable:
        raise RailwayLogsError("Railway CLI is not installed or not in PATH.")

    period_settings = LOG_PERIODS[period]
    since, until = _period_range(period)
    max_lines = min(
        RAILWAY_LOGS_MAX_LINES,
        MAX_LOG_LINES_PER_DEPLOYMENT,
    )
    possibly_truncated = RAILWAY_LOGS_MAX_LINES > max_lines
    loop = asyncio.get_running_loop()
    deadline = loop.time() + RAILWAY_LOGS_TIMEOUT_SECONDS
    records = []
    queried_deployments = []

    with tempfile.TemporaryDirectory(prefix="railway-logs-") as cli_directory:
        start = datetime.fromisoformat(since.replace("Z", "+00:00"))
        end = datetime.fromisoformat(until.replace("Z", "+00:00"))
        deployment_rows = []
        cursor = None
        has_more_deployments = False
        while len(deployment_rows) < MAX_DEPLOYMENTS_TO_LIST:
            page_size = min(
                DEPLOYMENTS_PAGE_SIZE,
                MAX_DEPLOYMENTS_TO_LIST - len(deployment_rows),
            )
            variables = {
                "input": {
                    "projectId": settings["RAILWAY_LOGS_PROJECT_ID"],
                    "environmentId": settings["RAILWAY_LOGS_ENVIRONMENT_ID"],
                    "serviceId": settings["RAILWAY_LOGS_SERVICE_ID"],
                },
                "first": page_size,
                "after": cursor,
            }
            output = await _run_cli(
                executable,
                [
                    "api",
                    DEPLOYMENTS_QUERY,
                    "--variables",
                    json.dumps(variables),
                ],
                cli_directory,
                deadline,
            )
            data = _parse_api_response(output)
            connection = data.get("deployments")
            if not isinstance(connection, dict):
                raise RailwayLogsError(
                    "Railway Public API omitted the deployment list."
                )
            edges = connection.get("edges")
            page_info = connection.get("pageInfo")
            if not isinstance(edges, list) or not isinstance(page_info, dict):
                raise RailwayLogsError(
                    "Railway Public API returned an invalid deployment page."
                )
            for edge in edges:
                if isinstance(edge, dict) and isinstance(edge.get("node"), dict):
                    deployment_rows.append(edge["node"])
            has_more_deployments = bool(page_info.get("hasNextPage"))
            if not has_more_deployments:
                break
            next_cursor = page_info.get("endCursor")
            if not next_cursor or next_cursor == cursor:
                raise RailwayLogsError(
                    "Railway Public API returned an invalid deployment cursor."
                )
            cursor = next_cursor

        if has_more_deployments:
            possibly_truncated = True

        deployments = _select_relevant_deployments(
            deployment_rows,
            start,
            end,
        )

        for _created_at, deployment_id, _status in deployments:
            remaining_lines = max_lines - len(records)
            if remaining_lines <= 0:
                possibly_truncated = True
                break
            variables = {
                "deploymentId": deployment_id,
                "limit": min(
                    remaining_lines,
                    MAX_LOG_LINES_PER_DEPLOYMENT,
                ),
                "startDate": since,
                "endDate": until,
            }
            output = await _run_cli(
                executable,
                [
                    "api",
                    DEPLOYMENT_LOGS_QUERY,
                    "--variables",
                    json.dumps(variables),
                ],
                cli_directory,
                deadline,
            )
            data = _parse_api_response(output)
            deployment_logs = data.get("deploymentLogs")
            if not isinstance(deployment_logs, list):
                raise RailwayLogsError(
                    "Railway Public API omitted deployment logs."
                )
            if len(deployment_logs) >= variables["limit"]:
                possibly_truncated = True
            for record in deployment_logs[:remaining_lines]:
                if isinstance(record, dict):
                    record = dict(record)
                    record.setdefault("deploymentId", deployment_id)
                    timestamp = _log_timestamp(record)
                    if start <= timestamp <= end:
                        records.append(record)
            queried_deployments.append(deployment_id)

    records.sort(
        key=lambda record: str(
            record.get("timestamp", record.get("createdAt", ""))
            if isinstance(record, dict)
            else ""
        ),
        reverse=True,
    )

    generated_at = datetime.now(timezone.utc).isoformat().replace(
        "+00:00",
        "Z",
    )
    document = {
        "source": "Railway CLI runtime logs",
        "period": period,
        "period_label": period_settings["label"],
        "since": since,
        "until": until,
        "generated_at": generated_at,
        "deployments_queried": queried_deployments,
        "record_count": len(records),
        "max_lines": max_lines,
        "possibly_truncated": possibly_truncated,
        "logs": records,
    }
    payload = json.dumps(
        document,
        ensure_ascii=False,
        indent=2,
    ).encode("utf-8")
    return RailwayLogFile(
        payload=payload,
        count=len(records),
        possibly_truncated=possibly_truncated,
        max_lines=max_lines,
    )

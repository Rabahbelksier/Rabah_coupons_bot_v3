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
RUNTIME_DEPLOYMENT_STATUSES = {
    "SUCCESS",
    "CRASHED",
    "REMOVED",
    "REMOVING",
    "SLEEPING",
}


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


def _parse_cli_output(stdout):
    text = stdout.decode("utf-8", errors="replace").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        records = []
        try:
            for line in text.splitlines():
                if line.strip():
                    records.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise RailwayLogsError(
                "Railway CLI did not return valid JSON."
            ) from exc
        return records
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        return [parsed]
    raise RailwayLogsError("Railway CLI returned an unsupported JSON value.")


def _deployment_records(stdout):
    records = _parse_cli_output(stdout)
    if len(records) == 1 and isinstance(records[0], dict):
        for key in ("deployments", "data"):
            nested = records[0].get(key)
            if isinstance(nested, list):
                return nested
    return records


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
    max_lines = RAILWAY_LOGS_MAX_LINES
    loop = asyncio.get_running_loop()
    deadline = loop.time() + RAILWAY_LOGS_TIMEOUT_SECONDS
    records = []
    queried_deployments = []
    possibly_truncated = False

    with tempfile.TemporaryDirectory(prefix="railway-logs-") as cli_directory:
        await _run_cli(
            executable,
            [
                "link",
                "--project",
                settings["RAILWAY_LOGS_PROJECT_ID"],
                "--environment",
                settings["RAILWAY_LOGS_ENVIRONMENT_ID"],
                "--service",
                settings["RAILWAY_LOGS_SERVICE_ID"],
                "--json",
            ],
            cli_directory,
            deadline,
        )
        deployment_output = await _run_cli(
            executable,
            [
                "deployment",
                "list",
                "--service",
                settings["RAILWAY_LOGS_SERVICE_ID"],
                "--environment",
                settings["RAILWAY_LOGS_ENVIRONMENT_ID"],
                "--limit",
                str(MAX_DEPLOYMENTS_TO_LIST),
                "--json",
            ],
            cli_directory,
            deadline,
        )
        deployment_rows = _deployment_records(deployment_output)
        start = datetime.fromisoformat(since.replace("Z", "+00:00"))
        end = datetime.fromisoformat(until.replace("Z", "+00:00"))
        deployments = _select_relevant_deployments(
            deployment_rows,
            start,
            end,
        )
        if (
            len(deployment_rows) >= MAX_DEPLOYMENTS_TO_LIST
            and not any(
                _deployment_created_at(row) < start
                and str(row.get("status", "")).upper() == "SUCCESS"
                for row in deployment_rows
                if isinstance(row, dict)
            )
        ):
            possibly_truncated = True

        for _created_at, deployment_id, _status in deployments:
            remaining_lines = max_lines - len(records)
            if remaining_lines <= 0:
                possibly_truncated = True
                break
            output = await _run_cli(
                executable,
                [
                    "logs",
                    deployment_id,
                    "--project",
                    settings["RAILWAY_LOGS_PROJECT_ID"],
                    "--environment",
                    settings["RAILWAY_LOGS_ENVIRONMENT_ID"],
                    "--service",
                    settings["RAILWAY_LOGS_SERVICE_ID"],
                    "--since",
                    since,
                    "--until",
                    until,
                    "--lines",
                    str(remaining_lines),
                    "--json",
                ],
                cli_directory,
                deadline,
            )
            deployment_logs = _parse_cli_output(output)
            if len(deployment_logs) >= remaining_lines:
                possibly_truncated = True
            for record in deployment_logs[:remaining_lines]:
                if isinstance(record, dict):
                    record = dict(record)
                    record.setdefault("deploymentId", deployment_id)
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

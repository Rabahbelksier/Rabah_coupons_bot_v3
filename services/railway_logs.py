import asyncio
import json
import os
import shutil
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
        "since": "15m",
    },
    "hour": {
        "label": "آخر ساعة",
        "filename": "last-hour",
        "since": "1h",
    },
    "today": {
        "label": "اليوم (آخر 24 ساعة)",
        "filename": "today",
        "since": "24h",
    },
    "week": {
        "label": "آخر أسبوع",
        "filename": "last-week",
        "since": "168h",
    },
    "month": {
        "label": "هذا الشهر",
        "filename": "this-month",
        "since": None,
    },
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
    since = period_settings["since"] or _month_start_utc()
    max_lines = RAILWAY_LOGS_MAX_LINES
    command = [
        executable,
        "logs",
        "--project",
        settings["RAILWAY_LOGS_PROJECT_ID"],
        "--environment",
        settings["RAILWAY_LOGS_ENVIRONMENT_ID"],
        "--service",
        settings["RAILWAY_LOGS_SERVICE_ID"],
        "--since",
        since,
        "--lines",
        str(max_lines),
        "--json",
    ]

    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise RailwayLogsError("Could not start Railway CLI.") from exc

    try:
        stdout, _stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=RAILWAY_LOGS_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.wait()
        raise RailwayLogsError("Railway CLI timed out.") from exc

    if process.returncode:
        raise RailwayLogsError(
            f"Railway CLI exited with status {process.returncode}."
        )

    records = _parse_cli_output(stdout)
    possibly_truncated = len(records) >= max_lines
    if len(records) > max_lines:
        records = records[-max_lines:]

    generated_at = datetime.now(timezone.utc).isoformat().replace(
        "+00:00",
        "Z",
    )
    document = {
        "source": "Railway CLI runtime logs",
        "period": period,
        "period_label": period_settings["label"],
        "since": since,
        "generated_at": generated_at,
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

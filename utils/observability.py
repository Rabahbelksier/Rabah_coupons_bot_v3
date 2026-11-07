"""Console-only diagnostics. Never change Telegram replies or database reports."""

import contextvars
import functools
import inspect
import json
import logging
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import urlsplit


_trace = contextvars.ContextVar("diagnostic_trace", default=None)
_stage = contextvars.ContextVar("diagnostic_stage", default=None)
_diagnostics = logging.getLogger("bot.diagnostics")


def redact(value):
    """Remove common credentials and complete URL paths/query strings."""
    text = str(value)
    text = re.sub(
        r"(?i)\b(?:https?|postgres(?:ql)?)://[^\s<>\"']+",
        lambda match: _safe_url(match.group()),
        text,
    )
    text = re.sub(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b", "[REDACTED]", text)
    text = re.sub(
        r"(?i)\bBearer\s+\S+", "Bearer [REDACTED]", text,
    )
    text = re.sub(
        r"""(?ix)(["']?(?:[a-z0-9]+[_-])*(?:api[_-]?key|access[_-]?token|token|password|
        secret|authorization|session_string|access_code)["']?
        \s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s,;}\]]+)""",
        r"\1[REDACTED]",
        text,
    )
    return text


def _safe_url(value):
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower().startswith("postgres"):
            return "[DATABASE_URL_REDACTED]"
        return f"{parsed.scheme}://{parsed.hostname or '[host]'}/[REDACTED]"
    except Exception:
        return "[URL_REDACTED]"


class DiagnosticFormatter(logging.Formatter):
    """One JSON line per event, including escaped multiline tracebacks."""

    def format(self, record):
        try:
            payload = {
                "timestamp": datetime.fromtimestamp(
                    record.created, timezone.utc
                ).isoformat(),
                "level": record.levelname,
                "logger": record.name,
                "message": redact(record.getMessage()),
                "source": f"{record.filename}:{record.lineno}",
            }
            trace_id = _trace.get()
            stage = getattr(record, "diagnostic_stage", None) or _stage.get()
            if trace_id:
                payload["trace_id"] = trace_id
            if stage:
                payload["stage"] = stage
            for field in ("event", "duration_ms", "error_type", "provider",
                          "http_status", "attempt", "retryable"):
                value = getattr(record, f"diagnostic_{field}", None)
                if value is not None:
                    payload[field] = redact(value) if isinstance(value, str) else value
            if record.exc_info:
                payload["error_type"] = record.exc_info[0].__name__
                payload["traceback"] = redact(self.formatException(record.exc_info))
            elif record.levelno >= logging.ERROR and sys.exc_info()[0] is not None:
                # Older call sites log only str(error). Enrich console output
                # without mutating LogRecord or the database's existing format.
                exception = sys.exc_info()
                payload["error_type"] = exception[0].__name__
                payload["traceback"] = redact(self.formatException(exception))
            return json.dumps(payload, ensure_ascii=False)
        except Exception:
            # A malformed log argument must not interrupt a bot operation.
            return '{"level":"ERROR","message":"Diagnostic formatting failed"}'


class SafeConsoleHandler(logging.StreamHandler):
    def handleError(self, record):
        # A closed/unavailable output stream is not a business-operation error.
        pass


def configure_console_logging():
    """Replace console formatting only; leave database report handlers intact."""
    root = logging.getLogger()
    formatter = DiagnosticFormatter()
    console_handlers = [
        handler for handler in root.handlers
        if type(handler) is logging.StreamHandler
        or isinstance(handler, SafeConsoleHandler)
    ]
    if not console_handlers:
        handler = SafeConsoleHandler(sys.stdout)
        root.addHandler(handler)
        console_handlers = [handler]
    for handler in console_handlers:
        handler.setFormatter(formatter)
    root.setLevel(logging.INFO)
    # Diagnostic lifecycle events must never enter the admin's DB error feed.
    if not any(isinstance(h, SafeConsoleHandler) for h in _diagnostics.handlers):
        handler = SafeConsoleHandler(sys.stdout)
        handler.setFormatter(formatter)
        _diagnostics.addHandler(handler)
    _diagnostics.setLevel(logging.INFO)
    _diagnostics.propagate = False


def _emit(event, stage, started, error=None):
    try:
        _diagnostics.log(
            logging.ERROR if error else logging.INFO,
            "%s: %s", stage, event,
            extra={
                "diagnostic_event": event,
                "diagnostic_stage": stage,
                "diagnostic_duration_ms": round((time.perf_counter() - started) * 1000, 1),
                "diagnostic_error_type": type(error).__name__ if error else None,
            },
            exc_info=(type(error), error, error.__traceback__) if error else None,
        )
    except Exception:
        pass


def remote_failure(provider, attempt, *, http_status=None, retryable=False,
                   error=None):
    """Record remote failure metadata only, never payloads or request arguments."""
    try:
        _diagnostics.warning(
            "External service request failed",
            extra={
                "diagnostic_event": "remote_request_failed",
                "diagnostic_provider": provider,
                "diagnostic_http_status": http_status,
                "diagnostic_attempt": attempt,
                "diagnostic_retryable": retryable,
            },
            exc_info=(type(error), error, error.__traceback__) if error else None,
        )
    except Exception:
        pass


def observed(stage, *, new_trace=False):
    """Observe call boundaries without changing arguments, results or exceptions.

    `completed` means the function returned, not that every downstream service
    succeeded. Existing application warning/error records retain that distinction.
    Arguments and return values are deliberately never logged.
    """
    def decorate(function):
        def begin():
            trace_token = _trace.set(
                uuid.uuid4().hex if new_trace or not _trace.get() else _trace.get()
            )
            stage_token = _stage.set(stage)
            started = time.perf_counter()
            _emit("started", stage, started)
            return trace_token, stage_token, started

        def finish(tokens):
            _stage.reset(tokens[1])
            _trace.reset(tokens[0])

        if inspect.iscoroutinefunction(function):
            @functools.wraps(function)
            async def asynchronous(*args, **kwargs):
                tokens = begin()
                try:
                    result = await function(*args, **kwargs)
                except BaseException as error:
                    _emit("interrupted" if not isinstance(error, Exception) else "failed",
                          stage, tokens[2], error)
                    raise
                else:
                    _emit("completed", stage, tokens[2])
                    return result
                finally:
                    finish(tokens)
            return asynchronous

        @functools.wraps(function)
        def synchronous(*args, **kwargs):
            tokens = begin()
            try:
                result = function(*args, **kwargs)
            except BaseException as error:
                _emit("interrupted" if not isinstance(error, Exception) else "failed",
                      stage, tokens[2], error)
                raise
            else:
                _emit("completed", stage, tokens[2])
                return result
            finally:
                finish(tokens)
        return synchronous
    return decorate

"""Safe console diagnostics. Never reads secret values from the environment."""

import copy
import logging
import os
import re
import sys
import threading
import time
from urllib.parse import urlsplit, urlunsplit

from utils.log_context import current_log_context, log_context


logger = logging.getLogger(__name__)
_URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_DATABASE_URL = re.compile(
    r"(?:postgres(?:ql)?|mysql|redis(?:s)?)://[^\s<>\"']+", re.IGNORECASE
)
_TELEGRAM_TOKEN = re.compile(r"(?:bot)?\d{5,}[:%]+(?:3[Aa])?[\w-]{15,}")
_CREDENTIAL = re.compile(
    r"(?P<key>[\"']?\b(?:[\w-]*(?:password|passwd|secret|token|api[_-]?key|"
    r"api[_-]?hash|session[_-]?string|authorization|cookie)[\w-]*|"
    r"app_key|sign|_?fbp|_?fbc|user[_-]agent|client_ip|first_name|last_name|"
    r"message_text|prompt)[\"']?\s*[:=]\s*)"
    r"(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|\[[^\]\r\n]*\]|[^\s,;&}\]\r\n]+)",
    re.IGNORECASE,
)
_AUTHORIZATION = re.compile(r"\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+", re.I)
_PROVIDER_KEY = re.compile(
    r"\b(?:sk-(?:or-v1-|proj-|ant-api\d+-)?[A-Za-z0-9_-]{12,}|"
    r"AIza[A-Za-z0-9_-]{20,}|gsk_[A-Za-z0-9]{16,}|EAA[A-Za-z0-9]{20,})"
)


def redact_log_text(value):
    """Remove credentials and URL query payloads, including in tracebacks."""
    text = str(value)
    text = _DATABASE_URL.sub("[REDACTED_DATABASE_URL]", text)
    text = _AUTHORIZATION.sub("[REDACTED_AUTHORIZATION]", text)
    text = _PROVIDER_KEY.sub("[REDACTED_PROVIDER_KEY]", text)
    text = _CREDENTIAL.sub(lambda match: match["key"] + "[REDACTED]", text)
    # PostgreSQL can echo actual row values or interpolated SQL in diagnostics.
    text = re.sub(
        r"(?m)^(?:DETAIL|CONTEXT|QUERY|LINE \d+):[^\r\n]*",
        "[REDACTED_DATABASE_DETAIL]",
        text,
    )

    def safe_url(match):
        try:
            url = urlsplit(match[0])
            host = url.netloc.rsplit("@", 1)[-1]
            return urlunsplit((
                url.scheme,
                host,
                url.path,
                "[REDACTED]" if url.query else "",
                "[REDACTED]" if url.fragment else "",
            ))
        except ValueError:
            return "[REDACTED_URL]"

    text = _URL.sub(safe_url, text)
    return _TELEGRAM_TOKEN.sub("[REDACTED_TELEGRAM_TOKEN]", text)


class SafeLogFormatter(logging.Formatter):
    converter = time.gmtime

    def __init__(self):
        super().__init__(
            "%(asctime)s.%(msecs)03dZ %(levelname)s %(name)s "
            "%(filename)s:%(lineno)d %(funcName)s "
            "pid=%(process)d thread=%(threadName)s "
            "trace=%(log_trace_id)s operation=%(log_operation)s "
            "update=%(log_update_id)s product=%(log_product_id)s | %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )

    def format(self, record):
        # Do not mutate the original: other handlers may share this record.
        safe_record = copy.copy(record)
        context = current_log_context()
        for name, field in (
            ("log_trace_id", "trace_id"),
            ("log_operation", "operation"),
            ("log_update_id", "update_id"),
            ("log_product_id", "product_id"),
        ):
            if not hasattr(safe_record, name):
                setattr(safe_record, name, context.get(field, "-"))
        safe_record.exc_text = None
        try:
            return redact_log_text(super().format(safe_record))
        except Exception:
            # Never fall back to logging's raw record dump (it may hold secrets).
            return "ERROR observability | Unable to format log record safely"


def _install_record_factory():
    previous = logging.getLogRecordFactory()
    if getattr(previous, "_bot_observability", False) is True:
        return

    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        context = current_log_context()
        record.log_trace_id = context.get("trace_id", "-")
        record.log_operation = context.get("operation", "-")
        record.log_update_id = context.get("update_id", "-")
        record.log_product_id = context.get("product_id", "-")
        # Legacy warning/error calls inside except blocks retain their wording,
        # but now include the original exception and source traceback.
        if record.levelno >= logging.WARNING and not record.exc_info:
            exception = sys.exc_info()
            if exception[0] is not None:
                record.exc_info = exception
        return record

    factory._bot_observability = True
    logging.setLogRecordFactory(factory)


def configure_logging():
    """Configure existing destinations without adding duplicate streams."""
    level_name = os.getenv("BOT_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    if not isinstance(level, int):
        level = logging.INFO
    logging.basicConfig(level=level)
    _install_record_factory()
    formatter = SafeLogFormatter()
    root = logging.getLogger()
    root.setLevel(level)
    for handler in root.handlers:
        handler.setFormatter(formatter)
    # Hosting/WSGI loggers can own non-propagating handlers.
    for name in ("gunicorn.error", "gunicorn.access", "werkzeug", "flask.app"):
        for handler in logging.getLogger(name).handlers:
            handler.setFormatter(formatter)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    if getattr(sys.excepthook, "_bot_observability", False) is not True:
        previous = sys.excepthook

        def unhandled(kind, value, traceback):
            if issubclass(kind, KeyboardInterrupt):
                previous(kind, value, traceback)
                return
            logger.critical(
                "Unhandled process exception", exc_info=(kind, value, traceback)
            )

        unhandled._bot_observability = True
        sys.excepthook = unhandled

    if getattr(threading.excepthook, "_bot_observability", False) is not True:
        def thread_unhandled(args):
            if args.exc_type is SystemExit:
                return
            logger.critical(
                "Unhandled background thread exception thread=%s",
                args.thread.name if args.thread else "-",
                exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
            )

        thread_unhandled._bot_observability = True
        threading.excepthook = thread_unhandled


def log_asyncio_exception(loop, context):
    """Observe unawaited task failures without dumping task locals/payloads."""
    exception = context.get("exception")
    logger.error(
        "Unhandled asyncio task failure kind=%s",
        type(exception).__name__ if exception else "AsyncioDiagnostic",
        exc_info=(
            (type(exception), exception, exception.__traceback__)
            if exception else None
        ),
    )


async def telegram_error_handler(update, context):
    """PTB handles callback failures internally; observe without replying."""
    exception = context.error
    logger.error(
        "Telegram handler failed",
        exc_info=(type(exception), exception, exception.__traceback__),
    )


async def process_telegram_update(application, update):
    with log_context("telegram.update", update_id=update.update_id):
        started = time.monotonic()
        logger.info("Telegram update started")
        try:
            return await application.process_update(update)
        finally:
            # PTB may consume a callback exception after invoking its error
            # handler, so this deliberately does not claim delivery success.
            logger.info(
                "Telegram update processing finished elapsed_ms=%.1f",
                (time.monotonic() - started) * 1000,
            )


def install_http_logging(app):
    """Log endpoint names, never raw paths (the webhook path is a secret)."""
    from flask import g, request

    @app.before_request
    def start_request_log():
        g.bot_log_scope = log_context(
            "http." + (request.endpoint or "unmatched"), new_trace=True
        )
        g.bot_log_scope.__enter__()
        g.bot_log_started = time.monotonic()

    @app.after_request
    def finish_request_log(response):
        logger.info(
            "HTTP request finished method=%s endpoint=%s status=%s elapsed_ms=%.1f",
            request.method,
            request.endpoint or "unmatched",
            response.status_code,
            (time.monotonic() - g.bot_log_started) * 1000,
        )
        return response

    @app.teardown_request
    def release_request_log(exception):
        scope = g.pop("bot_log_scope", None)
        if scope is not None:
            scope.__exit__(None, None, None)

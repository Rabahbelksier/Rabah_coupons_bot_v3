"""Request-local diagnostics, independent of Telegram, Flask, or the database."""

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import logging
import time
import uuid


_context = ContextVar("bot_log_context", default=None)
logger = logging.getLogger(__name__)


def current_log_context():
    return dict(_context.get() or {})


def update_log_context(**fields):
    """Enrich only the current task; its enclosing scope restores the parent."""
    context = current_log_context()
    context.update(fields)
    _context.set(context)


@contextmanager
def log_context(operation, *, new_trace=False, **fields):
    context = current_log_context()
    if new_trace or not context.get("trace_id"):
        context["trace_id"] = uuid.uuid4().hex
    context.update(operation=operation, **fields)
    token = _context.set(context)
    try:
        yield context
    finally:
        _context.reset(token)


def traced_operation(operation, *, new_trace=False):
    """Observe a boundary without changing its return value or exceptions."""
    def decorate(function):
        if asyncio.iscoroutinefunction(function):
            @wraps(function)
            async def async_wrapper(*args, **kwargs):
                with log_context(operation, new_trace=new_trace):
                    started = time.monotonic()
                    logger.debug("Operation started")
                    try:
                        return await function(*args, **kwargs)
                    except asyncio.CancelledError:
                        logger.debug("Operation cancelled")
                        raise
                    except Exception:
                        logger.exception("Operation failed")
                        raise
                    finally:
                        logger.debug(
                            "Operation finished elapsed_ms=%.1f",
                            (time.monotonic() - started) * 1000,
                        )
            return async_wrapper

        @wraps(function)
        def sync_wrapper(*args, **kwargs):
            with log_context(operation, new_trace=new_trace):
                started = time.monotonic()
                logger.debug("Operation started")
                try:
                    return function(*args, **kwargs)
                except Exception:
                    logger.exception("Operation failed")
                    raise
                finally:
                    logger.debug(
                        "Operation finished elapsed_ms=%.1f",
                        (time.monotonic() - started) * 1000,
                    )
        return sync_wrapper
    return decorate

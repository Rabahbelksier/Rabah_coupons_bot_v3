import logging
import threading

from core.db import add_error_log
from utils.observability import SafeLogFormatter


class DatabaseErrorHandler(logging.Handler):
    """Copy server errors to the database for the admin reports tool."""

    def __init__(self):
        super().__init__()
        self._emitting = threading.local()

    def emit(self, record):
        if record.levelno < logging.ERROR or getattr(self._emitting, "active", False):
            return
        try:
            self._emitting.active = True
            add_error_log(
                record.levelname,
                record.name,
                self.format(record),
            )
        except Exception:
            # Logging must never be the reason the bot request fails.
            logging.getLogger(__name__).warning(
                "Could not persist error log; original error remains in console",
                exc_info=True,
            )
        finally:
            self._emitting.active = False


def install_error_log_handler():
    root_logger = logging.getLogger()
    if any(isinstance(handler, DatabaseErrorHandler) for handler in root_logger.handlers):
        return
    handler = DatabaseErrorHandler()
    handler.setFormatter(SafeLogFormatter())
    root_logger.addHandler(handler)
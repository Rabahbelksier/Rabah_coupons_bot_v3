import logging

from core.db import add_error_log


class DatabaseErrorHandler(logging.Handler):
    """Copy server errors to the database for the admin reports tool."""

    def emit(self, record):
        if record.levelno < logging.ERROR:
            return
        try:
            add_error_log(
                record.levelname,
                record.name,
                self.format(record),
            )
        except Exception:
            # Logging must never be the reason the bot request fails.
            pass


def install_error_log_handler():
    root_logger = logging.getLogger()
    if any(isinstance(handler, DatabaseErrorHandler) for handler in root_logger.handlers):
        return
    handler = DatabaseErrorHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root_logger.addHandler(handler)
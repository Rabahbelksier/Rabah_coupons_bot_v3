"""Offline regression coverage: diagnostics must not change application results."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
import io
import logging
import os
import sys
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

for name in ("APP_KEY", "APP_SECRET", "TRACKING_ID", "TELEGRAM_TOKEN"):
    os.environ.setdefault(name, "test")

from utils.log_context import (
    current_log_context,
    log_context,
    traced_operation,
    update_log_context,
)
from utils.observability import (
    SafeLogFormatter,
    _install_record_factory,
    configure_logging,
    install_http_logging,
    log_asyncio_exception,
    process_telegram_update,
    redact_log_text,
    telegram_error_handler,
)


class LoggingTests(unittest.TestCase):
    def setUp(self):
        self.previous_factory = logging.getLogRecordFactory()
        _install_record_factory()
        self.output = io.StringIO()
        self.handler = logging.StreamHandler(self.output)
        self.handler.setFormatter(SafeLogFormatter())
        self.logger = logging.getLogger("test.observability")
        self.logger.propagate = False
        self.logger.setLevel(logging.DEBUG)
        self.logger.addHandler(self.handler)

    def tearDown(self):
        logging.setLogRecordFactory(self.previous_factory)
        self.logger.removeHandler(self.handler)

    def test_existing_error_call_gains_traceback_context_and_source(self):
        with log_context("test.operation", update_id=42) as context:
            try:
                raise ValueError("synthetic failure")
            except ValueError as error:
                self.logger.error("Legacy error: %s", error)
        output = self.output.getvalue()
        self.assertIn("Traceback (most recent call last)", output)
        self.assertIn("ValueError: synthetic failure", output)
        self.assertIn("test_observability.py:", output)
        self.assertIn("operation=test.operation", output)
        self.assertIn("update=42", output)
        self.assertIn("trace=" + context["trace_id"], output)
        self.assertEqual(current_log_context(), {})

    def test_redacts_credentials_urls_and_exception_chain(self):
        telegram_token = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
        text = (
            f"https://api.telegram.org/bot{telegram_token}/sendMessage "
            f"POST /{telegram_token} "
            "postgresql://dbuser:dbpassword@dbhost/database "
            "https://example.test/api?access_token=querysecret&text=private-message "
            "Authorization: Bearer headersecret "
            "'api_key': 'apikeysecret' password=passsecret "
            "TELEGRAM_SESSION_STRING=sessionsecret "
            "https://login:httppassword@example.test/path "
            "'_fbp': 'browsersecret'"
        )
        with self.subTest("direct redaction"):
            output = redact_log_text(text)
            for secret in (
                telegram_token, "dbpassword", "querysecret", "private-message",
                "headersecret", "apikeysecret", "passsecret", "sessionsecret",
                "httppassword", "browsersecret",
            ):
                self.assertNotIn(secret, output)
        try:
            try:
                raise ValueError("access_token=innersecret")
            except ValueError as original:
                raise RuntimeError("password=outersecret") from original
        except RuntimeError:
            self.logger.exception("Failed password=messagesecret")
        output = self.output.getvalue()
        for secret in ("innersecret", "outersecret", "messagesecret"):
            self.assertNotIn(secret, output)
        self.assertIn("ValueError", output)
        self.assertIn("RuntimeError", output)

    def test_formatting_does_not_modify_record_or_leak_bad_arguments(self):
        record = logging.LogRecord(
            "test", logging.ERROR, __file__, 1, "password=%s", ("hidden",), None
        )
        output = SafeLogFormatter().format(record)
        self.assertNotIn("hidden", output)
        self.assertEqual(record.msg, "password=%s")
        self.assertEqual(record.args, ("hidden",))
        self.assertIsNone(record.exc_text)
        record.msg = "%s %s"
        output = SafeLogFormatter().format(record)
        self.assertNotIn("hidden", output)
        self.assertIn("Unable to format", output)

    def test_provider_key_shapes_and_database_details_are_redacted(self):
        keys = [
            "sk-proj-" + "A" * 30,
            "sk-or-v1-" + "B" * 30,
            "AIza" + "C" * 30,
            "gsk_" + "D" * 30,
            "EAA" + "E" * 30,
        ]
        text = "Incorrect API key provided: " + " ".join(keys)
        text += "\nDETAIL: Failing row contains (private-row-data)\n"
        text += "ValueError: still useful"
        output = redact_log_text(text)
        for key in keys:
            self.assertNotIn(key, output)
        self.assertNotIn("private-row-data", output)
        self.assertIn("ValueError: still useful", output)

    def test_factory_is_idempotent(self):
        before = logging.getLogRecordFactory()
        _install_record_factory()
        self.assertIs(before, logging.getLogRecordFactory())

    def test_product_context_does_not_leak_to_parent_or_next_request(self):
        with log_context("outer", update_id=12) as outer:
            with log_context("product"):
                update_log_context(product_id="12345")
                self.logger.info("Product diagnostic")
                self.assertEqual(current_log_context()["product_id"], "12345")
            self.assertEqual(current_log_context(), outer)
        self.assertIn("product=12345", self.output.getvalue())
        self.assertEqual(current_log_context(), {})

    def test_database_handler_redacts_and_does_not_recurse(self):
        from utils.error_logging import DatabaseErrorHandler

        handler = DatabaseErrorHandler()
        handler.setFormatter(SafeLogFormatter())
        self.logger.addHandler(handler)
        try:
            with patch("utils.error_logging.add_error_log") as save:
                def nested_error(*args):
                    self.logger.error("Nested persistence failure")
                save.side_effect = nested_error
                self.logger.error("password=database-secret")
                self.assertEqual(save.call_count, 1)
                self.assertNotIn("database-secret", save.call_args.args[2])
            with patch(
                "utils.error_logging.add_error_log",
                side_effect=RuntimeError("unavailable"),
            ):
                self.logger.error("Original error survives database outage")
            self.assertIn("Original error survives", self.output.getvalue())
        finally:
            self.logger.removeHandler(handler)

    def test_sync_operation_preserves_result_and_exception_identity(self):
        @traced_operation("sync.test")
        def run(value):
            self.assertEqual(current_log_context()["operation"], "sync.test")
            if isinstance(value, Exception):
                raise value
            return value

        value = object()
        self.assertIs(run(value), value)
        error = ValueError("same object")
        with self.assertRaises(ValueError) as raised:
            run(error)
        self.assertIs(raised.exception, error)
        self.assertEqual(current_log_context(), {})

    def test_configure_preserves_destinations_and_installs_safe_crash_hooks(self):
        root = logging.getLogger()
        loggers = [
            root, *[
                logging.getLogger(name) for name in
                ("gunicorn.error", "gunicorn.access", "werkzeug", "flask.app")
            ]
        ]
        originals = [(handler, handler.formatter)
                     for log in loggers for handler in log.handlers]
        original_handlers = list(root.handlers)
        original_level = root.level
        original_httpx_level = logging.getLogger("httpx").level
        try:
            with patch.object(sys, "excepthook"), patch.object(threading, "excepthook"):
                configure_logging()
                count = len(root.handlers)
                configure_logging()
                self.assertEqual(len(root.handlers), count)
                self.assertTrue(all(
                    isinstance(handler.formatter, SafeLogFormatter)
                    for handler in root.handlers
                ))
                with patch("utils.observability.logger") as log:
                    error = RuntimeError("synthetic")
                    sys.excepthook(RuntimeError, error, None)
                    threading.excepthook(SimpleNamespace(
                        exc_type=RuntimeError, exc_value=error,
                        exc_traceback=None, thread=threading.current_thread(),
                    ))
                    self.assertEqual(log.critical.call_count, 2)
        finally:
            root.handlers[:] = original_handlers
            root.setLevel(original_level)
            logging.getLogger("httpx").setLevel(original_httpx_level)
            for handler, formatter in originals:
                handler.setFormatter(formatter)


class AsyncDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_contexts_and_threads_are_isolated(self):
        async def run(number):
            with log_context("request", new_trace=True, update_id=number) as ctx:
                await asyncio.sleep(0)
                inherited = await asyncio.to_thread(current_log_context)
                with ThreadPoolExecutor(max_workers=1) as executor:
                    pooled = executor.submit(copy_context().run, current_log_context).result()
                self.assertEqual(inherited, ctx)
                self.assertEqual(pooled, ctx)
                return ctx
        first, second = await asyncio.gather(run(1), run(2))
        self.assertNotEqual(first["trace_id"], second["trace_id"])
        self.assertEqual(first["update_id"], 1)
        self.assertEqual(second["update_id"], 2)
        self.assertEqual(current_log_context(), {})

    async def test_async_operation_preserves_result_error_and_cancellation(self):
        @traced_operation("async.test")
        async def run(value):
            self.assertEqual(current_log_context()["operation"], "async.test")
            if isinstance(value, BaseException):
                raise value
            return value
        self.assertEqual(await run(9), 9)
        error = RuntimeError("same error")
        with self.assertRaises(RuntimeError) as raised:
            await run(error)
        self.assertIs(raised.exception, error)
        with self.assertRaises(asyncio.CancelledError):
            await run(asyncio.CancelledError())
        self.assertEqual(current_log_context(), {})

    async def test_telegram_errors_only_log_and_never_send_messages(self):
        error = ValueError("failed callback")
        context = SimpleNamespace(error=error, bot=Mock())
        with patch("utils.observability.logger") as log:
            await telegram_error_handler(SimpleNamespace(), context)
            self.assertIs(log.error.call_args.kwargs["exc_info"][1], error)
        self.assertEqual(context.bot.mock_calls, [])

    async def test_update_context_and_result_survive_submission_boundary(self):
        async def process(update):
            context = current_log_context()
            self.assertEqual(context["update_id"], 54)
            self.assertEqual(context["operation"], "telegram.update")
            self.assertTrue(context["trace_id"])
            return "unchanged"
        application = SimpleNamespace(process_update=AsyncMock(side_effect=process))
        result = await process_telegram_update(
            application, SimpleNamespace(update_id=54)
        )
        self.assertEqual(result, "unchanged")
        self.assertEqual(current_log_context(), {})

    async def test_asyncio_handler_does_not_dump_task_or_message_payload(self):
        error = ValueError("failed task")
        with patch("utils.observability.logger") as log:
            log_asyncio_exception(
                asyncio.get_running_loop(),
                {"exception": error, "message": "private payload", "task": "private task"},
            )
        self.assertNotIn("private", str(log.error.call_args.args))
        self.assertIs(log.error.call_args.kwargs["exc_info"][1], error)


class HttpDiagnosticsTests(unittest.TestCase):
    def test_http_response_unchanged_and_secret_webhook_path_not_logged(self):
        from flask import Flask

        app = Flask("offline_logging_test")
        app.testing = True
        install_http_logging(app)
        token = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"

        @app.post("/" + token)
        def synthetic_webhook():
            self.assertEqual(current_log_context()["operation"], "http.synthetic_webhook")
            return "unchanged", 202

        with patch("utils.observability.logger") as log:
            response = app.test_client().post("/" + token, json={"text": "private"})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data, b"unchanged")
        self.assertNotIn(token, str(log.info.call_args))
        self.assertNotIn("private", str(log.info.call_args))
        self.assertEqual(current_log_context(), {})

    def test_failed_http_request_releases_context_and_keeps_existing_500(self):
        from flask import Flask

        app = Flask("offline_logging_failure")
        app.testing = False
        install_http_logging(app)

        @app.get("/broken")
        def broken():
            raise ValueError("synthetic failure")

        with patch("utils.observability.logger"), patch.object(app.logger, "error"):
            response = app.test_client().get("/broken")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(current_log_context(), {})


class ApiLoggingCompatibilityTests(unittest.TestCase):
    def test_retry_count_delay_and_result_unchanged(self):
        import requests
        from core.api import send_api_request_with_retry

        params = {"method": "synthetic.test", "sign": "must-not-be-logged"}
        response = Mock(status_code=200)
        response.json.return_value = {"unchanged": True}
        with (
            patch("core.api._http_session.post", side_effect=[
                requests.exceptions.Timeout("synthetic timeout"), response,
            ]) as post,
            patch("core.api.time.sleep") as sleep,
        ):
            result = send_api_request_with_retry(params)
        self.assertEqual(result, {"unchanged": True})
        self.assertEqual(post.call_count, 2)
        self.assertIs(post.call_args.kwargs["data"], params)
        sleep.assert_called_once_with(0.5)

    def test_exhaustion_retains_existing_error_response(self):
        import requests
        from core.api import send_api_request_with_retry

        with (
            patch("core.api._http_session.post", side_effect=requests.exceptions.Timeout),
            patch("core.api.time.sleep"),
        ):
            result = send_api_request_with_retry({"method": "synthetic.test"})
        self.assertEqual(result, {
            "error_response": {
                "code": "MaxRetriesExceeded",
                "msg": "فشلت جميع المحاولات",
            }
        })

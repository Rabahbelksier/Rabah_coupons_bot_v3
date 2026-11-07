import asyncio
import io
import json
import logging
import unittest
from unittest.mock import patch

from utils.observability import (
    DiagnosticFormatter, SafeConsoleHandler, _diagnostics, _stage, _trace,
    configure_console_logging, observed, redact,
    remote_failure,
)


class ObservabilityTests(unittest.TestCase):
    def setUp(self):
        self.output = io.StringIO()
        self.handler = SafeConsoleHandler(self.output)
        self.handler.setFormatter(DiagnosticFormatter())
        self.old_handlers = _diagnostics.handlers[:]
        self.old_level = _diagnostics.level
        self.old_propagate = _diagnostics.propagate
        _diagnostics.handlers = [self.handler]
        _diagnostics.setLevel(logging.INFO)
        _diagnostics.propagate = False

    def tearDown(self):
        _diagnostics.handlers = self.old_handlers
        _diagnostics.setLevel(self.old_level)
        _diagnostics.propagate = self.old_propagate

    def records(self):
        return [json.loads(line) for line in self.output.getvalue().splitlines()]

    def test_sync_result_arguments_and_context_unchanged(self):
        sentinel = object()

        @observed("test.sync")
        def function(value, *, flag):
            self.assertIs(value, sentinel)
            self.assertTrue(flag)
            return value

        self.assertIs(function(sentinel, flag=True), sentinel)
        records = self.records()
        self.assertEqual([r["event"] for r in records], ["started", "completed"])
        self.assertEqual(records[0]["trace_id"], records[1]["trace_id"])
        self.assertIsNone(_trace.get())
        self.assertIsNone(_stage.get())

    def test_exception_identity_and_traceback_preserved(self):
        error = ValueError("test failure")

        @observed("test.failure")
        def function():
            raise error

        with self.assertRaises(ValueError) as caught:
            function()
        self.assertIs(caught.exception, error)
        record = self.records()[-1]
        self.assertEqual(record["event"], "failed")
        self.assertEqual(record["error_type"], "ValueError")
        self.assertIn("raise error", record["traceback"])

    def test_logging_failure_cannot_change_result_or_exception(self):
        @observed("test.safe")
        def function(fail=False):
            if fail:
                raise ValueError("unchanged")
            return 42

        with patch.object(_diagnostics, "log", side_effect=RuntimeError("broken log")):
            self.assertEqual(function(), 42)
            with self.assertRaisesRegex(ValueError, "unchanged"):
                function(True)

    def test_async_thread_context_and_cancellation(self):
        @observed("test.thread")
        def worker():
            return _trace.get()

        @observed("test.async")
        async def function():
            trace = _trace.get()
            self.assertEqual(await asyncio.to_thread(worker), trace)
            return trace

        async def run():
            first, second = await asyncio.gather(function(), function())
            self.assertNotEqual(first, second)
            self.assertIsNone(_trace.get())

            @observed("test.cancel")
            async def cancelled():
                raise asyncio.CancelledError()

            with self.assertRaises(asyncio.CancelledError):
                await cancelled()
            self.assertIsNone(_trace.get())
        asyncio.run(run())
        self.assertEqual(self.records()[-1]["event"], "interrupted")

    def test_redaction_covers_urls_tokens_credentials_and_bearer(self):
        raw = (
            "https://example.com/private?key=private-value "
            "postgresql://user:password@host/database "
            "/123456789:abcdefghijklmnopqrstuvwxyz_123456789 "
            'api_key="secret-value" password=private-value APP_SECRET=app-value '
            "Authorization: Bearer opaque-value"
        )
        safe = redact(raw)
        for private in ("private-value", "secret-value", "opaque-value",
                        "abcdefghijklmnopqrstuvwxyz", "user:password", "/private", "app-value"):
            self.assertNotIn(private, safe)

    def test_console_exception_enrichment_does_not_mutate_record(self):
        try:
            raise RuntimeError("password=hidden-value")
        except RuntimeError:
            record = logging.LogRecord("test", logging.ERROR, __file__, 1,
                                       "operation failed", (), None)
            formatted = json.loads(DiagnosticFormatter().format(record))
            self.assertIn("traceback", formatted)
            self.assertNotIn("hidden-value", formatted["traceback"])
            self.assertIsNone(record.exc_info)
            self.assertIsNone(record.exc_text)
            self.assertEqual(record.msg, "operation failed")

    def test_remote_failure_is_safe_and_structured(self):
        remote_failure("AliExpress", 2, http_status=429, retryable=True)
        record = self.records()[-1]
        self.assertEqual(record["http_status"], 429)
        self.assertEqual(record["attempt"], 2)
        self.assertTrue(record["retryable"])
        with patch.object(_diagnostics, "warning", side_effect=RuntimeError()):
            remote_failure("AliExpress", 1)

    def test_configuration_leaves_database_handler_unchanged(self):
        root = logging.getLogger()
        previous_handlers, previous_level = root.handlers[:], root.level

        class DatabaseStandIn(logging.Handler):
            def emit(self, record):
                raise AssertionError("Diagnostics reached database")

        database = DatabaseStandIn()
        old_formatter = logging.Formatter("existing %(message)s")
        database.setFormatter(old_formatter)
        try:
            root.handlers = [database]
            configure_console_logging()
            configure_console_logging()
            self.assertIs(database.formatter, old_formatter)
            self.assertEqual(sum(isinstance(h, SafeConsoleHandler)
                                 for h in root.handlers), 1)

            @observed("test.console_only")
            def fail():
                raise ValueError("expected")
            with self.assertRaises(ValueError):
                fail()
        finally:
            root.handlers = previous_handlers
            root.setLevel(previous_level)

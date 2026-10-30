import asyncio
import logging
import threading
import time
import unittest

from services.telegram_runtime import TelegramRuntime


class FakeApplication:
    def __init__(self):
        self._initialized = False
        self.running = False
        self.initialize_calls = 0
        self.start_calls = 0
        self.stop_calls = 0
        self.shutdown_calls = 0

    async def initialize(self):
        self.initialize_calls += 1
        self._initialized = True

    async def start(self):
        self.start_calls += 1
        self.running = True

    async def stop(self):
        self.stop_calls += 1
        self.running = False

    async def shutdown(self):
        self.shutdown_calls += 1
        self._initialized = False


class TelegramRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.applications = []
        self.factory_calls = []
        self.ready_applications = []
        self.logger = logging.getLogger("test.telegram_runtime")

        def factory(is_recovery):
            self.factory_calls.append(is_recovery)
            application = FakeApplication()
            self.applications.append(application)
            return application

        self.runtime = TelegramRuntime(
            factory,
            self.ready_applications.append,
            self.logger,
            startup_timeout=2,
            health_check_interval=10,
        )

    def tearDown(self):
        self.runtime.stop()

    def test_concurrent_ensure_ready_initializes_once(self):
        errors = []
        barriers = threading.Barrier(4)

        def ensure():
            try:
                barriers.wait(timeout=2)
                self.runtime.ensure_ready()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=ensure) for _ in range(3)]
        for thread in threads:
            thread.start()
        barriers.wait(timeout=2)
        for thread in threads:
            thread.join(timeout=2)

        self.assertEqual(errors, [])
        self.assertEqual(self.factory_calls, [False])
        self.assertEqual(self.applications[0].initialize_calls, 1)
        self.assertEqual(self.ready_applications, [self.applications[0]])
        self.assertTrue(self.runtime.is_healthy())

    def test_stopped_event_loop_is_rebuilt(self):
        self.runtime.ensure_ready()
        old_application = self.runtime.application
        old_loop = self.runtime.loop
        old_loop.call_soon_threadsafe(old_loop.stop)

        deadline = time.monotonic() + 2
        while old_loop.is_running() and time.monotonic() < deadline:
            time.sleep(0.01)

        self.runtime.ensure_ready()

        self.assertEqual(self.factory_calls, [False, True])
        self.assertIsNot(self.runtime.application, old_application)
        self.assertTrue(self.runtime.is_healthy())
        self.assertEqual(len(self.ready_applications), 2)

    def test_submit_runs_on_supervised_loop(self):
        self.runtime.ensure_ready()

        async def value():
            return "ok"

        result = self.runtime.submit(value(), "test operation").result(timeout=2)

        self.assertEqual(result, "ok")


if __name__ == "__main__":
    unittest.main()
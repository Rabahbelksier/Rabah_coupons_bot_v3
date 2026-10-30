"""Supervise the asyncio runtime used by the Telegram webhook application."""

import asyncio
import threading


class TelegramRuntime:
    """Keep one Telegram application and its event loop available.

    The runtime deliberately knows nothing about bot handlers or business
    logic.  The application factory is called once at startup and again only
    after the event-loop thread has stopped unexpectedly.
    """

    def __init__(
        self,
        application_factory,
        on_ready,
        logger,
        *,
        startup_timeout=30,
        health_check_interval=10,
    ):
        self._application_factory = application_factory
        self._on_ready = on_ready
        self._logger = logger
        self._startup_timeout = startup_timeout
        self._health_check_interval = health_check_interval

        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._application = application_factory(False)
        self._loop = None
        self._loop_thread = None
        self._initialized = False
        self._supervisor_thread = None

    @property
    def application(self):
        with self._lock:
            return self._application

    @property
    def loop(self):
        with self._lock:
            return self._loop

    @property
    def initialized(self):
        with self._lock:
            return self._initialized

    def _is_healthy_locked(self):
        return bool(
            self._initialized
            and self._loop
            and self._loop.is_running()
            and self._loop_thread
            and self._loop_thread.is_alive()
        )

    def is_healthy(self):
        with self._lock:
            return self._is_healthy_locked()

    def _run_loop(self, loop):
        asyncio.set_event_loop(loop)
        try:
            loop.run_forever()
        except Exception:
            self._logger.exception("Telegram event loop stopped unexpectedly")
        finally:
            if not loop.is_closed():
                loop.close()

    async def _start_application(self, application):
        await application.initialize()
        await application.start()

    async def _stop_application(self, application):
        if application.running:
            await application.stop()
        if getattr(application, "_initialized", False):
            await application.shutdown()

    def _discard_failed_runtime_locked(self):
        loop = self._loop
        thread = self._loop_thread
        self._loop = None
        self._loop_thread = None
        self._initialized = False

        if loop and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        if thread and thread is not threading.current_thread():
            thread.join(timeout=1)

    def ensure_ready(self):
        with self._lock:
            if self._is_healthy_locked():
                return self._loop

            recovering = self._initialized
            if recovering:
                self._logger.warning(
                    "Telegram runtime is unhealthy; rebuilding the application"
                )
                self._application = self._application_factory(True)

            self._stop_event.clear()
            loop = asyncio.new_event_loop()
            thread = threading.Thread(
                target=self._run_loop,
                args=(loop,),
                name="telegram-event-loop",
                daemon=True,
            )
            self._loop = loop
            self._loop_thread = thread
            thread.start()

            try:
                future = asyncio.run_coroutine_threadsafe(
                    self._start_application(self._application),
                    loop,
                )
                future.result(timeout=self._startup_timeout)
            except Exception:
                self._logger.exception("Telegram application startup failed")
                self._discard_failed_runtime_locked()
                raise

            self._initialized = True
            try:
                self._on_ready(self._application)
            except Exception:
                self._logger.exception(
                    "Telegram runtime ready callback failed"
                )
            self._ensure_supervisor_locked()
            self._logger.info(
                "Telegram application ready%s",
                " after runtime recovery" if recovering else "",
            )
            return loop

    def _ensure_supervisor_locked(self):
        if (
            self._supervisor_thread
            and self._supervisor_thread.is_alive()
        ):
            return
        self._supervisor_thread = threading.Thread(
            target=self._supervise,
            name="telegram-runtime-supervisor",
            daemon=True,
        )
        self._supervisor_thread.start()

    def _supervise(self):
        while not self._stop_event.wait(self._health_check_interval):
            with self._lock:
                unhealthy = (
                    self._initialized
                    and not self._is_healthy_locked()
                )
            if not unhealthy:
                continue

            self._logger.error(
                "Telegram event-loop thread is no longer healthy; "
                "starting recovery"
            )
            try:
                self.ensure_ready()
            except Exception:
                self._logger.exception(
                    "Telegram runtime recovery attempt failed"
                )

    def submit(self, coroutine, operation):
        """Submit work to the live loop and log failures from the task."""
        loop = self.ensure_ready()
        try:
            future = asyncio.run_coroutine_threadsafe(coroutine, loop)
        except Exception:
            coroutine.close()
            raise
        future.add_done_callback(
            lambda completed: self._log_future_result(completed, operation)
        )
        return future

    def _log_future_result(self, future, operation):
        try:
            future.result()
        except Exception:
            self._logger.exception("%s failed", operation)

    def stop(self):
        self._stop_event.set()
        with self._lock:
            loop = self._loop
            thread = self._loop_thread
            application = self._application
            healthy = self._is_healthy_locked()

            if healthy:
                try:
                    future = asyncio.run_coroutine_threadsafe(
                        self._stop_application(application),
                        loop,
                    )
                    future.result(timeout=self._startup_timeout)
                except Exception:
                    self._logger.exception(
                        "Telegram application shutdown failed"
                    )
                finally:
                    if loop.is_running():
                        loop.call_soon_threadsafe(loop.stop)

            if thread and thread is not threading.current_thread():
                thread.join(timeout=self._startup_timeout)

            self._initialized = False
            self._loop = None
            self._loop_thread = None
import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from telethon.tl.types import PeerChannel

from core import db
from core.channel_processor import ChannelProcessResult
from config import CHANNEL_AI_BATCH_SIZE
from services.channel_monitor import ChannelMonitor, _channel_reference


class FakeTelegramClient:
    def __init__(self, authorized=True):
        self.authorized = authorized
        self.loop = asyncio.get_running_loop()
        self.connected = False
        self.disconnected = asyncio.Event()
        self.added_handlers = []
        self.removed_handlers = []

    async def connect(self):
        self.connected = True

    async def is_user_authorized(self):
        return self.authorized

    async def disconnect(self):
        self.connected = False
        self.disconnected.set()

    async def get_entity(self, reference):
        return SimpleNamespace(username=str(reference))

    async def run_until_disconnected(self):
        await self.disconnected.wait()

    def add_event_handler(self, *handler):
        self.added_handlers.append(handler)

    def remove_event_handler(self, *handler):
        self.removed_handlers.append(handler)


async def _cancel_wait(awaitable, timeout):
    awaitable.close()
    raise asyncio.CancelledError


class ChannelReferenceTests(unittest.TestCase):
    def test_resolves_public_channel_url_to_username(self):
        self.assertEqual(
            _channel_reference("https://t.me/rabahcopons"),
            "rabahcopons",
        )

    def test_resolves_private_channel_link_to_peer_channel(self):
        reference = _channel_reference("https://t.me/c/1234567890/42")
        self.assertIsInstance(reference, PeerChannel)
        self.assertEqual(reference.channel_id, 1234567890)


class ChannelMonitorLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_external_ai_does_not_wait_for_other_provider_recovery(self):
        monitor = ChannelMonitor()
        queue = {
            "pending": 3,
            "processing": 5,
            "processed": 20,
            "oldest_pending_seconds": 120,
            "oldest_processing_seconds": 120,
        }
        wait_for = AsyncMock(side_effect=_cancel_wait)

        async def drain():
            monitor.last_batch_retryable = True
            return True

        monitor._drain_pending_batches = drain
        with (
            patch(
                "services.channel_monitor.get_channel_queue_status",
                return_value=queue,
            ),
            patch(
                "services.channel_monitor.get_ai_service_status",
                return_value={
                    "mode": "external",
                    "next_recovery_at": "2099-01-01T00:00:00+00:00",
                },
            ),
            patch("services.channel_monitor.asyncio.wait_for", new=wait_for),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await monitor._pending_retry_loop()

        wait_for.assert_awaited_once()
        self.assertEqual(wait_for.await_args.kwargs["timeout"], 60)

    async def test_local_ai_waits_until_external_recovery(self):
        monitor = ChannelMonitor()
        queue = {
            "pending": 3,
            "processing": 5,
            "processed": 20,
            "oldest_pending_seconds": 120,
            "oldest_processing_seconds": 120,
        }
        wait_for = AsyncMock(side_effect=_cancel_wait)
        drain = AsyncMock()
        monitor._drain_pending_batches = drain
        with (
            patch(
                "services.channel_monitor.get_channel_queue_status",
                return_value=queue,
            ),
            patch(
                "services.channel_monitor.get_ai_service_status",
                return_value={
                    "mode": "local",
                    "next_recovery_at": "2099-01-01T00:00:00+00:00",
                },
            ),
            patch("services.channel_monitor.asyncio.wait_for", new=wait_for),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await monitor._pending_retry_loop()

        drain.assert_not_awaited()
        wait_for.assert_awaited_once()
        self.assertEqual(wait_for.await_args.kwargs["timeout"], 300)

    async def test_manual_provider_disablement_defers_without_recovery_time(self):
        monitor = ChannelMonitor()
        queue = {
            "pending": 3,
            "processing": 5,
            "processed": 20,
            "oldest_pending_seconds": 120,
            "oldest_processing_seconds": 120,
        }
        wait_for = AsyncMock(side_effect=_cancel_wait)
        drain = AsyncMock()
        monitor._drain_pending_batches = drain
        with (
            patch(
                "services.channel_monitor.get_channel_queue_status",
                return_value=queue,
            ),
            patch(
                "services.channel_monitor.get_ai_service_status",
                return_value={
                    "mode": "local",
                    "next_recovery_at": None,
                },
            ),
            patch("services.channel_monitor.asyncio.wait_for", new=wait_for),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await monitor._pending_retry_loop()

        drain.assert_not_awaited()
        wait_for.assert_awaited_once()
        self.assertEqual(wait_for.await_args.kwargs["timeout"], 300)

    async def test_waking_retry_loop_resumes_after_provider_enablement(self):
        monitor = ChannelMonitor()
        queue = {
            "pending": 3,
            "processing": 5,
            "processed": 20,
            "oldest_pending_seconds": 120,
            "oldest_processing_seconds": 120,
        }
        statuses = iter((
            {"mode": "local", "next_recovery_at": None},
            {"mode": "external", "next_recovery_at": None},
        ))
        drain = AsyncMock(side_effect=asyncio.CancelledError)
        monitor._drain_pending_batches = drain

        async def wake_wait(awaitable, timeout):
            awaitable.close()
            monitor.wake_retry_loop()
            return True

        with (
            patch(
                "services.channel_monitor.get_channel_queue_status",
                return_value=queue,
            ),
            patch(
                "services.channel_monitor.get_ai_service_status",
                side_effect=lambda **_kwargs: next(statuses),
            ),
            patch(
                "services.channel_monitor.asyncio.wait_for",
                side_effect=wake_wait,
            ),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await monitor._pending_retry_loop()

        drain.assert_awaited_once()

    async def test_retry_loop_logs_warning_for_stale_processing(self):
        monitor = ChannelMonitor()
        monitor._drain_pending_batches = AsyncMock(
            side_effect=asyncio.CancelledError
        )
        queue = {
            "pending": 3,
            "processing": 1,
            "processed": 20,
            "oldest_pending_seconds": 120,
            "oldest_processing_seconds": 601,
        }
        with (
            patch(
                "services.channel_monitor.get_channel_queue_status",
                return_value=queue,
            ),
            patch("services.channel_monitor.get_ai_service_status", return_value={}),
            patch("services.channel_monitor.logger.warning") as warning,
        ):
            with self.assertRaises(asyncio.CancelledError):
                await monitor._pending_retry_loop()

        self.assertTrue(
            any(
                "ALERT=processing_stalled" in str(call)
                for call in warning.call_args_list
            )
        )

    async def test_missing_configuration_starts_no_background_tasks(self):
        monitor = ChannelMonitor()
        with (
            patch("services.channel_monitor.is_feature_enabled", return_value=True),
            patch("services.channel_monitor.TELEGRAM_API_ID", 0),
            patch("services.channel_monitor.TELEGRAM_API_HASH", ""),
            patch("services.channel_monitor.TELEGRAM_SESSION_STRING", ""),
        ):
            started = await monitor.start()

        self.assertFalse(started)
        self.assertIsNone(monitor.client)
        self.assertIsNone(monitor.cleanup_task)
        self.assertIsNone(monitor.retry_task)
        self.assertIsNone(monitor.task)

    async def test_start_is_idempotent_and_stop_removes_every_resource(self):
        monitor = ChannelMonitor()
        client = FakeTelegramClient()
        with (
            patch("services.channel_monitor.is_feature_enabled", return_value=True),
            patch("services.channel_monitor.TELEGRAM_API_ID", 1),
            patch("services.channel_monitor.TELEGRAM_API_HASH", "hash"),
            patch("services.channel_monitor.TELEGRAM_SESSION_STRING", "session"),
            patch("services.channel_monitor.StringSession", return_value=object()),
            patch("services.channel_monitor.TelegramClient", return_value=client),
            patch(
                "services.channel_monitor.get_telegram_channel_links",
                return_value=[{"link": "https://t.me/offers"}],
            ),
        ):
            self.assertTrue(await monitor.start())
            first_task = monitor.task
            self.assertTrue(await monitor.start())
            self.assertIs(monitor.task, first_task)
            self.assertEqual(len(client.added_handlers), 1)
            await monitor.stop()

        self.assertEqual(len(client.removed_handlers), 1)
        self.assertIsNone(monitor.client)
        self.assertIsNone(monitor.cleanup_task)
        self.assertIsNone(monitor.retry_task)
        self.assertIsNone(monitor.task)
        self.assertEqual(monitor.entities, [])

    async def test_unexpected_listener_completion_runs_full_teardown(self):
        monitor = ChannelMonitor()
        client = FakeTelegramClient()
        with (
            patch("services.channel_monitor.is_feature_enabled", return_value=True),
            patch("services.channel_monitor.TELEGRAM_API_ID", 1),
            patch("services.channel_monitor.TELEGRAM_API_HASH", "hash"),
            patch("services.channel_monitor.TELEGRAM_SESSION_STRING", "session"),
            patch("services.channel_monitor.StringSession", return_value=object()),
            patch("services.channel_monitor.TelegramClient", return_value=client),
            patch(
                "services.channel_monitor.get_telegram_channel_links",
                return_value=[{"link": "https://t.me/offers"}],
            ),
        ):
            self.assertTrue(await monitor.start())
            client.disconnected.set()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            if monitor._teardown_task:
                await monitor._teardown_task

        self.assertEqual(len(client.removed_handlers), 1)
        self.assertIsNone(monitor.client)
        self.assertIsNone(monitor.task)
        self.assertIsNone(monitor.retry_task)
        self.assertIsNone(monitor.cleanup_task)

    async def test_unauthorized_session_leaves_no_background_tasks(self):
        monitor = ChannelMonitor()
        client = FakeTelegramClient(authorized=False)
        with (
            patch("services.channel_monitor.is_feature_enabled", return_value=True),
            patch("services.channel_monitor.TELEGRAM_API_ID", 1),
            patch("services.channel_monitor.TELEGRAM_API_HASH", "hash"),
            patch("services.channel_monitor.TELEGRAM_SESSION_STRING", "session"),
            patch("services.channel_monitor.StringSession", return_value=object()),
            patch("services.channel_monitor.TelegramClient", return_value=client),
        ):
            started = await monitor.start()

        self.assertFalse(started)
        self.assertIsNone(monitor.client)
        self.assertIsNone(monitor.cleanup_task)
        self.assertIsNone(monitor.retry_task)
        self.assertIsNone(monitor.task)

    async def test_invalid_session_string_does_not_create_client(self):
        monitor = ChannelMonitor()
        with (
            patch("services.channel_monitor.is_feature_enabled", return_value=True),
            patch("services.channel_monitor.TELEGRAM_API_ID", 1),
            patch("services.channel_monitor.TELEGRAM_API_HASH", "hash"),
            patch("services.channel_monitor.TELEGRAM_SESSION_STRING", "invalid"),
            patch(
                "services.channel_monitor.StringSession",
                side_effect=ValueError("invalid session"),
            ),
            patch("services.channel_monitor.TelegramClient") as client_factory,
        ):
            self.assertFalse(await monitor.start())

        client_factory.assert_not_called()
        self.assertIsNone(monitor.cleanup_task)
        self.assertIsNone(monitor.retry_task)

    async def test_connection_failure_cleans_up_client(self):
        monitor = ChannelMonitor()
        client = FakeTelegramClient()
        client.connect = AsyncMock(side_effect=RuntimeError("network"))
        with (
            patch("services.channel_monitor.is_feature_enabled", return_value=True),
            patch("services.channel_monitor.TELEGRAM_API_ID", 1),
            patch("services.channel_monitor.TELEGRAM_API_HASH", "hash"),
            patch("services.channel_monitor.TELEGRAM_SESSION_STRING", "session"),
            patch("services.channel_monitor.StringSession", return_value=object()),
            patch("services.channel_monitor.TelegramClient", return_value=client),
        ):
            self.assertFalse(await monitor.start())

        self.assertIsNone(monitor.client)
        self.assertFalse(client.connected)
        self.assertIsNone(monitor.cleanup_task)
        self.assertIsNone(monitor.retry_task)

    async def test_database_failure_cleans_up_connected_client(self):
        monitor = ChannelMonitor()
        client = FakeTelegramClient()
        with (
            patch("services.channel_monitor.is_feature_enabled", return_value=True),
            patch("services.channel_monitor.TELEGRAM_API_ID", 1),
            patch("services.channel_monitor.TELEGRAM_API_HASH", "hash"),
            patch("services.channel_monitor.TELEGRAM_SESSION_STRING", "session"),
            patch("services.channel_monitor.StringSession", return_value=object()),
            patch("services.channel_monitor.TelegramClient", return_value=client),
            patch(
                "services.channel_monitor.get_telegram_channel_links",
                side_effect=RuntimeError("database"),
            ),
        ):
            self.assertFalse(await monitor.start())

        self.assertIsNone(monitor.client)
        self.assertIsNone(monitor.cleanup_task)
        self.assertIsNone(monitor.retry_task)

    async def test_no_channels_returns_ready_without_background_tasks(self):
        monitor = ChannelMonitor()
        client = FakeTelegramClient()
        with (
            patch("services.channel_monitor.is_feature_enabled", return_value=True),
            patch("services.channel_monitor.TELEGRAM_API_ID", 1),
            patch("services.channel_monitor.TELEGRAM_API_HASH", "hash"),
            patch("services.channel_monitor.TELEGRAM_SESSION_STRING", "session"),
            patch("services.channel_monitor.StringSession", return_value=object()),
            patch("services.channel_monitor.TelegramClient", return_value=client),
            patch(
                "services.channel_monitor.get_telegram_channel_links",
                return_value=[],
            ),
        ):
            self.assertTrue(await monitor.start())

        self.assertIsNone(monitor.client)
        self.assertIsNone(monitor.cleanup_task)
        self.assertIsNone(monitor.retry_task)
        self.assertIsNone(monitor.task)


class ChannelMonitorPipelineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.processing_lock = patch(
            "services.channel_monitor.acquire_channel_processing_lock",
            return_value=object(),
        )
        self.ai_status = patch(
            "services.channel_monitor.get_ai_service_status",
            return_value={"mode": "external"},
        )
        self.processing_unlock = patch(
            "services.channel_monitor.release_channel_processing_lock",
        )
        self.processing_lock.start()
        self.ai_status.start()
        self.processing_unlock.start()
        self.addCleanup(self.processing_lock.stop)
        self.addCleanup(self.ai_status.stop)
        self.addCleanup(self.processing_unlock.stop)

    @staticmethod
    def _processing_row(post_id):
        text = f"عرض {post_id} https://www.aliexpress.com/item/{post_id}.html"
        return {
            "id": post_id,
            "source_channel_id": -100123,
            "source_message_id": post_id,
            "content": text,
            "source_link": f"https://t.me/offers/{post_id}",
            "photo_file_id": None,
            "processing_status": "processing",
        }

    @staticmethod
    def _pending_row(post_id):
        return {
            "id": post_id,
            "source_channel_id": -100123,
            "source_message_id": post_id,
            "processing_status": "pending",
        }

    async def test_analyzes_only_a_full_processing_batch(self):
        monitor = ChannelMonitor()
        rows = [self._processing_row(index) for index in range(1, 5)]
        with (
            patch(
                "services.channel_monitor.get_processing_channel_posts",
                side_effect=[rows, rows],
            ),
            patch.object(
                monitor,
                "_prepare_processing_capacity",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "services.channel_monitor.process_channel_messages",
                new=AsyncMock(),
            ) as analyze,
        ):
            submitted = await monitor._drain_pending_posts()

        self.assertFalse(submitted)
        analyze.assert_not_awaited()

    async def test_analyzes_stale_partial_processing_batch(self):
        monitor = ChannelMonitor()
        rows = [self._processing_row(index) for index in range(1, 5)]
        started_at = datetime.now(timezone.utc) - timedelta(hours=1, seconds=1)
        for row in rows:
            row["processing_started_at"] = started_at
        with (
            patch(
                "services.channel_monitor.get_processing_channel_posts",
                side_effect=[rows, rows],
            ),
            patch.object(
                monitor,
                "_prepare_processing_capacity",
                new=AsyncMock(return_value=False),
            ) as prepare,
            patch(
                "services.channel_monitor.process_channel_messages",
                new=AsyncMock(
                    return_value=ChannelProcessResult([row["id"] for row in rows])
                ),
            ) as analyze,
        ):
            submitted = await monitor._drain_pending_posts()

        self.assertTrue(submitted)
        analyze.assert_awaited_once()
        self.assertEqual(len(analyze.await_args.args[0]), 4)
        self.assertEqual(prepare.await_args.args, (CHANNEL_AI_BATCH_SIZE * 2,))

    async def test_retry_method_returns_pipeline_result(self):
        monitor = ChannelMonitor()
        with patch.object(
            monitor,
            "_drain_pending_posts",
            new=AsyncMock(return_value=True),
        ):
            self.assertTrue(await monitor._retry_pending_posts())

    async def test_ready_next_batch_skips_drain_delay(self):
        monitor = ChannelMonitor()
        monitor._retry_pending_posts = AsyncMock(
            side_effect=[True, False],
        )
        with (
            patch(
                "services.channel_monitor.get_processing_channel_posts",
                return_value=[
                    self._processing_row(index)
                    for index in range(1, 6)
                ],
            ),
            patch(
                "services.channel_monitor.asyncio.sleep",
                new=AsyncMock(),
            ) as sleep,
        ):
            self.assertTrue(await monitor._drain_pending_batches())

        sleep.assert_not_awaited()

    async def test_second_process_cannot_enter_an_owned_pipeline(self):
        monitor = ChannelMonitor()
        with (
            patch(
                "services.channel_monitor.acquire_channel_processing_lock",
                return_value=None,
            ),
            patch.object(
                monitor,
                "_drain_pending_posts_owned",
                new=AsyncMock(),
            ) as owned_pipeline,
        ):
            self.assertFalse(await monitor._drain_pending_posts())

        owned_pipeline.assert_not_awaited()

    async def test_defers_processing_when_all_external_ai_quotas_are_exhausted(self):
        monitor = ChannelMonitor()
        rows = [self._processing_row(index) for index in range(1, 6)]
        with (
            patch(
                "services.channel_monitor.get_ai_service_status",
                return_value={
                    "mode": "local",
                    "next_recovery_at": "2099-01-01T00:00:00+00:00",
                },
            ),
            patch(
                "services.channel_monitor.get_processing_channel_posts",
                return_value=rows,
            ) as load_processing,
            patch(
                "services.channel_monitor.requeue_processing_channel_posts",
                return_value=5,
            ) as requeue_processing,
            patch(
                "services.channel_monitor.process_channel_messages",
                new=AsyncMock(),
            ) as analyze,
        ):
            submitted = await monitor._drain_pending_posts()

        self.assertFalse(submitted)
        load_processing.assert_called_once_with(10)
        requeue_processing.assert_called_once_with([1, 2, 3, 4, 5])
        analyze.assert_not_awaited()

    async def test_prepares_next_five_while_current_five_are_analyzed(self):
        monitor = ChannelMonitor()
        rows = [self._processing_row(index) for index in range(1, 6)]
        analysis_started = asyncio.Event()
        next_wave_finished = asyncio.Event()

        async def analyze(candidates):
            self.assertEqual(len(candidates), 5)
            analysis_started.set()
            await asyncio.wait_for(next_wave_finished.wait(), timeout=1)
            return ChannelProcessResult([item["post_id"] for item in candidates])

        async def prepare_capacity(target_count):
            self.assertEqual(target_count, 10)
            await asyncio.wait_for(analysis_started.wait(), timeout=1)
            next_wave_finished.set()
            return True

        with (
            patch(
                "services.channel_monitor.get_processing_channel_posts",
                return_value=rows,
            ),
            patch.object(
                monitor,
                "_prepare_processing_capacity",
                new=AsyncMock(side_effect=prepare_capacity),
            ) as prepare,
            patch(
                "services.channel_monitor.process_channel_messages",
                new=AsyncMock(side_effect=analyze),
            ) as analyze_batch,
        ):
            submitted = await monitor._drain_pending_posts()

        self.assertTrue(submitted)
        analyze_batch.assert_awaited_once()
        prepare.assert_awaited_once_with(10)

    async def test_preparation_failure_waits_for_analysis_before_unlocking(self):
        monitor = ChannelMonitor()
        rows = [self._processing_row(index) for index in range(1, 6)]
        analysis_finished = asyncio.Event()

        async def analyze(candidates):
            await asyncio.sleep(0.01)
            analysis_finished.set()
            return ChannelProcessResult([item["post_id"] for item in candidates])

        with (
            patch(
                "services.channel_monitor.get_processing_channel_posts",
                return_value=rows,
            ),
            patch.object(
                monitor,
                "_prepare_processing_capacity",
                new=AsyncMock(side_effect=RuntimeError("database")),
            ),
            patch(
                "services.channel_monitor.process_channel_messages",
                new=AsyncMock(side_effect=analyze),
            ),
            patch(
                "services.channel_monitor.release_channel_processing_lock",
            ) as release_lock,
        ):
            self.assertTrue(await monitor._drain_pending_posts())

        self.assertTrue(analysis_finished.is_set())
        release_lock.assert_called_once()

    async def test_cancellation_waits_for_analysis_before_unlocking(self):
        monitor = ChannelMonitor()
        rows = [self._processing_row(index) for index in range(1, 6)]
        analysis_started = asyncio.Event()
        allow_analysis_finish = asyncio.Event()
        released = asyncio.Event()

        async def analyze(candidates):
            analysis_started.set()
            await allow_analysis_finish.wait()
            return ChannelProcessResult([item["post_id"] for item in candidates])

        def release_lock(connection):
            released.set()

        with (
            patch(
                "services.channel_monitor.get_processing_channel_posts",
                return_value=rows,
            ),
            patch.object(
                monitor,
                "_prepare_processing_capacity",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "services.channel_monitor.process_channel_messages",
                new=AsyncMock(side_effect=analyze),
            ),
            patch(
                "services.channel_monitor.release_channel_processing_lock",
                side_effect=release_lock,
            ),
        ):
            drain_task = asyncio.create_task(monitor._drain_pending_posts())
            await analysis_started.wait()
            drain_task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(released.is_set())
            allow_analysis_finish.set()
            with self.assertRaises(asyncio.CancelledError):
                await drain_task

        self.assertTrue(released.is_set())

    async def test_missing_source_is_deleted_while_still_pending(self):
        monitor = ChannelMonitor()
        monitor.client = SimpleNamespace(
            get_messages=AsyncMock(return_value=None),
        )
        item = self._pending_row(7)
        with patch(
            "services.channel_monitor.delete_channel_post_after_analysis_failure",
            return_value=True,
        ) as delete_post:
            candidate, discarded = await monitor._prepare_pending_candidate(item)

        self.assertIsNone(candidate)
        self.assertTrue(discarded)
        delete_post.assert_called_once_with(7)

    async def test_transport_failure_retains_pending_source(self):
        monitor = ChannelMonitor()
        monitor.client = SimpleNamespace(
            get_messages=AsyncMock(side_effect=RuntimeError("temporary")),
        )
        item = self._pending_row(8)
        with patch(
            "services.channel_monitor.delete_channel_post_after_analysis_failure",
        ) as delete_post:
            candidate, discarded = await monitor._prepare_pending_candidate(item)

        self.assertIsNone(candidate)
        self.assertFalse(discarded)
        delete_post.assert_not_called()

    async def test_post_without_aliexpress_link_is_deleted_before_processing(self):
        monitor = ChannelMonitor()
        monitor.client = SimpleNamespace(
            get_messages=AsyncMock(return_value=SimpleNamespace(id=9)),
            get_entity=AsyncMock(return_value=SimpleNamespace(username="offers")),
        )
        item = self._pending_row(9)
        with (
            patch(
                "services.channel_monitor.prepare_channel_message",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "services.channel_monitor.delete_channel_post_after_analysis_failure",
                return_value=True,
            ) as delete_post,
        ):
            candidate, discarded = await monitor._prepare_pending_candidate(item)

        self.assertIsNone(candidate)
        self.assertTrue(discarded)
        delete_post.assert_called_once_with(9)

    async def test_new_message_is_registered_pending_before_pipeline_trigger(self):
        monitor = ChannelMonitor()
        event = SimpleNamespace(
            message=SimpleNamespace(id=11),
            chat_id=-100123,
            chat=SimpleNamespace(username="offers"),
            get_chat=AsyncMock(return_value=SimpleNamespace(username="offers")),
        )
        with (
            patch(
                "services.channel_monitor.register_channel_message",
                new=AsyncMock(return_value={"post_id": 11}),
            ) as prepare,
            patch.object(
                monitor,
                "_drain_pending_posts",
                new=AsyncMock(return_value=False),
            ) as drain,
        ):
            await monitor._handle_new_message(event)

        self.assertNotIn("processing_status", prepare.call_args.kwargs)
        drain.assert_awaited_once_with()
        event.get_chat.assert_not_awaited()

    async def test_fifth_registered_post_starts_ai_before_next_capacity_finishes(self):
        monitor = ChannelMonitor()
        first_rows = [self._processing_row(index) for index in range(1, 5)]
        full_rows = [self._processing_row(index) for index in range(1, 6)]
        analysis_started = asyncio.Event()
        next_capacity_finished = asyncio.Event()

        async def analyze(candidates):
            self.assertEqual(len(candidates), 5)
            analysis_started.set()
            await asyncio.wait_for(next_capacity_finished.wait(), timeout=1)
            return ChannelProcessResult([item["post_id"] for item in candidates])

        async def prepare_capacity(target_count):
            if target_count == CHANNEL_AI_BATCH_SIZE:
                return True
            self.assertEqual(target_count, CHANNEL_AI_BATCH_SIZE * 2)
            await asyncio.wait_for(analysis_started.wait(), timeout=1)
            next_capacity_finished.set()
            return True

        event = SimpleNamespace(
            message=SimpleNamespace(id=5),
            chat_id=-100123,
            chat=SimpleNamespace(username="offers"),
            get_chat=AsyncMock(),
        )
        with (
            patch(
                "services.channel_monitor.register_channel_message",
                new=AsyncMock(return_value={"post_id": 5}),
            ) as register,
            patch(
                "services.channel_monitor.get_processing_channel_posts",
                side_effect=[first_rows, full_rows],
            ),
            patch.object(
                monitor,
                "_prepare_processing_capacity",
                new=AsyncMock(side_effect=prepare_capacity),
            ) as prepare,
            patch(
                "services.channel_monitor.process_channel_messages",
                new=AsyncMock(side_effect=analyze),
            ) as analyze_batch,
        ):
            await monitor._handle_new_message(event)

        register.assert_awaited_once()
        analyze_batch.assert_awaited_once()
        prepare.assert_awaited()
        self.assertEqual(
            prepare.await_args_list[0].args,
            (CHANNEL_AI_BATCH_SIZE,),
        )
        self.assertEqual(
            prepare.await_args_list[1].args,
            (CHANNEL_AI_BATCH_SIZE * 2,),
        )
        event.get_chat.assert_not_awaited()


class ChannelProcessingLockPostgresTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ai_status = patch(
            "services.channel_monitor.get_ai_service_status",
            return_value={"mode": "external"},
        )
        self.ai_status.start()
        self.addCleanup(self.ai_status.stop)

    @classmethod
    def setUpClass(cls):
        if not db.DATABASE_URL:
            raise unittest.SkipTest("PostgreSQL DATABASE_URL is not configured")
        try:
            connection = db.get_db_connection()
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
            connection.close()
        except Exception as exc:
            raise unittest.SkipTest(f"PostgreSQL is unavailable: {exc}") from exc

    @staticmethod
    def _processing_rows():
        return [
            {
                "id": index,
                "source_channel_id": -100123,
                "source_message_id": index,
                "content": f"عرض {index}",
                "source_link": f"https://t.me/offers/{index}",
                "photo_file_id": None,
                "processing_status": "processing",
            }
            for index in range(1, 6)
        ]

    async def _assert_lock_can_be_reacquired(self):
        connection = await asyncio.to_thread(db.acquire_channel_processing_lock)
        self.assertIsNotNone(connection)
        await asyncio.to_thread(db.release_channel_processing_lock, connection)

    async def test_only_one_independent_connection_owns_the_lock(self):
        first = await asyncio.to_thread(db.acquire_channel_processing_lock)
        self.assertIsNotNone(first)
        try:
            second = await asyncio.to_thread(db.acquire_channel_processing_lock)
            self.assertIsNone(second)
        finally:
            await asyncio.to_thread(db.release_channel_processing_lock, first)

        await self._assert_lock_can_be_reacquired()

    async def test_success_releases_database_lock(self):
        monitor = ChannelMonitor()
        rows = self._processing_rows()
        with (
            patch(
                "services.channel_monitor.get_processing_channel_posts",
                return_value=rows,
            ),
            patch.object(
                monitor,
                "_prepare_processing_capacity",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "services.channel_monitor.process_channel_messages",
                new=AsyncMock(
                    return_value=ChannelProcessResult(
                        [row["id"] for row in rows]
                    )
                ),
            ),
        ):
            self.assertTrue(await monitor._drain_pending_posts())

        await self._assert_lock_can_be_reacquired()

    async def test_failure_releases_database_lock(self):
        monitor = ChannelMonitor()
        rows = self._processing_rows()
        with (
            patch(
                "services.channel_monitor.get_processing_channel_posts",
                return_value=rows,
            ),
            patch.object(
                monitor,
                "_prepare_processing_capacity",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "services.channel_monitor.process_channel_messages",
                new=AsyncMock(side_effect=RuntimeError("analysis failed")),
            ),
            patch(
                "services.channel_monitor.mark_channel_post_pending",
                return_value=True,
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "analysis failed"):
                await monitor._drain_pending_posts()

        await self._assert_lock_can_be_reacquired()

    async def test_cancellation_keeps_lock_until_analysis_finishes(self):
        monitor = ChannelMonitor()
        rows = self._processing_rows()
        analysis_started = asyncio.Event()
        allow_analysis_finish = asyncio.Event()

        async def analyze(_candidates):
            analysis_started.set()
            await allow_analysis_finish.wait()
            return ChannelProcessResult([row["id"] for row in rows])

        with (
            patch(
                "services.channel_monitor.get_processing_channel_posts",
                return_value=rows,
            ),
            patch.object(
                monitor,
                "_prepare_processing_capacity",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "services.channel_monitor.process_channel_messages",
                new=AsyncMock(side_effect=analyze),
            ),
        ):
            drain_task = asyncio.create_task(monitor._drain_pending_posts())
            await asyncio.wait_for(analysis_started.wait(), timeout=2)

            self.assertIsNone(
                await asyncio.to_thread(db.acquire_channel_processing_lock)
            )
            drain_task.cancel()
            await asyncio.sleep(0)
            self.assertIsNone(
                await asyncio.to_thread(db.acquire_channel_processing_lock)
            )

            allow_analysis_finish.set()
            with self.assertRaises(asyncio.CancelledError):
                await drain_task

        await self._assert_lock_can_be_reacquired()


if __name__ == "__main__":
    unittest.main()

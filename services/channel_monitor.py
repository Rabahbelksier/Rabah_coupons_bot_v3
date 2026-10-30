import asyncio
import io
import logging
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from urllib.parse import urlparse

from telethon import TelegramClient, events
from telethon.errors import (
    ChannelBannedError,
    ChannelIdInvalidError,
    ChannelInvalidError,
    ChannelPrivateError,
    ChatAdminInviteRequiredError,
    ChatAdminRequiredError,
    ChatForbiddenError,
    ChatInvalidError,
    ChatRestrictedError,
    MessageIdInvalidError,
    NeedChatInvalidError,
    PeerIdInvalidError,
    PeerIdNotSupportedError,
    PublicChannelMissingError,
    UserBannedInChannelError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
)
from telethon.tl.types import PeerChannel
from telethon.sessions import StringSession

from config import (
    CHANNEL_AI_BATCH_SIZE,
    CHANNEL_AI_DRAIN_BATCHES_PER_CYCLE,
    CHANNEL_AI_DRAIN_DELAY_SECONDS,
    TELEGRAM_API_HASH,
    TELEGRAM_API_ID,
    TELEGRAM_SESSION_STRING,
)
from core.db import (
    acquire_channel_processing_lock,
    delete_channel_post_after_analysis_failure,
    delete_expired_channel_posts,
    get_pending_channel_posts,
    get_processing_channel_posts,
    get_channel_queue_status,
    get_telegram_channel_links,
    is_feature_enabled,
    mark_channel_post_pending,
    requeue_processing_channel_posts,
    release_channel_processing_lock,
)
from core.channel_processor import (
    prepare_channel_message,
    process_channel_messages,
    register_channel_message,
)
from core.ai import get_ai_service_status
from utils.parser import extract_aliexpress_urls

logger = logging.getLogger(__name__)

CLEANUP_INTERVAL_SECONDS = 60 * 60
PENDING_RETRY_INTERVAL_SECONDS = 60
PROCESSING_STALE_SECONDS = 10 * 60
PARTIAL_PROCESSING_BATCH_TIMEOUT_SECONDS = 60 * 60
_TERMINAL_SOURCE_ERRORS = (
    ChannelBannedError,
    ChannelIdInvalidError,
    ChannelInvalidError,
    ChannelPrivateError,
    ChatAdminInviteRequiredError,
    ChatAdminRequiredError,
    ChatForbiddenError,
    ChatInvalidError,
    ChatRestrictedError,
    NeedChatInvalidError,
    PeerIdInvalidError,
    PeerIdNotSupportedError,
    PublicChannelMissingError,
    UserBannedInChannelError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
)


class ChannelMonitor:
    """Long-lived MTProto listener using a dedicated Telegram user account."""

    def __init__(self):
        self.client = None
        self.task = None
        self.cleanup_task = None
        self.retry_task = None
        self.entities = []
        self.batch_lock = asyncio.Lock()
        self.last_batch_retryable = False
        self._handler = None
        self._stop_lock = asyncio.Lock()
        self._teardown_task = None
        self._retry_wakeup = asyncio.Event()

    @property
    def configured(self):
        return bool(
            TELEGRAM_API_ID
            and TELEGRAM_API_HASH
            and TELEGRAM_SESSION_STRING
        )

    async def start(self):
        if self.task and not self.task.done():
            return True
        if self.client:
            await self.stop()
        if not await asyncio.to_thread(is_feature_enabled, "ai_search"):
            logger.info("Channel monitor is disabled because AI search is disabled")
            return False
        if not self.configured:
            missing = [
                name
                for name, value in (
                    ("TELEGRAM_API_ID", TELEGRAM_API_ID),
                    ("TELEGRAM_API_HASH", TELEGRAM_API_HASH),
                    ("TELEGRAM_SESSION_STRING", TELEGRAM_SESSION_STRING),
                )
                if not value
            ]
            logger.warning(
                "Channel monitor is disabled; missing environment variable(s): %s",
                ", ".join(missing),
            )
            return False

        try:
            session = StringSession(TELEGRAM_SESSION_STRING)
        except ValueError:
            logger.error(
                "Channel monitor is disabled: TELEGRAM_SESSION_STRING is not a "
                "valid Telethon StringSession; run scripts/create_telegram_session.py"
            )
            return False

        self.client = TelegramClient(
            session,
            TELEGRAM_API_ID,
            TELEGRAM_API_HASH,
        )
        try:
            await self.client.connect()
            if not await self.client.is_user_authorized():
                logger.error(
                    "Channel monitor is disabled: the Telegram user session is unauthorized"
                )
                await self.client.disconnect()
                self.client = None
                return False
        except Exception:
            logger.exception("Channel monitor could not connect to Telegram")
            await self.client.disconnect()
            self.client = None
            return False

        try:
            channels = get_telegram_channel_links()
        except Exception:
            logger.exception("Channel monitor could not load channels from the database")
            await self.client.disconnect()
            self.client = None
            return False
        logger.info("Loaded %d channel(s) from the database", len(channels))
        for channel in channels:
            try:
                self.entities.append(
                    await self.client.get_entity(_channel_reference(channel["link"]))
                )
            except Exception:
                logger.exception("Could not resolve monitored channel %s", channel["link"])

        if not self.entities:
            logger.warning("Channel monitor is ready but no channels are configured")
            await self.client.disconnect()
            self.client = None
            return True

        self._handler = (
            self._handle_new_message,
            events.NewMessage(chats=self.entities),
        )
        self.client.add_event_handler(*self._handler)
        if not self.cleanup_task or self.cleanup_task.done():
            self.cleanup_task = asyncio.create_task(self._cleanup_loop())
        if not self.retry_task or self.retry_task.done():
            self.retry_task = asyncio.create_task(self._pending_retry_loop())
        self.task = self.client.loop.create_task(
            self.client.run_until_disconnected()
        )
        self.task.add_done_callback(self._listener_done)
        logger.info(
            "Channel monitor started for %d channel(s); "
            "real-time posts only, history sync disabled",
            len(self.entities),
        )
        return True

    async def _retry_pending_posts(self):
        """Re-fetch and process one eligible channel AI batch."""
        return await self._drain_pending_posts()

    def wake_retry_loop(self):
        """Wake a deferred retry loop after an AI provider is enabled."""
        self._retry_wakeup.set()

    async def _drain_pending_batches(self):
        """Drain several batches while stopping promptly on a retryable failure."""
        submitted_any = False
        for batch_number in range(CHANNEL_AI_DRAIN_BATCHES_PER_CYCLE):
            self.last_batch_retryable = False
            submitted = await self._retry_pending_posts()
            if not submitted:
                break
            submitted_any = True
            if self.last_batch_retryable:
                break
            if batch_number + 1 < CHANNEL_AI_DRAIN_BATCHES_PER_CYCLE:
                next_processing = await asyncio.to_thread(
                    get_processing_channel_posts,
                    CHANNEL_AI_BATCH_SIZE,
                )
                if len(next_processing) < CHANNEL_AI_BATCH_SIZE:
                    await asyncio.sleep(CHANNEL_AI_DRAIN_DELAY_SECONDS)
                else:
                    logger.info(
                        "Channel AI next batch is ready; dispatching without "
                        "drain delay"
                    )
        return submitted_any

    async def _drain_pending_posts(self):
        drain_started_at = time.perf_counter()
        logger.info("Channel AI drain triggered")
        if not await asyncio.to_thread(is_feature_enabled, "ai_search"):
            logger.info("Channel AI drain skipped because AI search is disabled")
            return False
        lock_requested_at = time.perf_counter()
        acquire_task = asyncio.create_task(
            asyncio.to_thread(acquire_channel_processing_lock)
        )
        try:
            lock_connection = await asyncio.shield(acquire_task)
        except asyncio.CancelledError:
            lock_connection = await asyncio.shield(acquire_task)
            if lock_connection:
                await asyncio.to_thread(
                    release_channel_processing_lock,
                    lock_connection,
                )
            raise
        if not lock_connection:
            logger.info(
                "Channel AI drain skipped because processing lock is busy "
                "lock_wait_ms=%.1f",
                (time.perf_counter() - lock_requested_at) * 1000,
            )
            return False
        logger.info(
            "Channel AI processing lock acquired lock_wait_ms=%.1f",
            (time.perf_counter() - lock_requested_at) * 1000,
        )
        try:
            return await self._drain_pending_posts_owned()
        finally:
            await asyncio.to_thread(
                release_channel_processing_lock,
                lock_connection,
            )

    async def _channel_analysis_deferred_for_quota(self):
        try:
            status = await asyncio.to_thread(
                get_ai_service_status,
                request_kind="channel",
            )
        except Exception:
            logger.exception("Could not read AI status before channel analysis")
            return False
        if status.get("mode") == "local":
            logger.info(
                "Deferring channel analysis until an eligible external AI "
                "provider is available recovery_at=%s",
                status.get("next_recovery_at"),
            )
            return True
        return False

    async def _drain_pending_posts_owned(self):
        """Build a processing batch and analyze a stale partial batch if needed."""
        self.last_batch_retryable = False
        pipeline_started_at = time.perf_counter()
        async with self.batch_lock:
            if not await asyncio.to_thread(is_feature_enabled, "ai_search"):
                logger.info("Channel AI drain skipped because AI search is disabled")
                return False
            if await self._channel_analysis_deferred_for_quota():
                await self._requeue_processing_rows()
                logger.info(
                    "Channel AI drain deferred while all external providers "
                    "are unavailable duration_ms=%.1f",
                    (time.perf_counter() - pipeline_started_at) * 1000,
                )
                return False
            try:
                processing = await asyncio.to_thread(
                    get_processing_channel_posts, CHANNEL_AI_BATCH_SIZE
                )
            except Exception:
                logger.exception("Could not load pending channel posts")
                return False

            processing_partial_due = (
                len(processing) < CHANNEL_AI_BATCH_SIZE
                and self._partial_processing_batch_is_due(processing)
            )
            if len(processing) < CHANNEL_AI_BATCH_SIZE and not processing_partial_due:
                logger.info(
                    "Channel AI first batch incomplete current=%d target=%d; "
                    "preparing remaining capacity",
                    len(processing),
                    CHANNEL_AI_BATCH_SIZE,
                )
                await self._prepare_processing_capacity(CHANNEL_AI_BATCH_SIZE)
                processing = await asyncio.to_thread(
                    get_processing_channel_posts, CHANNEL_AI_BATCH_SIZE
                )
            candidates = [self._candidate_from_processing(row) for row in processing]
            partial_batch = len(candidates) < CHANNEL_AI_BATCH_SIZE
            if partial_batch and not self._partial_processing_batch_is_due(processing):
                return False

            logger.info(
                "Channel AI batch dispatching count=%d partial=%s preparation_ms=%.1f",
                len(candidates),
                partial_batch,
                (time.perf_counter() - pipeline_started_at) * 1000,
            )
            analysis_task = asyncio.create_task(process_channel_messages(candidates))
            logger.info(
                "Channel AI analysis task created count=%d; preparing next "
                "capacity target=%d",
                len(candidates),
                CHANNEL_AI_BATCH_SIZE * 2,
            )
            prepare_task = asyncio.create_task(
                self._prepare_processing_capacity(CHANNEL_AI_BATCH_SIZE * 2)
            )
            batch_group = asyncio.gather(
                analysis_task,
                prepare_task,
                return_exceptions=True,
            )
            cancelled = False
            try:
                process_result, prepare_result = await asyncio.shield(batch_group)
            except asyncio.CancelledError:
                cancelled = True
                process_result, prepare_result = await asyncio.shield(batch_group)

            if isinstance(process_result, BaseException):
                await asyncio.gather(*(
                    asyncio.to_thread(mark_channel_post_pending, candidate["post_id"])
                    for candidate in candidates
                ))
                if cancelled:
                    raise asyncio.CancelledError
                raise process_result
            if isinstance(prepare_result, BaseException):
                logger.error(
                    "Preparing the next channel batch failed",
                    exc_info=(
                        type(prepare_result),
                        prepare_result,
                        prepare_result.__traceback__,
                    ),
                )
            elif (
                getattr(process_result, "retryable_failure", False)
                and await self._channel_analysis_deferred_for_quota()
            ):
                await self._requeue_processing_rows()
            if cancelled:
                raise asyncio.CancelledError
            self.last_batch_retryable = bool(
                getattr(process_result, "retryable_failure", False)
            )
            logger.info(
                "Channel AI batch submitted count=%d retryable_failure=%s",
                len(candidates),
                self.last_batch_retryable,
            )
            return True

    @staticmethod
    def _partial_processing_batch_is_due(processing):
        """Return whether the oldest processing row has waited one hour."""
        started_at_values = [
            row.get("processing_started_at")
            for row in processing
            if row.get("processing_started_at") is not None
        ]
        if not started_at_values:
            return False

        normalized = []
        for started_at in started_at_values:
            if isinstance(started_at, str):
                try:
                    started_at = datetime.fromisoformat(
                        started_at.replace("Z", "+00:00")
                    )
                except ValueError:
                    continue
            if not isinstance(started_at, datetime):
                continue
            if started_at.tzinfo is None:
                started_at = started_at.replace(tzinfo=timezone.utc)
            normalized.append(started_at.astimezone(timezone.utc))

        if not normalized:
            return False
        oldest_started_at = min(normalized)
        age_seconds = (
            datetime.now(timezone.utc) - oldest_started_at
        ).total_seconds()
        return age_seconds >= PARTIAL_PROCESSING_BATCH_TIMEOUT_SECONDS

    async def _requeue_processing_rows(self):
        processing_rows = await asyncio.to_thread(
            get_processing_channel_posts,
            CHANNEL_AI_BATCH_SIZE * 2,
        )
        processing_ids = [
            row["id"]
            for row in processing_rows
            if row.get("processing_status") == "processing"
        ]
        if not processing_ids:
            return 0
        requeued = await asyncio.to_thread(
            requeue_processing_channel_posts,
            processing_ids,
        )
        if requeued:
            logger.warning(
                "Requeued %d channel processing row(s) because all "
                "channel AI providers are unavailable",
                requeued,
            )
        return requeued

    def _candidate_from_processing(self, row):
        text = row.get("content") or ""
        message = SimpleNamespace(id=row["source_message_id"], message=text, raw_text=text)
        return {
            "post_id": row["id"],
            "message": message,
            "channel_id": row["source_channel_id"],
            "channel_username": None,
            "text": text,
            "urls": extract_aliexpress_urls(text),
        }

    async def _prepare_pending_wave(self, rows):
        return await asyncio.gather(*(
            self._prepare_pending_candidate(item) for item in rows
        )) if rows else []

    async def _prepare_processing_capacity(self, target_count):
        """Prepare pending rows until the processing queue reaches a target."""
        started_at = time.perf_counter()
        made_progress = False
        logger.info("Channel processing capacity preparation started target=%d", target_count)
        while True:
            processing = await asyncio.to_thread(
                get_processing_channel_posts, target_count
            )
            needed = target_count - len(processing)
            if needed <= 0:
                logger.info(
                    "Channel processing capacity ready target=%d current=%d "
                    "prepared=%s duration_ms=%.1f",
                    target_count,
                    len(processing),
                    made_progress,
                    (time.perf_counter() - started_at) * 1000,
                )
                return made_progress
            pending = await asyncio.to_thread(get_pending_channel_posts, needed)
            if not pending:
                logger.info(
                    "Channel processing capacity stopped target=%d current=%d "
                    "prepared=%s duration_ms=%.1f reason=no_pending",
                    target_count,
                    len(processing),
                    made_progress,
                    (time.perf_counter() - started_at) * 1000,
                )
                return made_progress
            prepared = await self._prepare_pending_wave(pending)
            wave_progress = any(
                candidate is not None or discarded
                for candidate, discarded in prepared
            )
            made_progress = made_progress or wave_progress
            if not wave_progress:
                logger.warning(
                    "Channel processing capacity stopped target=%d current=%d "
                    "duration_ms=%.1f reason=no_progress",
                    target_count,
                    len(processing),
                    (time.perf_counter() - started_at) * 1000,
                )
                return made_progress

    async def _prepare_pending_candidate(self, item):
        """Fetch and validate one queued post without blocking its batch."""
        try:
            if not await asyncio.to_thread(is_feature_enabled, "ai_search"):
                return None, False
            try:
                source_message = await self.client.get_messages(
                    item["source_channel_id"],
                    ids=item["source_message_id"],
                )
            except MessageIdInvalidError:
                source_message = None
            except _TERMINAL_SOURCE_ERRORS:
                await asyncio.to_thread(
                    delete_channel_post_after_analysis_failure,
                    item["id"],
                )
                logger.info(
                    "Deleted channel post %s/%s because its Telegram source "
                    "is no longer accessible",
                    item["source_channel_id"],
                    item["source_message_id"],
                )
                return None, True
            except Exception:
                # A transport outage is not proof that the source vanished.
                logger.warning("Could not refresh pending source; retaining row", exc_info=True)
                return None, False
            if not source_message:
                await asyncio.to_thread(
                    delete_channel_post_after_analysis_failure,
                    item["id"],
                )
                logger.info(
                    "Deleted channel post %s/%s because its source is missing",
                    item["source_channel_id"],
                    item["source_message_id"],
                )
                return None, True
            try:
                entity = await self.client.get_entity(item["source_channel_id"])
            except _TERMINAL_SOURCE_ERRORS:
                await asyncio.to_thread(
                    delete_channel_post_after_analysis_failure,
                    item["id"],
                )
                logger.info(
                    "Deleted channel post %s/%s because its Telegram channel "
                    "is no longer accessible",
                    item["source_channel_id"],
                    item["source_message_id"],
                )
                return None, True
            candidate = await prepare_channel_message(
                source_message,
                channel_id=item["source_channel_id"],
                channel_username=getattr(entity, "username", None),
                existing_post_id=item["id"],
            )
            if candidate:
                return candidate, False
            if not await asyncio.to_thread(is_feature_enabled, "ai_search"):
                return None, False
            await asyncio.to_thread(
                delete_channel_post_after_analysis_failure,
                item["id"],
            )
            logger.info(
                "Deleted incomplete channel post %s/%s before AI analysis",
                item["source_channel_id"],
                item["source_message_id"],
            )
            return None, True
        except Exception:
            logger.exception(
                "Could not prepare pending channel post %s/%s; retaining it",
                item["source_channel_id"],
                item["source_message_id"],
            )
            return None, False

    async def _pending_retry_loop(self):
        """Retry pending and recoverable channel posts in bounded batches."""
        delay = 0
        while True:
            if delay:
                if not self._retry_wakeup.is_set():
                    try:
                        await asyncio.wait_for(
                            self._retry_wakeup.wait(),
                            timeout=delay,
                        )
                    except asyncio.TimeoutError:
                        pass
                self._retry_wakeup.clear()

            delay = PENDING_RETRY_INTERVAL_SECONDS
            if not await asyncio.to_thread(is_feature_enabled, "ai_search"):
                logger.info(
                    "Channel retry loop paused because AI search is disabled"
                )
                continue
            try:
                queue_status = await asyncio.to_thread(get_channel_queue_status)
                oldest_processing = queue_status["oldest_processing_seconds"]
                log_method = (
                    logger.warning
                    if oldest_processing is not None
                    and oldest_processing >= PROCESSING_STALE_SECONDS
                    else logger.info
                )
                log_method(
                    "Channel queue pending=%d processing=%d processed=%d "
                    "oldest_pending_seconds=%s oldest_processing_seconds=%s%s",
                    queue_status["pending"],
                    queue_status["processing"],
                    queue_status["processed"],
                    queue_status["oldest_pending_seconds"],
                    oldest_processing,
                    (
                        " ALERT=processing_stalled"
                        if oldest_processing is not None
                        and oldest_processing >= PROCESSING_STALE_SECONDS
                        else ""
                    ),
                )
            except Exception:
                logger.exception("Could not read channel queue status")
            try:
                status = await asyncio.to_thread(
                    get_ai_service_status,
                    request_kind="channel",
                )
                recovery_at = status.get("next_recovery_at")
                if status.get("mode") == "local":
                    if recovery_at:
                        reset = datetime.fromisoformat(recovery_at)
                        if reset.tzinfo is None:
                            reset = reset.replace(tzinfo=timezone.utc)
                        delay = max(
                            1,
                            min(
                                PENDING_RETRY_INTERVAL_SECONDS * 5,
                                (reset - datetime.now(timezone.utc)).total_seconds(),
                            ),
                        )
                    else:
                        delay = PENDING_RETRY_INTERVAL_SECONDS * 5
                    logger.info(
                        "Skipping channel retry while all external AI "
                        "providers are unavailable"
                    )
                    continue
                if recovery_at:
                    logger.info(
                        "Continuing channel retry because external AI is "
                        "available; provider recovery scheduled at %s",
                        recovery_at,
                    )
            except Exception:
                logger.exception("Could not read AI recovery status")
            try:
                drained = await self._drain_pending_batches()
            except Exception:
                logger.exception("Channel pending drain failed; retrying later")
                drained = False
            if drained and not self.last_batch_retryable:
                delay = CHANNEL_AI_DRAIN_DELAY_SECONDS
                logger.info(
                    "Channel AI drain cycle completed; continuing backlog drain "
                    "after %ss",
                    delay,
                )

    async def _cleanup_loop(self):
        while True:
            try:
                deleted_rows = await asyncio.to_thread(delete_expired_channel_posts)
                if deleted_rows:
                    logger.info(
                        "Deleted %d channel post(s) older than two days",
                        deleted_rows,
                    )
            except Exception:
                logger.exception("Channel post cleanup failed")
            await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)

    async def _handle_new_message(self, event):
        if not await asyncio.to_thread(is_feature_enabled, "ai_search"):
            logger.info("Ignoring new channel post because AI search is disabled")
            return
        message = event.message
        chat = getattr(event, "chat", None)
        channel_username = getattr(chat, "username", None)
        if channel_username is None:
            chat = await event.get_chat()
            channel_username = getattr(chat, "username", None)
        logger.info(
            "Received new post from channel_id=%s message_id=%s",
            event.chat_id,
            getattr(message, "id", None),
        )
        registered_post = await register_channel_message(
            message,
            channel_id=event.chat_id,
            channel_username=channel_username,
        )
        logger.info(
            "Channel post registered; triggering immediate AI drain "
            "channel_id=%s message_id=%s post_id=%s",
            event.chat_id,
            getattr(message, "id", None),
            (
                registered_post.get("post_id")
                if isinstance(registered_post, dict)
                else registered_post
            ),
        )
        drained = await self._drain_pending_posts()
        logger.info(
            "Channel post immediate AI drain finished channel_id=%s "
            "message_id=%s submitted=%s",
            event.chat_id,
            getattr(message, "id", None),
            drained,
        )

    async def download_photo(self, channel_id, message_id):
        """Download source media when no Bot API file_id is cached yet."""
        if not self.client:
            return None
        if not self.client.is_connected():
            await self.client.connect()
            if not await self.client.is_user_authorized():
                return None
        source_message = await self.client.get_messages(
            channel_id,
            ids=message_id,
        )
        if not source_message:
            return None
        if not (
            getattr(source_message, "photo", None)
            or getattr(source_message, "document", None)
            or getattr(source_message, "media", None)
        ):
            return None
        buffer = io.BytesIO()
        downloaded = await self.client.download_media(source_message, file=buffer)
        if not downloaded or not buffer.tell():
            return None
        buffer.seek(0)
        return buffer

    async def stop(self):
        async with self._stop_lock:
            running_task = self.task
            if self.client and self._handler:
                try:
                    self.client.remove_event_handler(*self._handler)
                except Exception:
                    logger.debug(
                        "Channel monitor handler was already removed",
                        exc_info=True,
                    )
                self._handler = None
            if self.retry_task:
                self.retry_task.cancel()
                try:
                    await self.retry_task
                except asyncio.CancelledError:
                    pass
                self.retry_task = None
            if self.cleanup_task:
                self.cleanup_task.cancel()
                try:
                    await self.cleanup_task
                except asyncio.CancelledError:
                    pass
                self.cleanup_task = None
            if self.client:
                await self.client.disconnect()
                self.client = None
            if (
                running_task
                and running_task is not asyncio.current_task()
                and not running_task.done()
            ):
                try:
                    await running_task
                except asyncio.CancelledError:
                    pass
            self.task = None
            self.entities = []

    def _listener_done(self, task):
        if self.task is not task:
            return
        if not self._teardown_task or self._teardown_task.done():
            self._teardown_task = asyncio.create_task(
                self._teardown_finished_listener(task)
            )

    async def _teardown_finished_listener(self, listener_task):
        try:
            if self.task is listener_task:
                await self.stop()
        finally:
            self._teardown_task = None


def _channel_reference(link):
    """Turn a stored public channel URL into a Telethon username reference."""
    value = (link or "").strip()
    try:
        parsed = urlparse(value)
    except ValueError:
        return value
    if parsed.netloc.lower() in {"t.me", "www.t.me", "telegram.me"}:
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 2 and parts[0] == "c" and parts[1].isdigit():
            return PeerChannel(int(parts[1]))
        if parts and parts[0] not in {"c", "joinchat", "+", "addlist"}:
            return parts[0].lstrip("@")
    return value
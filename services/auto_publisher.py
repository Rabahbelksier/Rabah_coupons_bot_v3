"""Background delivery of repeated products to the configured Telegram channel."""

import asyncio
import logging
from datetime import datetime, timezone
from types import SimpleNamespace

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from config import AUTO_PUBLISH_CHANNEL_LINK
from core.db import (
    claim_auto_publish_delivery,
    get_processed_channel_posts_for_auto_publish,
    is_feature_enabled,
    mark_auto_publish_delivery_failed,
    mark_auto_publish_delivery_published,
    product_identity,
    save_channel_photo_file_id,
)
from handlers.ai_search import (
    _caption_for_content,
    _load_post_photo,
    _photo_to_input_file,
    _post_content,
    _send_post_photo_with_fallback,
    _should_cache_resolved_photo,
)
from utils.telegram import telegram_chat_id as _target_chat_id

logger = logging.getLogger(__name__)

REPEAT_THRESHOLD = 5
PUBLISH_INTERVAL_SECONDS = 60
STALE_DELIVERY_RETRY_MESSAGE = (
    "Automatic publication was claimed but did not complete; it will be retried."
)


def _publication_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "تطبيق Offers 365",
            url="https://offres365page.up.railway.app/",
        ),
        InlineKeyboardButton(
            "بوت الذكاء الاصطناعي",
            url="https://t.me/rabahcoupons1bot",
        ),
    ]])


def _post_recency_key(post):
    published_at = post.get("published_at")
    if isinstance(published_at, str):
        try:
            published_at = datetime.fromisoformat(
                published_at.replace("Z", "+00:00")
            )
        except ValueError:
            published_at = None
    if isinstance(published_at, datetime):
        if published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=timezone.utc)
        timestamp = published_at.timestamp()
    else:
        timestamp = float("-inf")

    try:
        post_id = int(post.get("id") or 0)
    except (TypeError, ValueError):
        post_id = 0
    return timestamp, post_id


def _cheapest_post(posts):
    def price_key(post):
        price = post.get("price")
        return (
            price is None,
            float(price) if price is not None else 0,
        )

    lowest_price = min(price_key(post) for post in posts)
    cheapest_posts = [
        post for post in posts if price_key(post) == lowest_price
    ]
    return max(cheapest_posts, key=_post_recency_key)


def _due_publications(posts):
    """Return each newly reached 5, 10, 15... threshold and its best offer."""
    grouped = {}
    for post in posts:
        grouped.setdefault(product_identity(post), []).append(post)

    due = []
    for product_key, product_posts in grouped.items():
        occurrence_count = len(product_posts)
        for threshold in range(
            REPEAT_THRESHOLD,
            occurrence_count + 1,
            REPEAT_THRESHOLD,
        ):
            due.append((
                product_key,
                threshold,
                _cheapest_post(product_posts),
            ))
    return due


class AutoPublisher:
    """Own one lightweight task and never block Telegram update handling."""

    def __init__(self, logger_=logger):
        self._logger = logger_
        self._task = None
        self._stop_event = None
        self._application = None

    async def start(self, application):
        if self._task and not self._task.done():
            return
        self._application = application
        self._stop_event = asyncio.Event()
        if not AUTO_PUBLISH_CHANNEL_LINK:
            self._logger.info(
                "Automatic publication is disabled because "
                "AUTO_PUBLISH_CHANNEL_LINK is empty"
            )
            return
        self._task = asyncio.create_task(
            self._run_loop(),
            name="telegram-auto-publisher",
        )
        self._logger.info("Automatic channel publisher started")

    async def _run_loop(self):
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._logger.exception("Automatic publication cycle failed")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=PUBLISH_INTERVAL_SECONDS,
                )
                return
            except asyncio.TimeoutError:
                continue

    async def run_once(self):
        if not self._application or not AUTO_PUBLISH_CHANNEL_LINK:
            return
        if not await asyncio.to_thread(is_feature_enabled, "auto_publish"):
            return

        posts = await asyncio.to_thread(
            get_processed_channel_posts_for_auto_publish,
        )
        for product_key, threshold, post in _due_publications(posts):
            delivery_id = await asyncio.to_thread(
                claim_auto_publish_delivery,
                product_key,
                threshold,
                post["id"],
            )
            if not delivery_id:
                continue
            try:
                sent = await self._publish_post(post)
                await asyncio.to_thread(
                    mark_auto_publish_delivery_published,
                    delivery_id,
                    sent.message_id,
                )
                self._logger.info(
                    "Published repeated product %s at threshold %s",
                    product_key,
                    threshold,
                )
            except Exception:
                self._logger.exception(
                    "Could not publish repeated product %s at threshold %s",
                    product_key,
                    threshold,
                )
                try:
                    await asyncio.to_thread(
                        mark_auto_publish_delivery_failed,
                        delivery_id,
                    )
                except Exception:
                    self._logger.exception(
                        "Could not release failed automatic publication claim"
                    )

    async def _publish_post(self, post):
        chat_id = _target_chat_id(AUTO_PUBLISH_CHANNEL_LINK)
        if not chat_id:
            raise RuntimeError("AUTO_PUBLISH_CHANNEL_LINK is invalid")

        context = SimpleNamespace(
            application=self._application,
        )
        content = _post_content(post)
        sent = await _send_post_photo_with_fallback(
            self._application.bot,
            chat_id,
            context,
            post,
            content,
            _publication_keyboard(),
        )
        if not sent:
            raise RuntimeError("The repeated offer has no usable image")
        if len(content) > 1024:
            await self._application.bot.send_message(
                chat_id=chat_id,
                text=content,
            )
        return sent

    async def _send_photo(self, chat_id, post, photo, downloaded_source):
        sent = await self._application.bot.send_photo(
            chat_id=chat_id,
            photo=_photo_to_input_file(photo, post, downloaded_source),
            caption=_caption_for_content(_post_content(post)),
            reply_markup=_publication_keyboard(),
        )
        if (
            getattr(sent, "photo", None)
            and _should_cache_resolved_photo(post, downloaded_source)
        ):
            await asyncio.to_thread(
                save_channel_photo_file_id,
                post["id"],
                sent.photo[-1].file_id,
            )
        return sent

    def stop(self):
        task = self._task
        if self._stop_event:
            self._stop_event.set()
        if task and not task.done():
            loop = task.get_loop()
            if not loop.is_closed():
                loop.call_soon_threadsafe(task.cancel)
        self._task = None
        self._application = None

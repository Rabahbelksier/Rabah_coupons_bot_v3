import asyncio
import logging
from collections import deque

from services.worker import (
    process_link_for_user,
    send_reply_keyboard_anchor,
)
from utils.log_context import current_log_context, log_context, traced_operation

logger = logging.getLogger(__name__)

user_queues: dict = {}
active_workers: set = set()
_queued_log_contexts: dict = {}


@traced_operation("queue.worker")
async def user_queue_worker(chat_id: int, context):
    queue = user_queues.get(chat_id)
    if queue is None:
        active_workers.discard(chat_id)
        return
    reply_keyboard_anchor = await send_reply_keyboard_anchor(chat_id, context)
    try:
        while not queue.empty():
            url = queue.get_nowait()
            diagnostics = _queued_log_contexts.get(chat_id)
            fields = diagnostics.popleft() if diagnostics else {}
            fields.pop("operation", None)
            with log_context(
                "queue.product", new_trace=not bool(fields), **fields
            ):
                logger.info("Queued product processing started")
                anchor_removed = await process_link_for_user(
                    chat_id,
                    url,
                    context,
                    reply_keyboard_anchor=reply_keyboard_anchor,
                )
                logger.info("Queued product processing finished")
            if anchor_removed:
                reply_keyboard_anchor = None
            queue.task_done()
    finally:
        active_workers.discard(chat_id)
        if chat_id in user_queues and user_queues[chat_id].empty():
            del user_queues[chat_id]
            _queued_log_contexts.pop(chat_id, None)


async def enqueue_url(chat_id: int, url: str, context):
    if chat_id not in user_queues:
        user_queues[chat_id] = asyncio.Queue()
        _queued_log_contexts[chat_id] = deque()

    await user_queues[chat_id].put(url)
    _queued_log_contexts.setdefault(chat_id, deque()).append(current_log_context())
    logger.info("Product queued queue_depth=%s", user_queues[chat_id].qsize())

    if chat_id not in active_workers:
        active_workers.add(chat_id)
        asyncio.create_task(user_queue_worker(chat_id, context))

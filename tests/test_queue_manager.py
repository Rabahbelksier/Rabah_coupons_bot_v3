import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram import ReplyKeyboardMarkup

from handlers.ai_search import AI_SEARCH_BUTTON_TEXT
from services.queue_manager import (
    active_workers,
    user_queues,
    user_queue_worker,
)


class QueueWorkerKeyboardTests(unittest.IsolatedAsyncioTestCase):
    chat_id = 123

    async def asyncSetUp(self):
        user_queues.pop(self.chat_id, None)
        active_workers.discard(self.chat_id)

    async def asyncTearDown(self):
        user_queues.pop(self.chat_id, None)
        active_workers.discard(self.chat_id)

    async def test_reply_keyboard_anchor_is_sent_before_queue_processing(self):
        events = []

        queue = asyncio.Queue()
        anchor_message = SimpleNamespace(message_id=90)
        urls = [
            "https://www.aliexpress.com/item/first.html",
            "https://www.aliexpress.com/item/second.html",
        ]
        for url in urls:
            queue.put_nowait(url)
        user_queues[self.chat_id] = queue
        active_workers.add(self.chat_id)

        async def record_anchor(**_kwargs):
            events.append("anchor")
            return anchor_message

        context = SimpleNamespace(
            bot=SimpleNamespace(
                send_message=AsyncMock(side_effect=record_anchor),
            ),
            user_data={},
        )

        process_anchors = []

        async def record_processing(*_args, **kwargs):
            events.append("process")
            process_anchors.append(kwargs["reply_keyboard_anchor"])
            return len(process_anchors) == 1

        with patch(
            "services.queue_manager.process_link_for_user",
            new=AsyncMock(side_effect=record_processing),
        ) as process_link:
            await user_queue_worker(self.chat_id, context)

        self.assertEqual(events, ["anchor", "process", "process"])
        self.assertEqual(
            [call.args[1] for call in process_link.await_args_list],
            urls,
        )
        self.assertEqual(process_anchors, [anchor_message, None])
        context.bot.send_message.assert_awaited_once()
        anchor = context.bot.send_message.await_args.kwargs
        self.assertEqual(anchor["chat_id"], self.chat_id)
        self.assertEqual(anchor["text"], "\u2063")
        self.assertIsInstance(anchor["reply_markup"], ReplyKeyboardMarkup)
        self.assertIsNone(anchor["reply_markup"].is_persistent)
        self.assertIsNone(anchor["reply_markup"].one_time_keyboard)
        self.assertEqual(
            anchor["reply_markup"].keyboard[0][0].text,
            AI_SEARCH_BUTTON_TEXT,
        )
        self.assertNotIn(self.chat_id, user_queues)
        self.assertNotIn(self.chat_id, active_workers)

    async def test_keyboard_anchor_is_not_sent_if_ai_mode_was_reopened(self):
        queue = asyncio.Queue()
        queue.put_nowait("https://www.aliexpress.com/item/first.html")
        user_queues[self.chat_id] = queue
        active_workers.add(self.chat_id)
        context = SimpleNamespace(
            bot=SimpleNamespace(send_message=AsyncMock()),
            user_data={"ai_search_active": True},
        )

        with patch(
            "services.queue_manager.process_link_for_user",
            new=AsyncMock(),
        ):
            await user_queue_worker(self.chat_id, context)

        context.bot.send_message.assert_not_awaited()

    async def test_anchor_is_reused_until_a_successful_offer_removes_it(self):
        queue = asyncio.Queue()
        urls = [
            "https://www.aliexpress.com/item/first.html",
            "https://www.aliexpress.com/item/second.html",
            "https://www.aliexpress.com/item/third.html",
        ]
        for url in urls:
            queue.put_nowait(url)
        user_queues[self.chat_id] = queue
        active_workers.add(self.chat_id)
        anchor_message = SimpleNamespace(message_id=90)
        context = SimpleNamespace(
            bot=SimpleNamespace(
                send_message=AsyncMock(return_value=anchor_message),
            ),
            user_data={},
        )
        results = iter([False, True, True])

        with patch(
            "services.queue_manager.process_link_for_user",
            new=AsyncMock(side_effect=lambda *_args, **_kwargs: next(results)),
        ) as process_link:
            await user_queue_worker(self.chat_id, context)

        self.assertEqual(
            [
                call.kwargs["reply_keyboard_anchor"]
                for call in process_link.await_args_list
            ],
            [anchor_message, anchor_message, None],
        )
        context.bot.send_message.assert_awaited_once()
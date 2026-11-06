import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from handlers.ai_search import AI_SEARCH_BUTTON_TEXT, ai_search_reply_keyboard
from handlers.help import (
    AI_SEARCH_INFO_TEXT,
    HELP_MENU_TEXT,
    HELP_AI_INFO_CALLBACK,
    MORE_DETAILS_BUTTON_TEXT,
    START_TEXT,
    help_keyboard,
    help_command,
    help_topic_callback,
)
from handlers.start import (
    send_pending_telegram_start_events,
    start,
    start_ai_callback,
)


class HelpAndStartTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_sends_only_start_message_with_details_button(self):
        reply_text = AsyncMock()
        update = SimpleNamespace(
            effective_user=SimpleNamespace(
                id=123,
                first_name="Test",
                last_name=None,
            ),
            message=SimpleNamespace(reply_text=reply_text),
        )
        context = SimpleNamespace(user_data={}, args=[])

        with patch("handlers.start.save_user"):
            await start(update, context)

        reply_text.assert_awaited_once()
        self.assertEqual(reply_text.await_args.args[0], START_TEXT)
        keyboard = reply_text.await_args.kwargs["reply_markup"]
        labels = [
            button.text
            for row in keyboard.inline_keyboard
            for button in row
        ]
        self.assertIn(MORE_DETAILS_BUTTON_TEXT, labels)

    async def test_start_saves_matching_payload_and_dispatches_pending_events(self):
        reply_text = AsyncMock()
        update = SimpleNamespace(
            effective_user=SimpleNamespace(
                id=456,
                first_name="New",
                last_name="User",
            ),
            message=SimpleNamespace(reply_text=reply_text),
        )
        context = SimpleNamespace(user_data={}, args=["campaign-1"])
        pending = [{"id": 9, "chat_id": 456, "attribution_id": "campaign-1"}]

        with (
            patch("handlers.start.META_ATTRIBUTION_ID", "campaign-1"),
            patch("handlers.start.save_user", return_value=pending) as save_user,
            patch(
                "handlers.start.asyncio.to_thread",
                new=AsyncMock(),
            ) as to_thread,
        ):
            await start(update, context)

        save_user.assert_called_once_with(
            456,
            "New",
            "User",
            attribution_id="campaign-1",
            expected_attribution_id="campaign-1",
        )
        to_thread.assert_awaited_once_with(
            send_pending_telegram_start_events,
            pending,
        )
        reply_text.assert_awaited_once()

    async def test_start_replies_before_meta_delivery_finishes(self):
        order = []
        release_meta_delivery = asyncio.Event()

        async def reply_text(*_args, **_kwargs):
            order.append("reply")
            release_meta_delivery.set()

        async def delayed_meta_delivery(*_args, **_kwargs):
            order.append("meta-started")
            await release_meta_delivery.wait()
            order.append("meta-finished")

        update = SimpleNamespace(
            effective_user=SimpleNamespace(
                id=654,
                first_name="Test",
                last_name="User",
            ),
            message=SimpleNamespace(reply_text=reply_text),
        )
        context = SimpleNamespace(user_data={}, args=["campaign-1"])

        with (
            patch("handlers.start.META_ATTRIBUTION_ID", "campaign-1"),
            patch(
                "handlers.start.save_user",
                return_value=[{
                    "id": 10,
                    "chat_id": 654,
                    "attribution_id": "campaign-1",
                }],
            ),
            patch(
                "handlers.start.asyncio.to_thread",
                side_effect=delayed_meta_delivery,
            ),
        ):
            await start(update, context)

        self.assertLess(order.index("reply"), order.index("meta-finished"))

    async def test_start_does_not_pass_multiword_payload_as_attribution(self):
        reply_text = AsyncMock()
        update = SimpleNamespace(
            effective_user=SimpleNamespace(
                id=789,
                first_name="Existing",
                last_name=None,
            ),
            message=SimpleNamespace(reply_text=reply_text),
        )
        context = SimpleNamespace(user_data={}, args=["campaign-1", "extra"])

        with patch("handlers.start.save_user") as save_user:
            await start(update, context)

        self.assertIsNone(save_user.call_args.kwargs["attribution_id"])

    async def test_help_command_sends_shared_help_menu(self):
        reply_text = AsyncMock()
        update = SimpleNamespace(
            message=SimpleNamespace(reply_text=reply_text),
        )

        await help_command(update, SimpleNamespace())

        reply_text.assert_awaited_once()
        self.assertEqual(reply_text.await_args.args[0], HELP_MENU_TEXT)
        self.assertIsNotNone(reply_text.await_args.kwargs["reply_markup"])

    async def test_help_ai_button_sends_info_without_entering_ai_mode(self):
        reply_text = AsyncMock()
        query = SimpleNamespace(
            data=HELP_AI_INFO_CALLBACK,
            answer=AsyncMock(),
            message=SimpleNamespace(reply_text=reply_text),
        )
        context = SimpleNamespace(user_data={})

        await help_topic_callback(
            SimpleNamespace(callback_query=query),
            context,
        )

        query.answer.assert_awaited_once()
        reply_text.assert_awaited_once_with(AI_SEARCH_INFO_TEXT)
        self.assertEqual(context.user_data, {})

    def test_help_ai_button_uses_a_dedicated_callback(self):
        keyboard = help_keyboard()
        ai_button = keyboard.inline_keyboard[2][0]

        self.assertEqual(ai_button.callback_data, HELP_AI_INFO_CALLBACK)

    async def test_start_ai_button_still_enters_ai_mode(self):
        query = SimpleNamespace(
            answer=AsyncMock(),
            message=SimpleNamespace(),
        )
        context = SimpleNamespace(user_data={})

        with patch(
            "handlers.ai_search._activate_ai_search",
            new=AsyncMock(),
        ) as activate_ai:
            await start_ai_callback(
                SimpleNamespace(callback_query=query),
                context,
            )

        query.answer.assert_awaited_once()
        activate_ai.assert_awaited_once_with(query.message, context)

    def test_ai_search_reply_keyboard_has_only_ai_entry_button(self):
        keyboard = ai_search_reply_keyboard()
        labels = [
            button.text
            for row in keyboard.keyboard
            for button in row
        ]

        self.assertEqual(labels, [AI_SEARCH_BUTTON_TEXT])
        self.assertNotIn(MORE_DETAILS_BUTTON_TEXT, labels)
        self.assertIsNone(keyboard.is_persistent)
        self.assertIsNone(keyboard.one_time_keyboard)


if __name__ == "__main__":
    unittest.main()
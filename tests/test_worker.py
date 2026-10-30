import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from services.worker import process_link_for_user


class WorkerKeyboardTests(unittest.IsolatedAsyncioTestCase):
    async def test_loading_message_does_not_reopen_reply_keyboard(self):
        loading = SimpleNamespace(message_id=1)
        result = SimpleNamespace(photo=[])
        bot = SimpleNamespace(
            send_message=AsyncMock(return_value=loading),
            send_photo=AsyncMock(return_value=result),
            delete_message=AsyncMock(),
        )
        context = SimpleNamespace(
            bot=bot,
            user_data={},
        )

        with patch(
            "services.worker.extract_product_id",
            return_value="100500123",
        ), patch(
            "services.worker.get_product_info_from_api",
            return_value={"title": "هاتف", "image_url": None},
        ), patch(
            "services.worker.generate_affiliate_links",
            return_value=["عرض"],
        ), patch(
            "services.worker.is_feature_enabled",
            return_value=True,
        ):
            await process_link_for_user(
                123,
                "https://www.aliexpress.com/item/100500123.html",
                context,
            )

        loading_call = bot.send_message.await_args_list[0]
        self.assertIsNone(loading_call.kwargs.get("reply_markup"))
        self.assertEqual(bot.send_message.await_count, 2)

    async def test_processing_error_does_not_reopen_reply_keyboard(self):
        loading = SimpleNamespace(message_id=1)
        error = SimpleNamespace(message_id=2)
        anchor = SimpleNamespace(message_id=90)
        bot = SimpleNamespace(
            send_message=AsyncMock(
                side_effect=[loading, error],
            ),
            delete_message=AsyncMock(),
        )
        context = SimpleNamespace(
            bot=bot,
            user_data={},
        )

        with patch(
            "services.worker.extract_product_id",
            return_value="100500123",
        ), patch(
            "services.worker.get_product_info_from_api",
            side_effect=RuntimeError("temporary API failure"),
        ), patch(
            "services.worker.generate_affiliate_links",
            return_value=[],
        ):
            await process_link_for_user(
                123,
                "https://www.aliexpress.com/item/100500123.html",
                context,
                reply_keyboard_anchor=anchor,
            )

        self.assertEqual(len(bot.send_message.await_args_list), 2)
        loading_call = bot.send_message.await_args_list[0]
        self.assertIsNone(loading_call.kwargs.get("reply_markup"))
        refresh_call = bot.send_message.await_args_list[-1]
        self.assertIsNone(refresh_call.kwargs.get("reply_markup"))
        self.assertEqual(
            refresh_call.kwargs["text"],
            "❌ حدث خطأ أثناء معالجة طلبك، اتصل بالأدمن @Rabahbelksier",
        )
        bot.delete_message.assert_awaited_once_with(
            chat_id=123,
            message_id=loading.message_id,
        )

    async def test_does_not_show_ai_keyboard_while_ai_mode_is_active(self):
        loading = SimpleNamespace(message_id=1)
        error = SimpleNamespace(message_id=2)
        bot = SimpleNamespace(
            send_message=AsyncMock(
                side_effect=[loading, error],
            ),
            delete_message=AsyncMock(),
        )
        context = SimpleNamespace(
            bot=bot,
            user_data={"ai_search_active": True},
        )

        with patch(
            "services.worker.extract_product_id",
            return_value="100500123",
        ), patch(
            "services.worker.get_product_info_from_api",
            side_effect=RuntimeError("temporary API failure"),
        ), patch(
            "services.worker.generate_affiliate_links",
            return_value=[],
        ):
            await process_link_for_user(
                123,
                "https://www.aliexpress.com/item/100500123.html",
                context,
            )

        self.assertIsNone(
            bot.send_message.await_args_list[0].kwargs.get("reply_markup")
        )
        self.assertIsNone(
            bot.send_message.await_args_list[-1].kwargs.get("reply_markup")
        )

    async def test_sends_only_the_offer_after_successful_link_processing(self):
        loading = SimpleNamespace(message_id=1)
        result = SimpleNamespace(photo=[SimpleNamespace(file_id="photo-id")])
        bot = SimpleNamespace(
            send_message=AsyncMock(return_value=loading),
            send_photo=AsyncMock(return_value=result),
            delete_message=AsyncMock(),
        )
        context = SimpleNamespace(
            bot=bot,
            user_data={},
        )

        with patch(
            "services.worker.extract_product_id",
            return_value="100500123",
        ), patch(
            "services.worker.get_product_info_from_api",
            return_value={"title": "هاتف", "image_url": "https://example.com/phone.jpg"},
        ), patch(
            "services.worker.generate_affiliate_links",
            return_value=["عرض"],
        ), patch(
            "services.worker.is_feature_enabled",
            return_value=True,
        ):
            await process_link_for_user(
                123,
                "https://www.aliexpress.com/item/100500123.html",
                context,
            )

        self.assertEqual(bot.send_message.await_count, 1)
        bot.send_photo.assert_awaited_once()
        loading_call = bot.send_message.await_args_list[0]
        self.assertIsNone(loading_call.kwargs.get("reply_markup"))
        bot.delete_message.assert_awaited_once_with(
            chat_id=123,
            message_id=loading.message_id,
        )

    async def test_photo_offer_deletes_anchor_before_loading_message_cleanup(self):
        events = []
        loading = SimpleNamespace(message_id=1)
        anchor = SimpleNamespace(message_id=90)
        offer = SimpleNamespace(photo=[SimpleNamespace(file_id="photo-id")])

        async def send_message(**kwargs):
            events.append(("send_message", kwargs["text"]))
            return loading

        async def send_photo(**_kwargs):
            events.append(("send_photo", None))
            return offer

        async def delete_message(**kwargs):
            events.append(("delete", kwargs["message_id"]))

        bot = SimpleNamespace(
            send_message=AsyncMock(side_effect=send_message),
            send_photo=AsyncMock(side_effect=send_photo),
            delete_message=AsyncMock(side_effect=delete_message),
        )
        context = SimpleNamespace(bot=bot, user_data={})

        with patch(
            "services.worker.extract_product_id",
            return_value="100500123",
        ), patch(
            "services.worker.get_product_info_from_api",
            return_value={
                "title": "هاتف",
                "image_url": "https://example.com/phone.jpg",
            },
        ), patch(
            "services.worker.generate_affiliate_links",
            return_value=["عرض"],
        ), patch(
            "services.worker.is_feature_enabled",
            return_value=True,
        ):
            anchor_removed = await process_link_for_user(
                123,
                "https://www.aliexpress.com/item/100500123.html",
                context,
                reply_keyboard_anchor=anchor,
            )

        self.assertTrue(anchor_removed)
        bot.send_photo.assert_awaited_once()
        self.assertEqual(
            events,
            [
                ("send_message", "⏳ جاري البحث عن العروض"),
                ("send_photo", None),
                ("delete", anchor.message_id),
                ("delete", loading.message_id),
            ],
        )

    async def test_text_offer_also_removes_anchor_after_successful_send(self):
        events = []
        loading = SimpleNamespace(message_id=1)
        anchor = SimpleNamespace(message_id=90)

        async def send_message(**kwargs):
            text = kwargs["text"]
            events.append(("send_message", text))
            if text == "⏳ جاري البحث عن العروض":
                return loading
            return SimpleNamespace(message_id=2)

        async def delete_message(**kwargs):
            events.append(("delete", kwargs["message_id"]))

        bot = SimpleNamespace(
            send_message=AsyncMock(side_effect=send_message),
            send_photo=AsyncMock(),
            delete_message=AsyncMock(side_effect=delete_message),
        )
        context = SimpleNamespace(bot=bot, user_data={})

        with patch(
            "services.worker.extract_product_id",
            return_value="100500123",
        ), patch(
            "services.worker.get_product_info_from_api",
            return_value={"title": "هاتف", "image_url": None},
        ), patch(
            "services.worker.generate_affiliate_links",
            return_value=["عرض"],
        ), patch(
            "services.worker.is_feature_enabled",
            return_value=True,
        ):
            anchor_removed = await process_link_for_user(
                123,
                "https://www.aliexpress.com/item/100500123.html",
                context,
                reply_keyboard_anchor=anchor,
            )

        self.assertTrue(anchor_removed)
        self.assertEqual(
            [event[0] for event in events],
            ["send_message", "send_message", "delete", "delete"],
        )
        self.assertEqual(
            events[2:],
            [
                ("delete", anchor.message_id),
                ("delete", loading.message_id),
            ],
        )

    async def test_anchor_delete_failure_does_not_interrupt_offer_delivery(self):
        loading = SimpleNamespace(message_id=1)
        anchor = SimpleNamespace(message_id=90)

        async def send_message(**kwargs):
            if kwargs["text"] == "⏳ جاري البحث عن العروض":
                return loading
            return SimpleNamespace(message_id=2)

        async def delete_message(**kwargs):
            if kwargs["message_id"] == anchor.message_id:
                raise RuntimeError("temporary Telegram error")

        bot = SimpleNamespace(
            send_message=AsyncMock(side_effect=send_message),
            send_photo=AsyncMock(),
            delete_message=AsyncMock(side_effect=delete_message),
        )
        context = SimpleNamespace(bot=bot, user_data={})

        with patch(
            "services.worker.extract_product_id",
            return_value="100500123",
        ), patch(
            "services.worker.get_product_info_from_api",
            return_value={"title": "هاتف", "image_url": None},
        ), patch(
            "services.worker.generate_affiliate_links",
            return_value=["عرض"],
        ), patch(
            "services.worker.is_feature_enabled",
            return_value=True,
        ):
            anchor_removed = await process_link_for_user(
                123,
                "https://www.aliexpress.com/item/100500123.html",
                context,
                reply_keyboard_anchor=anchor,
            )

        self.assertFalse(anchor_removed)
        self.assertEqual(bot.send_message.await_count, 2)
        self.assertEqual(bot.delete_message.await_count, 2)
        self.assertEqual(
            bot.delete_message.await_args_list[-1].kwargs["message_id"],
            loading.message_id,
        )

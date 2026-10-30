import asyncio
import time
import unittest
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from telegram import (
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

import core.db as db
from core.categories import (
    SAVED_OFFER_CATEGORY_FILTERS,
    SAVED_OFFER_CATEGORY_GROUPS,
    SAVED_OFFER_CATEGORY_GROUPS_BY_KEY,
    SAVED_OFFER_CATEGORY_SUBCATEGORY_NAMES,
    saved_offer_filter_from_subcategory,
)
from handlers.messages import handle_message
from handlers.ai_search import (
    AI_SEARCH_BUTTON_TEXT,
    AI_SEARCH_HELP_TEXT,
    AI_SEARCH_NO_RESULTS_TEXT,
    AI_SEARCH_SESSION_EXPIRED_TEXT,
    AI_SEARCH_SUPPORTED_TEXT,
    SAVED_OFFERS_CATEGORY_TEXT,
    SAVED_OFFERS_BUTTON_TEXT,
    SAVED_OFFERS_MAIN_BUTTONS,
    SAVED_OFFERS_MENU_TEXT,
    SAVED_OFFERS_PHONE_BRANDS,
    SAVED_OFFERS_PHONE_TEXT,
    SAVED_OFFERS_BACK_BUTTON_TEXT,
    _clear_ai_search_state,
    _saved_offer_intent,
    _saved_offers_main_markup,
    ask_ai_callback,
    _navigation_keyboard,
    _load_post_photo,
    _replace_stored_post_message,
    ai_next_callback,
    ai_previous_callback,
    handle_ai_search_button_message,
    handle_ai_search_message,
    handle_saved_offers_reply_message,
    saved_offers_callback,
    send_stored_post,
)


class FakeMessage:
    def __init__(self, text=None):
        self.text = text
        self.replies = []
        self.chat = SimpleNamespace(id=123)

    async def reply_text(self, text, reply_markup=None):
        self.replies.append((text, reply_markup))


class FakeContext:
    def __init__(self):
        self.user_data = {}
        self.application = SimpleNamespace(bot_data={})
        self.bot = None


class KeyboardTraceMessage:
    def __init__(self, text=None, events=None):
        self.text = text
        self.chat = SimpleNamespace(id=123)
        self.message_id = 1
        self._next_message_id = 2
        self.replies = []
        self.events = events if events is not None else []

    async def reply_text(self, text, reply_markup=None):
        self.replies.append((text, reply_markup))
        self.events.append(("reply", text, reply_markup))
        sent = SimpleNamespace(
            message_id=self._next_message_id,
            chat=self.chat,
        )
        self._next_message_id += 1
        return sent


class AiSearchTests(unittest.IsolatedAsyncioTestCase):
    @patch("core.ai._call_ai")
    def test_equivalent_phone_price_queries_use_provider_canonical_request(
        self,
        call_ai,
    ):
        from core.ai import parse_user_request
        from core.ai_service import AIResponse

        canonical_request = "عروض هواتف بسعر أقل من 100 دولار"
        parsed_intent = {
            "canonical_request": canonical_request,
            "request_type": "category_price_range",
            "keywords": [],
            "category": "phones",
            "max_price": 100,
            "price_mode": "below",
            "category_filter_requested": True,
        }
        call_ai.side_effect = [
            AIResponse(parsed_intent, provider="Gemini"),
            AIResponse(parsed_intent, provider="Gemini"),
        ]

        first = parse_user_request("عروض الهواتف الاقل من 100$")
        second = parse_user_request("عروض هواتف بسعر اقل من 100$")

        self.assertEqual(dict(first), dict(second))
        self.assertEqual(first["request_type"], "category_price_range")
        self.assertEqual(first["category"], "phones")
        self.assertEqual(first["price_mode"], "below")
        self.assertEqual(first["max_price"], 100)
        self.assertFalse(first.get("no_results", False))
        for call in call_ai.call_args_list:
            prompt = call.args[0]
            self.assertIn('"canonical_request": ""', prompt)
            self.assertIn("وحّد صياغة الطلب دلاليًا", prompt)

    async def test_saved_offer_button_labels_are_normal_ai_search_text(self):
        button_cases = [
            (SAVED_OFFERS_BUTTON_TEXT, "main"),
            *((label, "main") for label in SAVED_OFFERS_MAIN_BUTTONS),
            (SAVED_OFFERS_BACK_BUTTON_TEXT, "phones"),
            (SAVED_OFFERS_BACK_BUTTON_TEXT, "categories"),
            *((brand, "phones") for brand in SAVED_OFFERS_PHONE_BRANDS),
            ("realme", "phones"),
            *(
                (label, "categories")
                for _category, label in SAVED_OFFER_CATEGORY_FILTERS
            ),
        ]

        for text, menu in button_cases:
            with self.subTest(text=text, menu=menu):
                context = FakeContext()
                context.user_data.update({
                    "ai_search_active": True,
                    "saved_offers_menu": menu,
                })
                message = FakeMessage(text)
                update = SimpleNamespace(
                    message=message,
                    effective_message=message,
                    effective_user=SimpleNamespace(
                        first_name="Test",
                        last_name="User",
                    ),
                    effective_chat=message.chat,
                )
                intent = {
                    "request_type": "product_best",
                    "keywords": [text.casefold()],
                }
                posts = [{"id": 1, "title": text}]

                with patch(
                    "handlers.messages.handle_admin_message",
                    new=AsyncMock(return_value=False),
                ), patch(
                    "handlers.messages.record_user_activity",
                ), patch(
                    "handlers.messages.handle_smart_cart_message",
                    new=AsyncMock(return_value=False),
                ), patch(
                    "handlers.ai_search.is_feature_enabled",
                    return_value=True,
                ), patch(
                    "handlers.ai_search.parse_user_request",
                    return_value=intent,
                ) as parse_request, patch(
                    "handlers.ai_search.search_channel_posts",
                    return_value=posts,
                ) as search_posts, patch(
                    "handlers.ai_search.send_stored_post",
                    new=AsyncMock(),
                ) as send_post:
                    await handle_message(update, context)

                parse_request.assert_called_once_with(text)
                search_posts.assert_called_once_with(intent)
                send_post.assert_awaited_once()
                self.assertTrue(context.user_data["ai_search_active"])

    async def test_natural_ai_text_reaches_search_through_message_handler(self):
        context = FakeContext()
        context.user_data["ai_search_active"] = True
        message = FakeMessage("أرخص هاتف Samsung S24")
        update = SimpleNamespace(
            message=message,
            effective_message=message,
            effective_user=SimpleNamespace(first_name="Test", last_name="User"),
            effective_chat=message.chat,
        )
        intent = {
            "request_type": "product_cheapest",
            "keywords": ["Samsung S24"],
        }
        posts = [{"id": 1, "title": "Samsung S24"}]

        with patch(
            "handlers.messages.handle_admin_message",
            new=AsyncMock(return_value=False),
        ), patch(
            "handlers.messages.record_user_activity",
        ), patch(
            "handlers.messages.handle_smart_cart_message",
            new=AsyncMock(return_value=False),
        ), patch(
            "handlers.ai_search.is_feature_enabled",
            return_value=True,
        ), patch(
            "handlers.ai_search.parse_user_request",
            return_value=intent,
        ) as parse_request, patch(
            "handlers.ai_search.search_channel_posts",
            return_value=posts,
        ) as search_posts, patch(
            "handlers.ai_search.send_stored_post",
            new=AsyncMock(),
        ) as send_post:
            await handle_message(update, context)

        parse_request.assert_called_once_with(message.text)
        search_posts.assert_called_once_with(intent)
        send_post.assert_awaited_once_with(
            123,
            context,
            posts[0],
            has_previous=False,
            has_next=False,
        )

    async def test_saved_offer_button_names_are_not_searches_outside_ai_mode(self):
        button_texts = [
            *SAVED_OFFERS_MAIN_BUTTONS,
            SAVED_OFFERS_BACK_BUTTON_TEXT,
            *SAVED_OFFERS_PHONE_BRANDS,
            *[label for _category, label in SAVED_OFFER_CATEGORY_FILTERS],
        ]

        for text in button_texts:
            with self.subTest(text=text):
                context = FakeContext()
                message = FakeMessage(text)
                update = SimpleNamespace(
                    message=message,
                    effective_message=message,
                    effective_user=SimpleNamespace(
                        first_name="Test",
                        last_name="User",
                    ),
                    effective_chat=message.chat,
                )

                with patch(
                    "handlers.messages.handle_admin_message",
                    new=AsyncMock(return_value=False),
                ), patch(
                    "handlers.messages.record_user_activity",
                ), patch(
                    "handlers.messages.handle_smart_cart_message",
                    new=AsyncMock(return_value=False),
                ), patch(
                    "handlers.ai_search.parse_user_request",
                    side_effect=AssertionError(
                        "saved-offer button names must not be AI searches"
                    ),
                ), patch(
                    "handlers.ai_search.search_channel_posts",
                    side_effect=AssertionError(
                        "saved-offer button names must not be searches"
                    ),
                ), patch(
                    "handlers.messages.enqueue_url",
                    new=AsyncMock(),
                ) as enqueue:
                    await handle_message(update, context)

                self.assertNotIn("ai_search_active", context.user_data)
                self.assertIn("انتهت جلسة لوحة العروض", message.replies[-1][0])
                self.assertIsInstance(
                    message.replies[-1][1],
                    ReplyKeyboardMarkup,
                )
                enqueue.assert_not_awaited()

    async def test_saved_offer_keyboard_after_restart_is_replaced_without_search(self):
        context = FakeContext()
        message = FakeMessage(f"{SAVED_OFFERS_MAIN_BUTTONS[0]}\u2063")
        update = SimpleNamespace(
            message=message,
            effective_message=message,
            effective_user=SimpleNamespace(first_name="Test", last_name="User"),
            effective_chat=message.chat,
        )

        with patch(
            "handlers.messages.handle_admin_message",
            new=AsyncMock(return_value=False),
        ), patch(
            "handlers.messages.record_user_activity",
        ), patch(
            "handlers.messages.handle_smart_cart_message",
            new=AsyncMock(return_value=False),
        ), patch(
            "handlers.messages.enqueue_url",
            new=AsyncMock(),
        ) as enqueue, patch(
            "handlers.ai_search.parse_user_request",
            side_effect=AssertionError("stale buttons must not start AI search"),
        ), patch(
            "handlers.ai_search.search_channel_posts",
            side_effect=AssertionError("stale buttons must not search offers"),
        ):
            await handle_message(update, context)

        self.assertEqual(len(message.replies), 1)
        self.assertIn("انتهت جلسة لوحة العروض", message.replies[0][0])
        self.assertIsInstance(message.replies[0][1], ReplyKeyboardMarkup)
        self.assertEqual(
            message.replies[0][1].keyboard[0][0].text,
            AI_SEARCH_BUTTON_TEXT,
        )
        enqueue.assert_not_awaited()
        self.assertNotIn("ai_search_active", context.user_data)

    async def test_stale_saved_offers_inline_button_does_not_search_after_restart(self):
        context = FakeContext()
        message = FakeMessage()
        query = SimpleNamespace(
            data="saved_offers:shipping",
            message=message,
            answer=AsyncMock(),
        )
        update = SimpleNamespace(callback_query=query)

        with patch(
            "handlers.ai_search.search_channel_posts",
            side_effect=AssertionError("stale callbacks must not search offers"),
        ):
            await saved_offers_callback(update, context)

        query.answer.assert_awaited_once()
        self.assertEqual(len(message.replies), 1)
        self.assertIn("انتهت جلسة لوحة العروض", message.replies[0][0])
        self.assertIsInstance(message.replies[0][1], ReplyKeyboardMarkup)

    async def test_stale_ai_navigation_button_does_not_use_empty_restart_state(self):
        context = FakeContext()
        message = FakeMessage()
        query = SimpleNamespace(
            message=message,
            answer=AsyncMock(),
        )
        update = SimpleNamespace(callback_query=query)

        with patch(
            "handlers.ai_search.is_feature_enabled",
            side_effect=AssertionError("stale navigation must not reach AI"),
        ):
            await ai_next_callback(update, context)

        query.answer.assert_awaited_once()
        self.assertEqual(len(message.replies), 1)
        self.assertIn("انتهت جلسة لوحة العروض", message.replies[0][0])

    async def test_plain_text_outside_ai_mode_uses_normal_path(self):
        message = FakeMessage("أرخص هاتف Samsung S24")
        update = SimpleNamespace(
            message=message,
            effective_message=message,
            effective_user=SimpleNamespace(first_name="Test", last_name="User"),
            effective_chat=SimpleNamespace(id=123),
        )
        context = FakeContext()

        with patch(
            "handlers.messages.handle_admin_message",
            new=AsyncMock(return_value=False),
        ), patch(
            "handlers.messages.record_user_activity",
        ), patch(
            "handlers.messages.handle_smart_cart_message",
            new=AsyncMock(return_value=False),
        ), patch(
            "handlers.messages.handle_ai_search_button_message",
            new=AsyncMock(return_value=False),
        ), patch(
            "handlers.ai_search.parse_user_request",
        ) as parse_request, patch(
            "handlers.ai_search.search_channel_posts",
        ) as search_posts, patch(
            "handlers.messages.enqueue_url",
            new=AsyncMock(),
        ) as enqueue:
            await handle_message(update, context)

        parse_request.assert_not_called()
        search_posts.assert_not_called()
        self.assertNotIn("ai_search_active", context.user_data)
        enqueue.assert_not_awaited()
        self.assertIn("من فضلك قم بإرسال روابط", message.replies[-1][0])

    async def test_inactive_ai_search_ignores_natural_language(self):
        context = FakeContext()
        message = FakeMessage("أرخص هاتف Samsung S24")
        update = SimpleNamespace(
            effective_message=message,
            effective_chat=SimpleNamespace(id=123),
        )
        posts = [{"id": 1, "title": "Samsung S24"}]

        with patch(
            "handlers.ai_search.is_feature_enabled",
            return_value=True,
        ), patch(
            "handlers.ai_search.parse_user_request",
            return_value={"request_type": "product_cheapest"},
        ), patch(
            "handlers.ai_search.search_channel_posts",
            return_value=posts,
        ), patch(
            "handlers.ai_search.send_stored_post",
            new=AsyncMock(),
        ) as send_post:
            handled = await handle_ai_search_message(
                update,
                context,
            )

        self.assertFalse(handled)
        send_post.assert_not_awaited()
        self.assertNotIn("ai_search_active", context.user_data)

    async def test_slow_intent_parser_uses_local_fallback(self):
        context = FakeContext()
        context.user_data["ai_search_active"] = True
        message = FakeMessage("أرخص هاتف Samsung")
        update = SimpleNamespace(
            effective_message=message,
            effective_chat=SimpleNamespace(id=123),
        )
        fallback_intent = {
            "request_type": "category_cheapest",
            "category": "phones",
        }

        def slow_parser(_text):
            time.sleep(0.05)
            return {"request_type": "unsupported"}

        with patch(
            "handlers.ai_search.is_feature_enabled",
            return_value=True,
        ), patch(
            "handlers.ai_search.parse_user_request",
            side_effect=slow_parser,
        ), patch(
            "handlers.ai_search._safe_local_user_request_with_cache",
            return_value=fallback_intent,
        ) as safe_local_parser, patch(
            "handlers.ai_search.search_channel_posts",
            return_value=[{"id": 1, "title": "هاتف"}],
        ), patch(
            "handlers.ai_search.send_stored_post",
            new=AsyncMock(),
        ) as send_post, patch(
            "handlers.ai_search.AI_USER_REQUEST_TIMEOUT_SECONDS",
            0.01,
        ):
            handled = await handle_ai_search_message(
                update,
                context,
            )

        self.assertTrue(handled)
        safe_local_parser.assert_called_once_with("أرخص هاتف Samsung")
        send_post.assert_awaited_once()

    async def test_cache_miss_uses_no_results_message_instead_of_help(self):
        context = FakeContext()
        context.user_data["ai_search_active"] = True
        message = FakeMessage("اريد عروض على المجوهرات")
        update = SimpleNamespace(
            effective_message=message,
            effective_chat=SimpleNamespace(id=123),
        )

        with patch(
            "handlers.ai_search.is_feature_enabled",
            return_value=True,
        ), patch(
            "handlers.ai_search.parse_user_request",
            return_value={
                "request_type": "unsupported",
                "keywords": [],
                "no_results": True,
            },
        ), patch(
            "handlers.ai_search.saved_offers_help_markup",
        ) as help_markup:
            handled = await handle_ai_search_message(update, context)

        self.assertTrue(handled)
        self.assertEqual(message.replies[-1][0], AI_SEARCH_NO_RESULTS_TEXT)
        help_markup.assert_called_once()

    async def test_inactive_aliexpress_link_is_not_an_ai_request(self):
        context = FakeContext()
        message = FakeMessage(
            "https://www.aliexpress.com/item/1005001234567890.html"
        )

        with patch(
            "handlers.ai_search.is_feature_enabled",
            side_effect=AssertionError("links must bypass AI feature lookup"),
        ):
            handled = await handle_ai_search_message(
                SimpleNamespace(effective_message=message),
                context,
            )

        self.assertFalse(handled)
        self.assertEqual(context.user_data, {})

    def test_clearing_ai_state_removes_stale_auxiliary_messages(self):
        context = FakeContext()
        context.user_data.update({
            "ai_search_active": True,
            "ai_search_results": [{"id": 1}],
            "ai_search_index": 0,
            "ai_search_auxiliary_message_ids": [10, 11],
        })

        _clear_ai_search_state(context)

        self.assertEqual(context.user_data, {})

    async def test_reply_button_enters_ai_with_saved_offers_button(self):
        context = FakeContext()
        message = FakeMessage(AI_SEARCH_BUTTON_TEXT)

        handled = await handle_ai_search_button_message(
            SimpleNamespace(effective_message=message),
            context,
        )

        self.assertTrue(handled)
        self.assertTrue(context.user_data["ai_search_active"])
        markup = message.replies[-1][1]
        self.assertIsInstance(markup, InlineKeyboardMarkup)
        self.assertEqual(
            markup.inline_keyboard[0][0].text,
            SAVED_OFFERS_BUTTON_TEXT,
        )

    async def test_offer_ai_button_enters_ai_mode_like_reply_keyboard(self):
        reply_text = AsyncMock()
        query = SimpleNamespace(
            answer=AsyncMock(),
            message=SimpleNamespace(reply_text=reply_text),
        )
        context = FakeContext()

        with patch("handlers.ai_search.is_feature_enabled", return_value=True):
            await ask_ai_callback(
                SimpleNamespace(callback_query=query),
                context,
            )

        query.answer.assert_awaited_once()
        self.assertEqual(reply_text.await_count, 2)
        self.assertTrue(context.user_data["ai_search_active"])
        self.assertEqual(reply_text.await_args_list[-1].args[0], AI_SEARCH_HELP_TEXT)
        markup = reply_text.await_args_list[-1].kwargs["reply_markup"]
        self.assertIsInstance(markup, InlineKeyboardMarkup)
        self.assertEqual(
            markup.inline_keyboard[0][0].text,
            SAVED_OFFERS_BUTTON_TEXT,
        )

    async def test_ai_activation_removes_reply_keyboard_before_help_message(self):
        events = []
        context = FakeContext()
        context.bot = SimpleNamespace(
            delete_message=AsyncMock(
                side_effect=lambda **kwargs: events.append(("delete", kwargs))
            )
        )
        message = KeyboardTraceMessage(AI_SEARCH_BUTTON_TEXT, events)

        with patch("handlers.ai_search.is_feature_enabled", return_value=True):
            handled = await handle_ai_search_button_message(
                SimpleNamespace(effective_message=message),
                context,
            )

        self.assertTrue(handled)
        self.assertEqual(
            [event[0] for event in events],
            ["reply", "delete", "reply"],
        )
        self.assertEqual(message.replies[0][0], "\u2063")
        self.assertIsInstance(message.replies[0][1], ReplyKeyboardRemove)
        self.assertEqual(message.replies[1][0], AI_SEARCH_HELP_TEXT)
        self.assertIsInstance(message.replies[1][1], InlineKeyboardMarkup)
        self.assertTrue(context.user_data["ai_search_active"])

    async def test_saved_offer_browsing_uses_reply_keyboard_for_navigation(self):
        context = FakeContext()
        context.user_data["ai_search_active"] = True
        entry_message = FakeMessage()
        query = SimpleNamespace(
            answer=AsyncMock(),
            data="saved_offers",
            message=entry_message,
        )

        with patch(
            "handlers.ai_search.count_processed_channel_posts",
            return_value=17,
        ):
            await saved_offers_callback(
                SimpleNamespace(callback_query=query),
                context,
            )

        self.assertEqual(len(entry_message.replies), 1)
        main_keyboard = entry_message.replies[0][1]
        self.assertIsInstance(main_keyboard, ReplyKeyboardMarkup)
        self.assertEqual(
            [
                [button.text.removesuffix("\u2063") for button in row]
                for row in main_keyboard.keyboard
            ],
            [
                list(SAVED_OFFERS_MAIN_BUTTONS[:3]),
                list(SAVED_OFFERS_MAIN_BUTTONS[3:]),
            ],
        )

        phone_message = FakeMessage(f"{SAVED_OFFERS_MAIN_BUTTONS[0]}\u2063")
        await handle_saved_offers_reply_message(
            SimpleNamespace(effective_message=phone_message),
            context,
        )
        self.assertEqual(len(phone_message.replies), 1)
        phone_keyboard = phone_message.replies[0][1]
        self.assertIsInstance(phone_keyboard, ReplyKeyboardMarkup)
        self.assertEqual(context.user_data["saved_offers_menu"], "phones")
        self.assertEqual(
            [
                [button.text.removesuffix("\u2063") for button in row]
                for row in phone_keyboard.keyboard
            ][-1],
            [SAVED_OFFERS_BACK_BUTTON_TEXT],
        )

        back_message = FakeMessage(f"{SAVED_OFFERS_BACK_BUTTON_TEXT}\u2063")
        with patch(
            "handlers.ai_search.count_processed_channel_posts",
            return_value=17,
        ):
            await handle_saved_offers_reply_message(
                SimpleNamespace(effective_message=back_message),
                context,
            )
        self.assertEqual(context.user_data["saved_offers_menu"], "main")
        self.assertEqual(len(back_message.replies), 1)
        self.assertEqual(
            [
                [button.text.removesuffix("\u2063") for button in row]
                for row in back_message.replies[-1][1].keyboard
            ],
            [
                list(SAVED_OFFERS_MAIN_BUTTONS[:3]),
                list(SAVED_OFFERS_MAIN_BUTTONS[3:]),
            ],
        )

        categories_message = FakeMessage(
            f"{SAVED_OFFERS_MAIN_BUTTONS[3]}\u2063"
        )
        await handle_saved_offers_reply_message(
            SimpleNamespace(effective_message=categories_message),
            context,
        )
        self.assertEqual(context.user_data["saved_offers_menu"], "categories")

        category_back_message = FakeMessage(
            f"{SAVED_OFFERS_BACK_BUTTON_TEXT}\u2063"
        )
        with patch(
            "handlers.ai_search.count_processed_channel_posts",
            return_value=17,
        ):
            await handle_saved_offers_reply_message(
                SimpleNamespace(effective_message=category_back_message),
                context,
            )
        self.assertEqual(context.user_data["saved_offers_menu"], "main")

    async def test_marked_realme_reply_button_uses_saved_brand_filter(self):
        context = FakeContext()
        context.user_data.update({
            "ai_search_active": True,
            "saved_offers_menu": "phones",
        })
        message = FakeMessage("Realme\u2063")
        message.chat = SimpleNamespace(id=123)
        posts = [{"id": 7, "title": "Realme offer"}]

        with patch(
            "handlers.ai_search.search_channel_posts",
            return_value=posts,
        ) as search_posts, patch(
            "handlers.ai_search.send_stored_post",
            new=AsyncMock(),
        ) as send_post, patch(
            "handlers.ai_search.parse_user_request",
            side_effect=AssertionError(
                "reply-keyboard taps must not use AI parsing"
            ),
        ):
            handled = await handle_saved_offers_reply_message(
                SimpleNamespace(effective_message=message),
                context,
            )

        self.assertTrue(handled)
        search_posts.assert_called_once_with(
            _saved_offer_intent("brand", "realme")
        )
        send_post.assert_awaited_once_with(
            123,
            context,
            posts[0],
            has_previous=False,
            has_next=False,
        )
        self.assertTrue(context.user_data["ai_search_active"])

    async def test_message_router_handles_marked_tap_before_ai_search(self):
        context = FakeContext()
        context.user_data.update({
            "ai_search_active": True,
            "saved_offers_menu": "phones",
        })
        message = FakeMessage("Realme\u2063")
        update = SimpleNamespace(
            message=message,
            effective_message=message,
            effective_user=SimpleNamespace(first_name="Test", last_name="User"),
            effective_chat=message.chat,
        )

        with patch(
            "handlers.messages.handle_admin_message",
            new=AsyncMock(return_value=False),
        ), patch(
            "handlers.messages.record_user_activity",
        ), patch(
            "handlers.messages.handle_smart_cart_message",
            new=AsyncMock(return_value=False),
        ), patch(
            "handlers.messages.handle_ai_search_message",
            new=AsyncMock(return_value=False),
        ) as ai_search, patch(
            "handlers.ai_search.search_channel_posts",
            return_value=[],
        ) as search_posts:
            await handle_message(update, context)

        ai_search.assert_not_awaited()
        search_posts.assert_called_once_with(
            _saved_offer_intent("brand", "realme")
        )

    async def test_marked_shipping_bundle_and_category_buttons_use_saved_filters(self):
        category, category_label = SAVED_OFFER_CATEGORY_FILTERS[0]
        cases = [
            (
                "main",
                SAVED_OFFERS_MAIN_BUTTONS[1],
                _saved_offer_intent("shipping"),
            ),
            (
                "main",
                SAVED_OFFERS_MAIN_BUTTONS[2],
                _saved_offer_intent("bundle"),
            ),
            (
                "categories",
                category_label,
                _saved_offer_intent("category", category),
            ),
        ]

        for menu, label, intent in cases:
            with self.subTest(menu=menu, label=label):
                context = FakeContext()
                context.user_data.update({
                    "ai_search_active": True,
                    "saved_offers_menu": menu,
                })
                message = FakeMessage(f"{label}\u2063")

                with patch(
                    "handlers.ai_search.search_channel_posts",
                    return_value=[],
                ) as search_posts, patch(
                    "handlers.ai_search.parse_user_request",
                    side_effect=AssertionError(
                        "reply-keyboard taps must use saved filters"
                    ),
                ):
                    handled = await handle_saved_offers_reply_message(
                        SimpleNamespace(effective_message=message),
                        context,
                    )

                self.assertTrue(handled)
                search_posts.assert_called_once_with(intent)

    async def test_saved_offers_main_menu_keeps_button_message_and_uses_reply_keyboard(self):
        context = FakeContext()
        context.user_data["ai_search_active"] = True
        reply_text = AsyncMock()
        edit_text = AsyncMock()
        query = SimpleNamespace(
            answer=AsyncMock(),
            data="saved_offers",
            message=SimpleNamespace(
                edit_text=edit_text,
                reply_text=reply_text,
            ),
        )

        with patch(
            "handlers.ai_search.count_processed_channel_posts",
            return_value=17,
        ):
            await saved_offers_callback(
                SimpleNamespace(callback_query=query),
                context,
            )

        query.answer.assert_awaited_once()
        edit_text.assert_not_awaited()
        reply_text.assert_awaited_once()
        self.assertEqual(
            reply_text.await_args.args[0],
            SAVED_OFFERS_MENU_TEXT.format(count=17),
        )
        keyboard = reply_text.await_args.kwargs["reply_markup"]
        self.assertIsInstance(keyboard, ReplyKeyboardMarkup)
        self.assertEqual(
            [
                button.text.removesuffix("\u2063")
                for row in keyboard.keyboard
                for button in row
            ],
            list(SAVED_OFFERS_MAIN_BUTTONS),
        )
        self.assertEqual(
            [
                [button.text.removesuffix("\u2063") for button in row]
                for row in keyboard.keyboard
            ],
            [
                list(SAVED_OFFERS_MAIN_BUTTONS[:3]),
                list(SAVED_OFFERS_MAIN_BUTTONS[3:]),
            ],
        )
        self.assertEqual(context.user_data["saved_offers_menu"], "main")

    def test_saved_offers_main_reply_markup_uses_two_requested_rows(self):
        keyboard = _saved_offers_main_markup()

        self.assertIsNone(keyboard.is_persistent)
        self.assertIsNone(keyboard.one_time_keyboard)
        self.assertEqual(
            [
                [button.text.removesuffix("\u2063") for button in row]
                for row in keyboard.keyboard
            ],
            [
                list(SAVED_OFFERS_MAIN_BUTTONS[:3]),
                list(SAVED_OFFERS_MAIN_BUTTONS[3:]),
            ],
        )

    async def test_saved_offers_main_menu_uses_reply_keyboard(self):
        context = FakeContext()
        context.user_data["ai_search_active"] = True
        reply_text = AsyncMock()
        edit_text = AsyncMock()
        query = SimpleNamespace(
            answer=AsyncMock(),
            data="saved_offers",
            message=SimpleNamespace(
                edit_text=edit_text,
                reply_text=reply_text,
                chat=SimpleNamespace(id=123),
            ),
        )

        with patch(
            "handlers.ai_search.count_processed_channel_posts",
            return_value=17,
        ):
            await saved_offers_callback(
                SimpleNamespace(callback_query=query),
                context,
            )

        reply_text.assert_awaited_once()
        self.assertNotIn("saved_offers_reply_keyboard_hidden", context.user_data)
        self.assertIsInstance(
            reply_text.await_args.kwargs["reply_markup"],
            ReplyKeyboardMarkup,
        )

    async def test_saved_offers_phone_menu_has_three_columns_and_back(self):
        context = FakeContext()
        context.user_data["ai_search_active"] = True
        reply_text = AsyncMock()
        query = SimpleNamespace(
            answer=AsyncMock(),
            data="saved_offers:phones",
            message=SimpleNamespace(reply_text=reply_text),
        )

        await saved_offers_callback(
            SimpleNamespace(callback_query=query),
            context,
        )

        self.assertEqual(reply_text.await_args.args[0], SAVED_OFFERS_PHONE_TEXT)
        keyboard = reply_text.await_args.kwargs["reply_markup"]
        self.assertIsInstance(keyboard, ReplyKeyboardMarkup)
        self.assertEqual(
            [
                [button.text.removesuffix("\u2063") for button in row]
                for row in keyboard.keyboard
            ],
            [
                list(SAVED_OFFERS_PHONE_BRANDS[:3]),
                list(SAVED_OFFERS_PHONE_BRANDS[3:6]),
                list(SAVED_OFFERS_PHONE_BRANDS[6:]),
                [SAVED_OFFERS_BACK_BUTTON_TEXT],
            ],
        )

    async def test_saved_offers_category_menu_includes_tablets_and_more_categories(self):
        context = FakeContext()
        context.user_data["ai_search_active"] = True
        reply_text = AsyncMock()
        query = SimpleNamespace(
            answer=AsyncMock(),
            data="saved_offers:other",
            message=SimpleNamespace(reply_text=reply_text),
        )

        await saved_offers_callback(
            SimpleNamespace(callback_query=query),
            context,
        )

        self.assertEqual(reply_text.await_args.args[0], SAVED_OFFERS_CATEGORY_TEXT)
        keyboard = reply_text.await_args.kwargs["reply_markup"]
        self.assertIsInstance(keyboard, ReplyKeyboardMarkup)
        labels = [
            button.text.removesuffix("\u2063")
            for row in keyboard.keyboard
            for button in row
        ]
        self.assertEqual(labels[-1], SAVED_OFFERS_BACK_BUTTON_TEXT)
        self.assertEqual(
            len(labels) - 1,
            17,
        )
        self.assertEqual(
            labels[:-1],
            [
                "هواتف",
                "طابلات",
                "أجهزة الكترونية",
                "كمبيوتر",
                "ساعات ذكية",
                "سماعات بلوثوث",
                "رياضة",
                "المنزل",
                "الأطفال",
                "الأزياء",
                "الإكسسوارات",
                "الجمال",
                "المركبات",
                "الأدوات",
                "متنوعات",
                "قطع غيار",
                "مستلزمات إلكترونية",
            ],
        )

    async def test_saved_offers_category_menu_uses_reply_buttons(self):
        context = FakeContext()
        context.user_data.update({
            "ai_search_active": True,
            "saved_offers_menu": "categories",
        })
        message = FakeMessage()

        await saved_offers_callback(
            SimpleNamespace(
                callback_query=SimpleNamespace(
                    answer=AsyncMock(),
                    data="saved_offers:other",
                    message=message,
                )
            ),
            context,
        )

        keyboard = message.replies[0][1]
        self.assertIsInstance(keyboard, ReplyKeyboardMarkup)
        self.assertEqual(
            [
                button.text.removesuffix("\u2063")
                for row in keyboard.keyboard
                for button in row
            ][-1],
            SAVED_OFFERS_BACK_BUTTON_TEXT,
        )
        self.assertEqual(message.replies, [(SAVED_OFFERS_CATEGORY_TEXT, keyboard)])

    async def test_stale_saved_offers_show_callback_is_ignored_after_ai_exit(self):
        context = FakeContext()
        context.user_data.update({
            "ai_search_active": True,
            "saved_offers_menu": "phones",
            "normal_session_state": "browsing",
        })
        _clear_ai_search_state(context)
        state_after_ai_exit = dict(context.user_data)
        reply_text = AsyncMock()
        query = SimpleNamespace(
            answer=AsyncMock(),
            data="saved_offers:show",
            message=SimpleNamespace(reply_text=reply_text),
        )

        await saved_offers_callback(
            SimpleNamespace(callback_query=query),
            context,
        )

        query.answer.assert_awaited_once()
        reply_text.assert_not_awaited()
        self.assertEqual(context.user_data, state_after_ai_exit)

    async def test_saved_offer_search_callback_after_ai_exit_cannot_search(self):
        context = FakeContext()
        context.user_data["saved_offers_menu"] = "phones"
        message = FakeMessage()
        query = SimpleNamespace(
            answer=AsyncMock(),
            data="saved_offers:brand:realme",
            message=message,
        )

        with patch(
            "handlers.ai_search.search_channel_posts",
        ) as search_posts:
            await saved_offers_callback(
                SimpleNamespace(callback_query=query),
                context,
            )

        query.answer.assert_awaited_once()
        search_posts.assert_not_called()
        self.assertEqual(
            message.replies[-1][0],
            AI_SEARCH_SESSION_EXPIRED_TEXT,
        )

    async def test_saved_brand_search_uses_local_search_without_ai(self):
        context = FakeContext()
        context.user_data["ai_search_active"] = True
        edit_text = AsyncMock()
        query = SimpleNamespace(
            answer=AsyncMock(),
            data="saved_offers:brand:redmi",
            message=SimpleNamespace(
                edit_text=edit_text,
                chat=SimpleNamespace(id=123),
            ),
        )
        posts = [{"id": 9, "title": "Redmi Note"}]

        with patch(
            "handlers.ai_search.search_channel_posts",
            return_value=posts,
        ) as search_posts, patch(
            "handlers.ai_search.parse_user_request",
            side_effect=AssertionError("saved buttons must not call AI"),
        ), patch(
            "handlers.ai_search.send_stored_post",
            new=AsyncMock(),
        ) as send_post:
            await saved_offers_callback(
                SimpleNamespace(callback_query=query),
                context,
            )

        search_posts.assert_called_once_with(
            {
                "request_type": "product_best",
                "keywords": ["redmi"],
                "category": "phones",
                "required_specs": [],
            }
        )
        send_post.assert_awaited_once_with(
            123,
            context,
            posts[0],
            has_previous=False,
            has_next=False,
        )
        edit_text.assert_not_awaited()
        self.assertTrue(context.user_data["ai_search_active"])
        self.assertEqual(context.user_data["ai_search_results"], posts)

    async def test_saved_tablets_search_uses_internal_category_without_ai(self):
        context = FakeContext()
        context.user_data["ai_search_active"] = True
        edit_text = AsyncMock()
        tablet_index = next(
            index
            for index, (_category, label) in enumerate(SAVED_OFFER_CATEGORY_FILTERS)
            if label == "طابلات"
        )
        query = SimpleNamespace(
            answer=AsyncMock(),
            data=f"saved_offers:category:{tablet_index}",
            message=SimpleNamespace(
                edit_text=edit_text,
                chat=SimpleNamespace(id=123),
            ),
        )
        posts = [{"id": 10, "title": "Tablet"}]

        with patch(
            "handlers.ai_search.search_channel_posts",
            return_value=posts,
        ) as search_posts, patch(
            "handlers.ai_search.parse_user_request",
            side_effect=AssertionError("saved buttons must not call AI"),
        ), patch(
            "handlers.ai_search.send_stored_post",
            new=AsyncMock(),
        ) as send_post:
            await saved_offers_callback(
                SimpleNamespace(callback_query=query),
                context,
            )

        search_posts.assert_called_once_with(
            {
                "request_type": "trending",
                "keywords": [],
                "required_specs": [],
                "saved_subcategory": "saved_tablets",
            }
        )
        send_post.assert_awaited_once()

    async def test_saved_special_category_search_uses_category_column_without_ai(self):
        cases = (
            ("ساعات ذكية", "smart_watches"),
            ("سماعات بلوثوث", "bluetooth_headphones"),
        )

        for label, category in cases:
            with self.subTest(label=label):
                context = FakeContext()
                context.user_data["ai_search_active"] = True
                edit_text = AsyncMock()
                category_index = next(
                    index
                    for index, (_category, current_label)
                    in enumerate(SAVED_OFFER_CATEGORY_FILTERS)
                    if current_label == label
                )
                query = SimpleNamespace(
                    answer=AsyncMock(),
                    data=f"saved_offers:category:{category_index}",
                    message=SimpleNamespace(
                        edit_text=edit_text,
                        chat=SimpleNamespace(id=123),
                    ),
                )
                posts = [{"id": 11, "title": label}]

                with patch(
                    "handlers.ai_search.search_channel_posts",
                    return_value=posts,
                ) as search_posts, patch(
                    "handlers.ai_search.parse_user_request",
                    side_effect=AssertionError(
                        "saved buttons must not call AI"
                    ),
                ), patch(
                    "handlers.ai_search.send_stored_post",
                    new=AsyncMock(),
                ):
                    await saved_offers_callback(
                        SimpleNamespace(callback_query=query),
                        context,
                    )

                search_posts.assert_called_once_with(
                    {
                        "request_type": "trending",
                        "keywords": [],
                        "required_specs": [],
                        "saved_category": category,
                    }
                )

    def test_saved_special_category_matching_uses_only_category_column(self):
        from core.db import _post_match_score

        smart_watch_intent = _saved_offer_intent(
            "category",
            "smart_watches",
        )
        bluetooth_intent = _saved_offer_intent(
            "category",
            "bluetooth_headphones",
        )

        self.assertIsNotNone(
            _post_match_score(
                {
                    "category": "smart_watches",
                    "second_level_category_name": "Smart Watches",
                },
                smart_watch_intent,
                [],
            )
        )
        self.assertIsNone(
            _post_match_score(
                {
                    "category": "bluetooth_headphones",
                    "second_level_category_name": "Smart Watches",
                },
                smart_watch_intent,
                [],
            )
        )
        self.assertIsNotNone(
            _post_match_score(
                {
                    "category": "bluetooth_headphones",
                    "second_level_category_name": "Earphones & Headphones",
                },
                bluetooth_intent,
                [],
            )
        )

    def test_product_matching_uses_one_synonym_per_keyword_group(self):
        from core.db import _post_match_score

        intent = {
            "request_type": "product_best",
            "keywords": ["حافظات"],
            "category": "phones",
            "keyword_groups": [
                ["حافظات", "coques", "cases"],
            ],
        }
        post = {
            "title": "Coques téléphone antichoc",
            "category": "phones",
            "price": 5,
        }

        self.assertIsNotNone(_post_match_score(post, intent, ["حافظات"]))

    def test_cached_cross_language_product_terms_match_only_related_titles(self):
        from core.db import _post_match_score

        intent = {
            "request_type": "product_best",
            "keywords": ["automotive diagnostic scanner"],
            "category": None,
            "keyword_groups": [
                ["automotive diagnostic scanner", "obd code reader"],
            ],
        }
        matching_post = {
            "title": "Automotive OBD2 Diagnostic Scanner Tool",
            "price": 18,
        }
        unrelated_post = {
            "title": "Digital Tire Pressure Monitor",
            "price": 18,
        }

        self.assertIsNotNone(
            _post_match_score(
                matching_post,
                intent,
                intent["keywords"],
            )
        )
        self.assertIsNone(
            _post_match_score(
                unrelated_post,
                intent,
                intent["keywords"],
            )
        )

    async def test_saved_offer_no_results_has_no_keyboard(self):
        context = FakeContext()
        context.user_data["ai_search_active"] = True
        edit_text = AsyncMock()
        reply_text = AsyncMock()
        query = SimpleNamespace(
            answer=AsyncMock(),
            data="saved_offers:shipping",
            message=SimpleNamespace(
                edit_text=edit_text,
                reply_text=reply_text,
                chat=SimpleNamespace(id=123),
            ),
        )

        with patch(
            "handlers.ai_search.search_channel_posts",
            return_value=[],
        ):
            await saved_offers_callback(
                SimpleNamespace(callback_query=query),
                context,
            )

        self.assertEqual(reply_text.await_args.args[0], AI_SEARCH_NO_RESULTS_TEXT)
        self.assertIsNone(reply_text.await_args.kwargs.get("reply_markup"))
        edit_text.assert_not_awaited()

    async def test_saved_offers_inline_phone_choice_keeps_ai_mode(self):
        context = FakeContext()
        context.user_data["ai_search_active"] = True
        message = FakeMessage()
        query = SimpleNamespace(
            answer=AsyncMock(),
            data="saved_offers:phones",
            message=message,
        )

        with patch(
            "handlers.ai_search.search_channel_posts",
            side_effect=AssertionError("menu selection must not search"),
        ):
            await saved_offers_callback(
                SimpleNamespace(callback_query=query),
                context,
            )

        self.assertEqual(context.user_data["saved_offers_menu"], "phones")
        self.assertTrue(context.user_data["ai_search_active"])
        self.assertEqual(message.replies[-1][0], SAVED_OFFERS_PHONE_TEXT)
        self.assertIsInstance(message.replies[-1][1], ReplyKeyboardMarkup)

    async def test_saved_offer_group_buttons_search_all_member_categories(self):
        for group_key, label, categories in SAVED_OFFER_CATEGORY_GROUPS:
            with self.subTest(group_key=group_key):
                context = FakeContext()
                context.user_data.update({
                    "ai_search_active": True,
                    "saved_offers_menu": "categories",
                })
                category_index = next(
                    index
                    for index, (category, _label) in enumerate(
                        SAVED_OFFER_CATEGORY_FILTERS
                    )
                    if category == group_key
                )
                message = FakeMessage()
                message.chat = SimpleNamespace(id=123)
                query = SimpleNamespace(
                    answer=AsyncMock(),
                    data=f"saved_offers:category:{category_index}",
                    message=message,
                )
                posts = [{"id": 1, "title": label}]

                with patch(
                    "handlers.ai_search.search_channel_posts",
                    return_value=posts,
                ) as search_posts, patch(
                    "handlers.ai_search.parse_user_request",
                    side_effect=AssertionError(
                        "saved group buttons must not call AI"
                    ),
                ), patch(
                    "handlers.ai_search.send_stored_post",
                    new=AsyncMock(),
                ) as send_post:
                    await saved_offers_callback(
                        SimpleNamespace(callback_query=query),
                        context,
                    )

                query.answer.assert_awaited_once()
                search_posts.assert_called_once_with(
                    {
                        "request_type": "trending",
                        "keywords": [],
                        "required_specs": [],
                        "saved_subcategory": group_key,
                    }
                )
                send_post.assert_awaited_once()

    def test_saved_offer_group_intents_are_defined_for_every_group_button(self):
        self.assertEqual(
            set(SAVED_OFFER_CATEGORY_GROUPS_BY_KEY),
            {
                group_key
                for group_key, _label, _categories in SAVED_OFFER_CATEGORY_GROUPS
            },
        )
        for group_key, _label, _patterns in SAVED_OFFER_CATEGORY_GROUPS:
            with self.subTest(group_key=group_key):
                self.assertEqual(
                    _saved_offer_intent("category", group_key)[
                        "saved_subcategory"
                    ],
                    group_key,
                )

    def test_saved_offer_group_matching_accepts_only_member_categories(self):
        from core.db import _post_match_score

        for group_key, _label, patterns in SAVED_OFFER_CATEGORY_GROUPS:
            intent = _saved_offer_intent("category", group_key)
            for pattern in patterns:
                with self.subTest(group_key=group_key, pattern=pattern):
                    self.assertIsNotNone(
                        _post_match_score(
                            {"second_level_category_name": pattern},
                            intent,
                            [],
                        )
                    )
            self.assertIsNone(
                _post_match_score(
                    {"second_level_category_name": "Sports & Entertainment"},
                    intent,
                    [],
                )
            )

    def test_saved_offer_filters_require_second_level_category(self):
        from core.db import _post_match_score

        phones_intent = _saved_offer_intent("category", "saved_phones")

        self.assertIsNotNone(
            _post_match_score(
                {
                    "first_level_category_name": "Phones & Telecommunications",
                    "second_level_category_name": "Mobile Phones",
                },
                phones_intent,
                [],
            )
        )
        self.assertIsNone(
            _post_match_score(
                {
                    "first_level_category_name": "Phones & Telecommunications",
                    "second_level_category_name": "Mobile Phone Accessories",
                },
                phones_intent,
                [],
            )
        )
        self.assertIsNone(
            _post_match_score(
                {
                    "first_level_category_name": "Phones & Telecommunications",
                },
                phones_intent,
                [],
            )
        )

    def test_textual_category_matching_does_not_fall_back_to_first_level(self):
        from core.db import _post_match_score

        intent = {
            "request_type": "category_cheapest",
            "category": "phones",
        }
        self.assertIsNotNone(
            _post_match_score(
                {
                    "first_level_category_name": "Phones & Telecommunications",
                    "second_level_category_name": "Mobile Phones",
                },
                intent,
                [],
            )
        )
        self.assertIsNone(
            _post_match_score(
                {
                    "first_level_category_name": "Phones & Telecommunications",
                    "second_level_category_name": "Mobile Phone Accessories",
                },
                intent,
                [],
            )
        )
        self.assertIsNone(
            _post_match_score(
                {
                    "first_level_category_name": "Phones & Telecommunications",
                    "category": "phones",
                },
                intent,
                [],
            )
        )

    async def test_saved_offers_inline_brand_search_uses_local_search(self):
        context = FakeContext()
        context.user_data.update({
            "ai_search_active": True,
            "saved_offers_menu": "phones",
        })
        message = FakeMessage()
        message.chat = SimpleNamespace(id=123)
        query = SimpleNamespace(
            answer=AsyncMock(),
            data="saved_offers:brand:redmi",
            message=message,
        )
        posts = [{"id": 9, "title": "Redmi Note"}]

        with patch(
            "handlers.ai_search.search_channel_posts",
            return_value=posts,
        ) as search_posts, patch(
            "handlers.ai_search.send_stored_post",
            new=AsyncMock(),
        ) as send_post:
            await saved_offers_callback(
                SimpleNamespace(callback_query=query),
                context,
            )

        query.answer.assert_awaited_once()
        search_posts.assert_called_once_with(
            {
                "request_type": "product_best",
                "keywords": ["redmi"],
                "category": "phones",
                "required_specs": [],
            }
        )
        send_post.assert_awaited_once_with(
            123,
            context,
            posts[0],
            has_previous=False,
            has_next=False,
        )
        self.assertTrue(context.user_data["ai_search_active"])

    async def test_filtered_brand_results_navigate_without_reparsing(self):
        context = FakeContext()
        context.user_data.update({
            "ai_search_active": True,
            "saved_offers_menu": "phones",
        })
        posts = [
            {"id": 9, "title": "Redmi Note أول"},
            {"id": 10, "title": "Redmi Note ثان"},
            {"id": 11, "title": "Redmi Note ثالث"},
        ]
        message = FakeMessage()
        message.chat = SimpleNamespace(id=123)
        query = SimpleNamespace(
            answer=AsyncMock(),
            data="saved_offers:brand:redmi",
            message=message,
        )

        with patch(
            "handlers.ai_search.search_channel_posts",
            return_value=posts,
        ) as search_posts, patch(
            "handlers.ai_search.parse_user_request",
            side_effect=AssertionError("filtered buttons must not call AI"),
        ) as parse_request, patch(
            "handlers.ai_search.send_stored_post",
            new=AsyncMock(),
        ) as send_post, patch(
            "handlers.ai_search.is_feature_enabled",
            return_value=True,
        ), patch(
            "handlers.ai_search._replace_stored_post_message",
            new=AsyncMock(),
        ) as replace_post:
            await saved_offers_callback(
                SimpleNamespace(callback_query=query),
                context,
            )

            navigation_query = SimpleNamespace(
                message=SimpleNamespace(
                    chat=SimpleNamespace(id=123),
                    message_id=456,
                    photo=None,
                    reply_text=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            navigation_update = SimpleNamespace(callback_query=navigation_query)
            await ai_next_callback(navigation_update, context)
            await ai_next_callback(navigation_update, context)
            await ai_previous_callback(navigation_update, context)

        query.answer.assert_awaited_once()
        search_posts.assert_called_once()
        parse_request.assert_not_called()
        self.assertEqual(context.user_data["ai_search_results"], posts)
        self.assertEqual(context.user_data["ai_search_index"], 1)
        send_post.assert_awaited_once_with(
            123,
            context,
            posts[0],
            has_previous=False,
            has_next=True,
        )
        self.assertEqual(
            [call.args[2] for call in replace_post.await_args_list],
            [posts[1], posts[2], posts[1]],
        )
        self.assertEqual(
            [
                (call.kwargs["has_previous"], call.kwargs["has_next"])
                for call in replace_post.await_args_list
            ],
            [(True, True), (True, False), (True, True)],
        )

    async def test_saved_offers_navigate_through_more_than_ten_results(self):
        context = FakeContext()
        context.user_data.update({
            "ai_search_active": True,
            "saved_offers_menu": "phones",
        })
        posts = [
            {"id": index, "title": f"Redmi Note {index}"}
            for index in range(1, 12)
        ]
        message = FakeMessage()
        message.chat = SimpleNamespace(id=123)
        query = SimpleNamespace(
            answer=AsyncMock(),
            data="saved_offers:brand:redmi",
            message=message,
        )

        with patch(
            "handlers.ai_search.search_channel_posts",
            return_value=posts,
        ), patch(
            "handlers.ai_search.send_stored_post",
            new=AsyncMock(),
        ), patch(
            "handlers.ai_search.is_feature_enabled",
            return_value=True,
        ), patch(
            "handlers.ai_search._replace_stored_post_message",
            new=AsyncMock(),
        ) as replace_post:
            await saved_offers_callback(
                SimpleNamespace(callback_query=query),
                context,
            )

            query = SimpleNamespace(
                message=SimpleNamespace(
                    chat=SimpleNamespace(id=123),
                    message_id=456,
                    photo=None,
                    reply_text=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            for _ in range(10):
                await ai_next_callback(
                    SimpleNamespace(callback_query=query),
                    context,
                )
            self.assertEqual(len(context.user_data["ai_search_results"]), 11)
            self.assertEqual(context.user_data["ai_search_index"], 10)
            self.assertEqual(replace_post.await_count, 10)
            self.assertFalse(replace_post.await_args.kwargs["has_next"])

    def test_explicit_subcategories_map_to_one_saved_offer_filter(self):
        for expected_filter, names in SAVED_OFFER_CATEGORY_SUBCATEGORY_NAMES.items():
            for name in names:
                with self.subTest(expected_filter=expected_filter, name=name):
                    self.assertEqual(
                        saved_offer_filter_from_subcategory(name),
                        expected_filter,
                    )

    def test_new_saved_offer_buttons_and_electronics_rename_are_registered(self):
        labels = dict(SAVED_OFFER_CATEGORY_FILTERS)

        self.assertEqual(labels["saved_electronics"], "أجهزة الكترونية")
        self.assertEqual(labels["saved_spare_parts"], "قطع غيار")
        self.assertEqual(
            labels["saved_electronic_supplies"],
            "مستلزمات إلكترونية",
        )
        self.assertIn(
            "saved_spare_parts",
            SAVED_OFFER_CATEGORY_GROUPS_BY_KEY,
        )
        self.assertIn(
            "saved_electronic_supplies",
            SAVED_OFFER_CATEGORY_GROUPS_BY_KEY,
        )

    async def test_filtered_category_results_clear_stale_navigation_state(self):
        context = FakeContext()
        context.user_data.update({
            "ai_search_active": True,
            "saved_offers_menu": "categories",
            "ai_search_results": [{"id": 99, "title": "نتيجة قديمة"}],
            "ai_search_index": 0,
        })
        message = FakeMessage()
        category_index = next(
            index
            for index, (category, _label) in enumerate(
                SAVED_OFFER_CATEGORY_FILTERS
            )
            if category == "saved_tablets"
        )
        query = SimpleNamespace(
            answer=AsyncMock(),
            data=f"saved_offers:category:{category_index}",
            message=message,
        )

        with patch(
            "handlers.ai_search.search_channel_posts",
            return_value=[],
        ) as search_posts:
            await saved_offers_callback(
                SimpleNamespace(callback_query=query),
                context,
            )

        query.answer.assert_awaited_once()
        search_posts.assert_called_once()
        self.assertEqual(context.user_data["ai_search_results"], [])
        self.assertEqual(context.user_data["ai_search_index"], 0)
        self.assertEqual(message.replies[-1][0], AI_SEARCH_NO_RESULTS_TEXT)

    async def test_filtered_category_results_navigate_without_reparsing(self):
        context = FakeContext()
        context.user_data.update({
            "ai_search_active": True,
            "saved_offers_menu": "categories",
        })
        posts = [
            {"id": 20, "title": "طابلات أول"},
            {"id": 21, "title": "طابلات ثان"},
        ]
        message = FakeMessage()
        message.chat = SimpleNamespace(id=123)
        category_index = next(
            index
            for index, (category, _label) in enumerate(
                SAVED_OFFER_CATEGORY_FILTERS
            )
            if category == "saved_tablets"
        )
        query = SimpleNamespace(
            answer=AsyncMock(),
            data=f"saved_offers:category:{category_index}",
            message=message,
        )

        with patch(
            "handlers.ai_search.search_channel_posts",
            return_value=posts,
        ) as search_posts, patch(
            "handlers.ai_search.send_stored_post",
            new=AsyncMock(),
        ) as send_post, patch(
            "handlers.ai_search.is_feature_enabled",
            return_value=True,
        ), patch(
            "handlers.ai_search._replace_stored_post_message",
            new=AsyncMock(),
        ) as replace_post:
            await saved_offers_callback(
                SimpleNamespace(callback_query=query),
                context,
            )

            navigation_query = SimpleNamespace(
                message=SimpleNamespace(
                    chat=SimpleNamespace(id=123),
                    message_id=456,
                    photo=None,
                    reply_text=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            navigation_update = SimpleNamespace(
                callback_query=navigation_query
            )
            await ai_next_callback(navigation_update, context)
            await ai_previous_callback(navigation_update, context)

        query.answer.assert_awaited_once()
        search_posts.assert_called_once()
        self.assertEqual(context.user_data["ai_search_results"], posts)
        self.assertEqual(context.user_data["ai_search_index"], 0)
        send_post.assert_awaited_once_with(
            123,
            context,
            posts[0],
            has_previous=False,
            has_next=True,
        )
        self.assertEqual(
            [call.args[2] for call in replace_post.await_args_list],
            [posts[1], posts[0]],
        )

    async def test_saved_offers_back_button_returns_to_main_reply_keyboard(self):
        context = FakeContext()
        context.user_data.update({
            "ai_search_active": True,
            "saved_offers_menu": "categories",
        })
        message = FakeMessage()
        query = SimpleNamespace(
            answer=AsyncMock(),
            data="saved_offers:back:main",
            message=message,
        )

        with patch(
            "handlers.ai_search.count_processed_channel_posts",
            return_value=17,
        ):
            await saved_offers_callback(
                SimpleNamespace(callback_query=query),
                context,
            )

        query.answer.assert_awaited_once()
        self.assertEqual(context.user_data["saved_offers_menu"], "main")
        self.assertEqual(
            message.replies[-1][0],
            SAVED_OFFERS_MENU_TEXT.format(count=17),
        )
        self.assertIsInstance(message.replies[-1][1], ReplyKeyboardMarkup)

    async def test_aliexpress_link_is_the_ai_exit_path(self):
        context = FakeContext()
        context.user_data["ai_search_active"] = True
        message = FakeMessage(
            "https://www.aliexpress.com/item/1005001234567890.html"
        )

        handled = await handle_ai_search_message(
            SimpleNamespace(effective_message=message),
            context,
        )

        self.assertFalse(handled)
        self.assertNotIn("ai_search_active", context.user_data)
        self.assertEqual(message.replies, [])
        self.assertNotIn("restore_ai_search_keyboard", context.user_data)

    async def test_direct_aliexpress_link_after_ai_exit_is_enqueued_once(self):
        context = FakeContext()
        context.user_data["ai_search_active"] = True
        message = FakeMessage(
            "https://www.aliexpress.com/item/1005001234567890.html"
        )
        update = SimpleNamespace(
            message=message,
            effective_message=message,
            effective_user=SimpleNamespace(first_name="Test", last_name="User"),
            effective_chat=message.chat,
        )

        with patch(
            "handlers.messages.handle_admin_message",
            new=AsyncMock(return_value=False),
        ), patch(
            "handlers.messages.record_user_activity",
        ), patch(
            "handlers.messages.handle_smart_cart_message",
            new=AsyncMock(return_value=False),
        ), patch(
            "handlers.ai_search.extract_aliexpress_url",
            return_value=message.text,
        ), patch(
            "handlers.messages.extract_aliexpress_url",
            return_value=message.text,
        ), patch(
            "handlers.messages.enqueue_url",
            new=AsyncMock(),
        ) as enqueue:
            await handle_message(update, context)

        enqueue.assert_awaited_once_with(
            message.chat.id,
            message.text,
            context,
        )
        self.assertNotIn("ai_search_active", context.user_data)

    async def test_non_aliexpress_url_exits_ai_with_warning(self):
        context = FakeContext()
        context.user_data["ai_search_active"] = True
        message = FakeMessage("https://example.com/product")

        handled = await handle_ai_search_message(
            SimpleNamespace(effective_message=message),
            context,
        )

        self.assertTrue(handled)
        self.assertNotIn("ai_search_active", context.user_data)
        self.assertEqual(
            message.replies[-1][0],
            "⚠️ من فضلك قم بإرسال روابط منتجات Aliexpress فقط ",
        )
        self.assertIsInstance(
            message.replies[-1][1],
            ReplyKeyboardMarkup,
        )

    async def test_source_photo_is_uploaded_and_cached(self):
        context = FakeContext()
        sent = SimpleNamespace(photo=[SimpleNamespace(file_id="bot-file-id")])
        context.bot = SimpleNamespace(
            send_photo=AsyncMock(return_value=sent),
            send_message=AsyncMock(),
        )
        post = {
            "id": 42,
            "content": "عرض تجريبي",
            "photo_file_id": None,
            "source_channel_id": -100,
            "source_message_id": 7,
        }

        with patch(
            "handlers.ai_search._download_source_photo",
            new=AsyncMock(return_value=BytesIO(b"image")),
        ), patch(
            "handlers.ai_search._cache_photo_file_id",
            new=AsyncMock(),
        ) as cache:
            await send_stored_post(123, context, post, has_next=False)

        uploaded = context.bot.send_photo.await_args.kwargs["photo"]
        self.assertEqual(uploaded.filename, "channel-post-42.jpg")
        cache.assert_awaited_once_with(42, "bot-file-id")

    async def test_cached_file_id_precedes_saved_link_img(self):
        context = FakeContext()
        post = {
            "id": 47,
            "photo_file_id": "telegram-cached-id",
            "link_img": "https://img.example/saved.jpg",
            "source_channel_id": -100,
            "source_message_id": 12,
        }

        with patch(
            "handlers.ai_search._download_source_photo",
            new=AsyncMock(),
        ) as download, patch(
            "handlers.ai_search.get_product_info_from_api",
        ) as api:
            photo, downloaded_source = await _load_post_photo(post, context)

        self.assertEqual(photo, "telegram-cached-id")
        self.assertFalse(downloaded_source)
        download.assert_not_awaited()
        api.assert_not_called()

    async def test_saved_link_img_precedes_source_download(self):
        context = FakeContext()
        post = {
            "id": 48,
            "photo_file_id": None,
            "link_img": "https://img.example/saved.jpg",
            "source_channel_id": -100,
            "source_message_id": 13,
        }

        with patch(
            "handlers.ai_search._download_source_photo",
            new=AsyncMock(),
        ) as download, patch(
            "handlers.ai_search.get_product_info_from_api",
        ) as api:
            photo, downloaded_source = await _load_post_photo(post, context)

        self.assertEqual(photo, "https://img.example/saved.jpg")
        self.assertFalse(downloaded_source)
        self.assertEqual(post["_resolved_photo_origin"], "link_img")
        download.assert_not_awaited()
        api.assert_not_called()

    async def test_failed_photo_file_id_uses_saved_link_img(self):
        context = FakeContext()
        sent = SimpleNamespace(
            photo=[SimpleNamespace(file_id="refreshed-photo-id")],
        )
        context.bot = SimpleNamespace(
            send_photo=AsyncMock(
                side_effect=[RuntimeError("cached file id unavailable"), sent],
            ),
            send_message=AsyncMock(),
        )
        post = {
            "id": 49,
            "content": "عرض محفوظ",
            "photo_file_id": "unavailable-photo-id",
            "link_img": "https://img.example/saved.jpg",
        }

        with patch(
            "handlers.ai_search._cache_photo_file_id",
            new=AsyncMock(),
        ) as cache, patch(
            "handlers.ai_search.get_product_info_from_api",
        ) as api:
            await send_stored_post(123, context, post)

        calls = context.bot.send_photo.await_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].kwargs["photo"], "unavailable-photo-id")
        self.assertEqual(
            calls[1].kwargs["photo"],
            "https://img.example/saved.jpg",
        )
        cache.assert_awaited_once_with(49, "refreshed-photo-id")
        api.assert_not_called()

    async def test_aliexpress_api_photo_is_used_after_source_photo_fails(self):
        context = FakeContext()
        sent = SimpleNamespace(photo=[SimpleNamespace(file_id="api-photo-file-id")])
        context.bot = SimpleNamespace(
            send_photo=AsyncMock(return_value=sent),
            send_message=AsyncMock(),
        )
        post = {
            "id": 45,
            "content": "عرض https://www.aliexpress.com/item/123.html",
            "photo_file_id": None,
            "source_channel_id": -100,
            "source_message_id": 10,
            "aliexpress_product_id": "123",
        }

        with patch(
            "handlers.ai_search._download_source_photo",
            new=AsyncMock(return_value=None),
        ), patch(
            "handlers.ai_search.get_product_info_from_api",
            return_value={"image_url": "https://img.example/api.jpg"},
        ), patch(
            "handlers.ai_search.get_product_details_scraping",
        ) as scrape, patch(
            "handlers.ai_search._cache_photo_file_id",
            new=AsyncMock(),
        ) as cache, patch(
            "handlers.ai_search.save_channel_photo_link",
        ) as save_link:
            await send_stored_post(123, context, post, has_next=False)

        self.assertEqual(
            context.bot.send_photo.await_args.kwargs["photo"],
            "https://img.example/api.jpg",
        )
        scrape.assert_not_called()
        cache.assert_awaited_once_with(45, "api-photo-file-id")
        save_link.assert_called_once_with(
            45,
            "https://img.example/api.jpg",
        )

    async def test_scraping_photo_is_used_after_api_has_no_image(self):
        context = FakeContext()
        sent = SimpleNamespace(photo=[SimpleNamespace(file_id="scraped-photo-file-id")])
        context.bot = SimpleNamespace(
            send_photo=AsyncMock(return_value=sent),
            send_message=AsyncMock(),
        )
        post = {
            "id": 46,
            "content": "عرض",
            "photo_file_id": None,
            "source_channel_id": -100,
            "source_message_id": 11,
            "aliexpress_product_id": "456",
        }

        with patch(
            "handlers.ai_search._download_source_photo",
            new=AsyncMock(return_value=None),
        ), patch(
            "handlers.ai_search.get_product_info_from_api",
            return_value={"title": "منتج", "image_url": None},
        ), patch(
            "handlers.ai_search.get_product_details_scraping",
            return_value={"image_url": "https://img.example/scraped.jpg"},
        ) as scrape, patch(
            "handlers.ai_search._cache_photo_file_id",
            new=AsyncMock(),
        ) as cache, patch(
            "handlers.ai_search.save_channel_photo_link",
        ) as save_link:
            await send_stored_post(123, context, post, has_next=False)

        self.assertEqual(
            context.bot.send_photo.await_args.kwargs["photo"],
            "https://img.example/scraped.jpg",
        )
        scrape.assert_called_once_with("456")
        cache.assert_awaited_once_with(46, "scraped-photo-file-id")
        save_link.assert_called_once_with(
            46,
            "https://img.example/scraped.jpg",
        )

    async def test_failed_saved_link_falls_back_to_source_photo(self):
        context = FakeContext()
        sent = SimpleNamespace(
            photo=[SimpleNamespace(file_id="source-photo-file-id")],
        )
        context.bot = SimpleNamespace(
            send_photo=AsyncMock(
                side_effect=[RuntimeError("saved URL unavailable"), sent],
            ),
            send_message=AsyncMock(),
        )
        post = {
            "id": 50,
            "content": "عرض محفوظ",
            "photo_file_id": None,
            "link_img": "https://img.example/unavailable.jpg",
            "source_channel_id": -100,
            "source_message_id": 15,
        }

        with patch(
            "handlers.ai_search._download_source_photo",
            new=AsyncMock(return_value=BytesIO(b"source-image")),
        ), patch(
            "handlers.ai_search._cache_photo_file_id",
            new=AsyncMock(),
        ) as cache, patch(
            "handlers.ai_search.get_product_info_from_api",
        ) as api:
            await send_stored_post(123, context, post)

        calls = context.bot.send_photo.await_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual(
            calls[0].kwargs["photo"],
            "https://img.example/unavailable.jpg",
        )
        self.assertEqual(calls[1].kwargs["photo"].filename, "channel-post-50.jpg")
        cache.assert_awaited_once_with(50, "source-photo-file-id")
        api.assert_not_called()

    async def test_concurrent_photo_requests_share_one_source_download(self):
        context = FakeContext()
        post = {
            "id": 44,
            "photo_file_id": None,
            "source_channel_id": -100,
            "source_message_id": 9,
        }
        release = asyncio.Event()
        calls = 0

        async def download(_post, _context):
            nonlocal calls
            calls += 1
            await release.wait()
            return BytesIO(b"image")

        with patch(
            "handlers.ai_search._download_source_photo",
            side_effect=download,
        ):
            first = asyncio.create_task(_load_post_photo(post, context))
            second = asyncio.create_task(_load_post_photo(post, context))
            await asyncio.sleep(0.01)
            self.assertEqual(calls, 1)
            release.set()
            self.assertEqual(await first, (await second))

    async def test_navigation_replaces_existing_text_message(self):
        context = FakeContext()
        context.bot = SimpleNamespace(
            edit_message_text=AsyncMock(),
            delete_message=AsyncMock(),
            send_message=AsyncMock(),
        )
        message = SimpleNamespace(
            chat=SimpleNamespace(id=123),
            message_id=456,
            photo=None,
        )
        query = SimpleNamespace(
            message=message,
            answer=AsyncMock(),
        )
        post = {
            "id": 43,
            "content": "عرض آخر",
            "title": "عرض آخر",
            "photo_file_id": None,
            "source_channel_id": -100,
            "source_message_id": 8,
        }

        with patch(
            "handlers.ai_search._load_post_photo",
            new=AsyncMock(return_value=(None, False)),
        ):
            await _replace_stored_post_message(
                query,
                context,
                post,
                has_previous=True,
                has_next=False,
            )

        edit_kwargs = context.bot.edit_message_text.await_args.kwargs
        buttons = edit_kwargs["reply_markup"].inline_keyboard[0]
        self.assertEqual([button.text for button in buttons], ["السابق"])

    async def test_search_keeps_complete_results_for_fast_navigation(self):
        context = FakeContext()
        context.user_data["ai_search_active"] = True
        message = FakeMessage("أرخص الهواتف")
        posts = [
            {"id": 1, "title": "هاتف أول"},
            {"id": 2, "title": "هاتف ثان"},
        ]
        update = SimpleNamespace(
            effective_message=message,
            effective_chat=SimpleNamespace(id=123),
        )

        with patch(
            "handlers.ai_search.parse_user_request",
            return_value={"request_type": "category_cheapest"},
        ), patch(
            "handlers.ai_search.search_channel_posts",
            return_value=posts,
        ), patch(
            "handlers.ai_search.send_stored_post",
            new=AsyncMock(),
        ) as send_post:
            handled = await handle_ai_search_message(update, context)

        self.assertTrue(handled)
        self.assertEqual(message.replies[0][0], "⏳ جاري تجهيز طلبك...")
        self.assertEqual(context.user_data["ai_search_results"], posts)
        self.assertEqual(context.user_data["ai_search_index"], 0)
        send_post.assert_awaited_once_with(
            123,
            context,
            posts[0],
            has_previous=False,
            has_next=True,
        )

    async def test_unsupported_long_request_keeps_existing_supported_message(self):
        context = FakeContext()
        context.user_data["ai_search_active"] = True
        message = FakeMessage(
            "السلام عليكم، أريد منك مقارنة هاتفين وتحليل الكاميرا والبطارية "
            "ثم اختيار الأفضل لي حسب استخدامي."
        )
        update = SimpleNamespace(
            effective_message=message,
            effective_chat=SimpleNamespace(id=123),
        )

        with patch(
            "handlers.ai_search.parse_user_request",
            return_value={
                "request_type": "unsupported",
                "keywords": [],
                "category": None,
                "min_price": None,
                "max_price": None,
            },
        ), patch(
            "handlers.ai_search.search_channel_posts",
            side_effect=AssertionError("unsupported requests must not search"),
        ):
            handled = await handle_ai_search_message(update, context)

        self.assertTrue(handled)
        self.assertEqual(message.replies[-1][0], AI_SEARCH_SUPPORTED_TEXT)

    async def test_next_uses_in_memory_result_without_database_lookup(self):
        context = FakeContext()
        context.user_data["ai_search_results"] = [
            {"id": 1, "title": "العرض الأول"},
            {"id": 2, "title": "العرض الثاني", "photo_file_id": "cached-photo"},
        ]
        context.user_data["ai_search_index"] = 0
        query_message = SimpleNamespace(
            chat=SimpleNamespace(id=123),
            message_id=456,
            photo=None,
            reply_text=AsyncMock(),
        )
        query = SimpleNamespace(
            message=query_message,
            answer=AsyncMock(),
        )
        update = SimpleNamespace(callback_query=query)

        with patch(
            "handlers.ai_search.get_channel_post",
            side_effect=AssertionError("navigation should not query the database"),
        ), patch(
            "handlers.ai_search._replace_stored_post_message",
            new=AsyncMock(),
        ) as replace_post:
            await ai_next_callback(update, context)

        query.answer.assert_awaited_once()
        replace_post.assert_awaited_once_with(
            query,
            context,
            context.user_data["ai_search_results"][1],
            has_previous=True,
            has_next=False,
        )
        self.assertEqual(context.user_data["ai_search_index"], 1)

    async def test_navigation_downloads_and_caches_uncached_source_photo(self):
        context = FakeContext()
        sent = SimpleNamespace(
            photo=[SimpleNamespace(file_id="next-photo-file-id")],
        )
        context.bot = SimpleNamespace(
            send_photo=AsyncMock(return_value=sent),
            edit_message_text=AsyncMock(),
            edit_message_media=AsyncMock(),
            delete_message=AsyncMock(),
            send_message=AsyncMock(),
        )
        message = SimpleNamespace(
            chat=SimpleNamespace(id=123),
            message_id=456,
            photo=[SimpleNamespace(file_id="current-photo-file-id")],
        )
        query = SimpleNamespace(message=message)
        post = {
            "id": 43,
            "content": "عرض سريع بدون صورة مخزنة",
            "photo_file_id": None,
            "source_channel_id": -100,
            "source_message_id": 8,
        }

        with patch(
            "handlers.ai_search._download_source_photo",
            new=AsyncMock(return_value=BytesIO(b"next-image")),
        ) as download, patch(
            "handlers.ai_search._cache_photo_file_id",
            new=AsyncMock(),
        ) as cache:
            await _replace_stored_post_message(
                query,
                context,
                post,
                has_previous=True,
                has_next=False,
            )

        download.assert_awaited_once_with(post, context)
        context.bot.delete_message.assert_awaited_once_with(
            chat_id=123,
            message_id=456,
        )
        context.bot.send_photo.assert_awaited_once()
        context.bot.edit_message_media.assert_not_awaited()
        cache.assert_awaited_once_with(43, "next-photo-file-id")

    async def test_failed_navigation_does_not_advance_index(self):
        context = FakeContext()
        context.user_data["ai_search_results"] = [
            {"id": 1, "title": "العرض الأول"},
            {"id": 2, "title": "العرض الثاني"},
        ]
        context.user_data["ai_search_index"] = 0
        query_message = SimpleNamespace(
            chat=SimpleNamespace(id=123),
            reply_text=AsyncMock(),
        )
        query = SimpleNamespace(
            message=query_message,
            answer=AsyncMock(),
        )
        update = SimpleNamespace(callback_query=query)

        with patch(
            "handlers.ai_search._replace_stored_post_message",
            new=AsyncMock(side_effect=RuntimeError("Telegram error")),
        ):
            await ai_next_callback(update, context)

        self.assertEqual(context.user_data["ai_search_index"], 0)
        query_message.reply_text.assert_awaited_once()

    def test_navigation_buttons_are_side_by_side(self):
        markup = _navigation_keyboard(has_previous=True, has_next=True)

        self.assertEqual(
            [button.text for button in markup.inline_keyboard[0]],
            ["السابق", "التالي"],
        )


class DatabaseSearchTests(unittest.TestCase):
    def test_shipping_country_aliases_match_whole_tokens_only(self):
        self.assertTrue(
            db._shipping_country_matches(
                "United States",
                "united states",
                "Shipping to US",
            )
        )
        self.assertFalse(
            db._shipping_country_matches(
                None,
                "united states",
                "ASUS phone with global shipping",
            )
        )
        self.assertFalse(
            db._shipping_country_matches(
                None,
                "morocco",
                "Smart watch with global shipping",
            )
        )

    @patch.object(db, "DATABASE_URL", "postgres://test")
    @patch.object(db, "get_db_connection")
    def test_specific_product_search_uses_title_and_specific_keyword(
        self,
        get_connection,
    ):
        connection = get_connection.return_value
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = []

        db.search_channel_posts(
            {
                "request_type": "product_best",
                "keywords": ["هاتف", "iPhone 15", "phone"],
            },
            limit=3,
        )

        query, params = cursor.execute.call_args.args
        self.assertNotIn("content ILIKE", query)
        self.assertNotIn("LIMIT", query)
        self.assertIn("link_img", query)
        self.assertEqual(params, [])

    @patch.object(db, "DATABASE_URL", "postgres://test")
    @patch.object(db, "get_db_connection")
    def test_get_channel_post_returns_persisted_image_link(self, get_connection):
        connection = get_connection.return_value
        cursor = connection.cursor.return_value.__enter__.return_value
        columns = [
            "id",
            "title",
            "price",
            "content",
            "processing_status",
            "source_link",
            "published_at",
            "photo_file_id",
            "link_img",
            "source_channel_id",
            "source_message_id",
            "aliexpress_product_id",
            "shipping_country",
            "offer_type",
            "category",
            "first_level_category_name",
            "second_level_category_name",
            "category_source",
            "source_analize",
        ]
        cursor.description = [(column,) for column in columns]
        cursor.fetchone.return_value = (
            12,
            "عرض محفوظ",
            20,
            "نص العرض",
            "processed",
            None,
            None,
            None,
            "https://img.example/saved.jpg",
            -100,
            15,
            "product-12",
            "dz",
            "normal",
            "other",
            None,
            None,
            "ai",
            None,
        )

        post = db.get_channel_post(12)

        self.assertEqual(post["link_img"], "https://img.example/saved.jpg")
        self.assertEqual(
            post["photo_file_id"],
            None,
        )

    @patch.object(db, "DATABASE_URL", "postgres://test")
    @patch.object(db, "get_db_connection")
    def test_update_channel_post_saves_link_img(self, get_connection):
        connection = get_connection.return_value
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.rowcount = 1

        updated = db.update_channel_post(
            12,
            link_img="https://img.example/saved.jpg",
        )

        self.assertTrue(updated)
        query, params = cursor.execute.call_args.args
        self.assertIn("SET link_img = %s", query)
        self.assertEqual(
            params,
            ["https://img.example/saved.jpg", 12],
        )

    @patch.object(db, "DATABASE_URL", "postgres://test")
    @patch.object(db, "get_db_connection")
    @patch("core.ai.get_gemini_keyword_cache_snapshot")
    @patch("core.ai._call_ai")
    def test_gemini_vehicle_scanner_finds_cached_offer_in_other_taxonomy(
        self,
        call_ai,
        get_snapshot,
        get_connection,
    ):
        from core.ai import parse_user_request
        from core.ai_service import AIResponse

        title = (
            "Adaptateur scanner OBD2 L17 Bluetooth sans fil – lecteur de "
            "codes OBD II, réinitialisation et effacement du voyant moteur"
        )
        phrases = db._keyword_cache_phrases(title)
        cached_phrase = "lecteur_de_codes_OBD_II"
        call_ai.return_value = AIResponse(
            {
                "request_type": "product_best",
                "keywords": [cached_phrase],
                "keyword_groups": [[cached_phrase]],
                "category": "other",
            },
            provider="Gemini",
        )
        call_ai.return_value.keyword_cache_active = True
        get_snapshot.return_value = {"keywords": phrases}

        intent = parse_user_request("عروض جهاز فحص السيارات")

        connection = get_connection.return_value
        cursor = connection.cursor.return_value.__enter__.return_value
        columns = [
            "id",
            "title",
            "price",
            "content",
            "processing_status",
            "source_link",
            "published_at",
            "photo_file_id",
            "source_channel_id",
            "source_message_id",
            "aliexpress_product_id",
            "shipping_country",
            "offer_type",
            "category",
            "first_level_category_name",
            "second_level_category_name",
        ]
        cursor.description = [(column,) for column in columns]
        cursor.fetchall.return_value = [
            (
                1,
                title,
                18,
                None,
                "processed",
                None,
                None,
                None,
                None,
                None,
                "obd-local",
                None,
                "normal",
                None,
                "Automobiles & Motorcycles",
                "Car Electronics",
            )
        ]

        posts = db.search_channel_posts(intent)

        self.assertEqual(intent["keywords"], ["lecteur de codes OBD II"])
        self.assertEqual(intent["category"], "other")
        self.assertEqual([post["title"] for post in posts], [title])
        self.assertIsNone(
            db._post_match_score(
                {
                    "title": title,
                    "first_level_category_name": "Automobiles & Motorcycles",
                    "second_level_category_name": "Car Electronics",
                },
                {**intent, "category": "phones"},
                intent["keywords"],
            )
        )

    def test_cached_product_concept_groups_remain_or_within_and_between(self):
        from core.db import _post_match_score

        intent = {
            "request_type": "product_best",
            "keywords": [
                "automotive diagnostic scanner",
                "bluetooth adapter",
            ],
            "category": None,
            "keyword_groups": [
                [
                    "automotive diagnostic scanner",
                    "obd code reader",
                ],
                [
                    "bluetooth adapter",
                    "wireless receiver",
                ],
            ],
        }
        title_with_each_concept = {
            "title": "Automotive diagnostic scanner with Bluetooth adapter",
            "price": 18,
        }
        title_missing_second_concept = {
            "title": "OBD code reader for cars",
            "price": 18,
        }

        self.assertIsNotNone(
            _post_match_score(
                title_with_each_concept,
                intent,
                intent["keywords"],
            )
        )
        self.assertIsNone(
            _post_match_score(
                title_missing_second_concept,
                intent,
                intent["keywords"],
            )
        )

    @patch.object(db, "DATABASE_URL", "postgres://test")
    @patch.object(db, "get_db_connection")
    @patch("core.ai.get_gemini_keyword_cache_snapshot")
    @patch("core.ai._call_ai")
    def test_non_gemini_intent_searches_with_semantic_cache_match(
        self,
        call_ai,
        get_snapshot,
        get_connection,
    ):
        from core.ai import parse_user_request
        from core.ai_service import AIResponse

        title = (
            "Adaptateur scanner OBD2 L17 Bluetooth sans fil – lecteur de "
            "codes OBD II, réinitialisation et effacement du voyant moteur"
        )
        cached_phrase = "lecteur_de_codes_OBD_II"
        phrases = db._keyword_cache_phrases(title)
        get_snapshot.return_value = {"keywords": phrases}

        connection = get_connection.return_value
        cursor = connection.cursor.return_value.__enter__.return_value
        columns = [
            "id",
            "title",
            "price",
            "content",
            "processing_status",
            "source_link",
            "published_at",
            "photo_file_id",
            "source_channel_id",
            "source_message_id",
            "aliexpress_product_id",
            "shipping_country",
            "offer_type",
            "category",
            "first_level_category_name",
            "second_level_category_name",
        ]
        cursor.description = [(column,) for column in columns]
        cursor.fetchall.return_value = [
            (
                1,
                title,
                18,
                None,
                "processed",
                None,
                None,
                None,
                None,
                None,
                "obd-local",
                None,
                "normal",
                None,
                "Automobiles & Motorcycles",
                "Car Electronics",
            )
        ]

        requests = [
            ("Groq", "عروض جهاز فحص السيارات", "جهاز فحص السيارات"),
            (
                "OpenRouter",
                "Je cherche un lecteur de codes de voiture",
                "lecteur de codes",
            ),
            (
                "Cloudflare",
                "Find a car diagnostic scanner",
                "car diagnostic scanner",
            ),
        ]
        for provider_name, user_text, primary_keyword in requests:
            with self.subTest(user_text=user_text):
                primary_result = AIResponse(
                    {
                        "request_type": "product_best",
                        "keywords": [primary_keyword],
                        "category": "other",
                    },
                    provider=provider_name,
                )
                semantic_result = AIResponse(
                    {
                        "keywords": [cached_phrase],
                        "keyword_groups": [[cached_phrase]],
                    },
                    provider="Gemini",
                )
                semantic_result.keyword_cache_active = True
                semantic_result.keyword_cache_keywords = phrases
                call_ai.side_effect = [primary_result, semantic_result]

                intent = parse_user_request(user_text)
                posts = db.search_channel_posts(intent)

                self.assertEqual(
                    intent["keywords"],
                    ["lecteur de codes OBD II"],
                )
                self.assertEqual(intent["category"], "other")
                self.assertEqual([post["title"] for post in posts], [title])
        self.assertEqual(call_ai.call_count, 6)
        self.assertEqual(
            call_ai.call_args.kwargs["provider_name"],
            "Gemini",
        )

    @patch.object(db, "DATABASE_URL", "postgres://test")
    @patch.object(db, "get_db_connection")
    @patch("core.ai.get_gemini_keyword_cache_snapshot")
    @patch("core.ai._call_ai")
    def test_closed_vocabulary_category_misclass_still_finds_product_offer(
        self,
        call_ai,
        get_snapshot,
        get_connection,
    ):
        from core.ai import parse_user_request
        from core.ai_service import AIResponse

        title = "Mini machine à laver portable et pliable, capacité 8 L"
        cached_phrase = "Mini machine à laver portable"
        get_snapshot.return_value = {"keywords": [cached_phrase]}

        connection = get_connection.return_value
        cursor = connection.cursor.return_value.__enter__.return_value
        columns = [
            "id",
            "title",
            "price",
            "content",
            "processing_status",
            "source_link",
            "published_at",
            "photo_file_id",
            "source_channel_id",
            "source_message_id",
            "aliexpress_product_id",
            "shipping_country",
            "offer_type",
            "category",
            "first_level_category_name",
            "second_level_category_name",
        ]
        cursor.description = [(column,) for column in columns]
        cursor.fetchall.return_value = [
            (
                1, title, 30, None, "processed", None, None, None, None, None,
                "washer-good", "Algeria", "bundle", None, "Home & Garden",
                "Home Appliances",
            ),
            (
                2, title, 45, None, "processed", None, None, None, None, None,
                "washer-too-expensive", "Algeria", "bundle", None,
                "Home & Garden", "Home Appliances",
            ),
            (
                3, title, 20, None, "processed", None, None, None, None, None,
                "washer-wrong-country", "Morocco", "bundle", None,
                "Home & Garden", "Home Appliances",
            ),
            (
                4, title, 20, None, "processed", None, None, None, None, None,
                "washer-wrong-offer-type", "Algeria", "normal", None,
                "Home & Garden", "Home Appliances",
            ),
            (
                5,
                "Foldable Laundry Basket Storage Organizer for Clothes",
                15, None, "processed", None, None, None, None, None,
                "laundry-basket", "Algeria", "bundle", None, "Home & Garden",
                "Laundry Products",
            ),
        ]
        user_text = (
            "أريد عروض باندل لغسالة الملابس المحمولة "
            "أقل من 40 دولار وتشحن إلى الجزائر"
        )

        for provider_name in ("Gemini", "DeepSeek"):
            with self.subTest(provider=provider_name):
                primary_result = AIResponse(
                    {
                        "request_type": "category_cheapest",
                        "keywords": [],
                        "category": "home",
                        "max_price": 40,
                        "price_mode": "below",
                        "shipping_country": "Algeria",
                        "offer_type": "bundle",
                        "category_filter_requested": True,
                    },
                    provider=provider_name,
                )
                primary_result.keyword_cache_active = True
                primary_result.keyword_cache_keywords = [cached_phrase]
                semantic_result = AIResponse(
                    {
                        "keywords": [cached_phrase],
                        "keyword_groups": [[cached_phrase]],
                    },
                    provider=provider_name,
                )
                semantic_result.keyword_cache_active = True
                semantic_result.keyword_cache_keywords = [cached_phrase]
                call_ai.side_effect = [primary_result, semantic_result]

                intent = parse_user_request(user_text)
                posts = db.search_channel_posts(intent)

                self.assertEqual(intent["request_type"], "product_price_range")
                self.assertEqual(intent["keywords"], [cached_phrase])
                self.assertIsNone(intent["category"])
                self.assertEqual(intent["max_price"], 40)
                self.assertEqual(intent["shipping_country"], "Algeria")
                self.assertEqual(intent["offer_type"], "bundle")
                self.assertEqual([post["title"] for post in posts], [title])
                query = cursor.execute.call_args.args[0]
                self.assertIn("processing_status = 'processed'", query)
                self.assertIn("price <= %s", query)
                self.assertIn("offer_type", query)
                self.assertEqual(call_ai.call_count, 2)
                call_ai.reset_mock()

    @patch.object(db, "DATABASE_URL", "postgres://test")
    @patch.object(db, "get_db_connection")
    def test_trending_search_returns_one_cheapest_offer_per_product(
        self,
        get_connection,
    ):
        connection = get_connection.return_value
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.description = [
            (name,) for name in (
                "id",
                "title",
                "price",
                "content",
                "processing_status",
                "source_link",
                "published_at",
                "photo_file_id",
                "source_channel_id",
                "source_message_id",
                "aliexpress_product_id",
            )
        ]
        cursor.fetchall.return_value = []

        self.assertEqual(
            db.search_channel_posts({"request_type": "trending"}, limit=50),
            [],
        )

        query, params = cursor.execute.call_args.args
        self.assertIn("ROW_NUMBER()", query)
        self.assertIn("WHERE product_rank = 1", query)
        self.assertIn("product_occurrences DESC", query)
        self.assertEqual(params, [50])

    @patch.object(db, "DATABASE_URL", "postgres://test")
    @patch.object(db, "get_db_connection")
    def test_unlimited_trending_search_returns_all_representatives(
        self,
        get_connection,
    ):
        connection = get_connection.return_value
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.description = [
            (name,) for name in (
                "id",
                "title",
                "price",
                "content",
                "processing_status",
                "source_link",
                "published_at",
                "photo_file_id",
                "source_channel_id",
                "source_message_id",
                "aliexpress_product_id",
            )
        ]
        cursor.fetchall.return_value = [
            (
                index,
                f"Product {index}",
                index,
                "",
                "processed",
                None,
                None,
                None,
                None,
                None,
                f"product-{index}",
            )
            for index in range(1, 26)
        ]

        result = db.search_channel_posts({"request_type": "trending"})

        self.assertEqual(len(result), 25)
        query, params = cursor.execute.call_args.args
        self.assertNotIn("LIMIT", query)
        self.assertEqual(params, [])

    @patch.object(db, "DATABASE_URL", "postgres://test")
    @patch.object(db, "get_db_connection")
    @patch.object(db, "_post_match_score", return_value=1.0)
    def test_unlimited_standard_search_returns_all_ranked_posts(
        self,
        _match_score,
        get_connection,
    ):
        connection = get_connection.return_value
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.description = [
            (name,) for name in (
                "id",
                "title",
                "price",
                "content",
                "processing_status",
                "source_link",
                "published_at",
                "photo_file_id",
                "source_channel_id",
                "source_message_id",
                "aliexpress_product_id",
            )
        ]
        cursor.fetchall.return_value = [
            (
                index,
                f"Product {index}",
                index,
                "",
                "processed",
                None,
                None,
                None,
                None,
                None,
                f"product-{index}",
            )
            for index in range(1, 26)
        ]

        result = db.search_channel_posts({
            "request_type": "product_best",
            "keywords": ["product"],
        })

        self.assertEqual(len(result), 25)

    @patch.object(db, "_post_match_score", return_value=1.0)
    def test_unlimited_filtered_trending_keeps_all_representatives(
        self,
        _match_score,
    ):
        posts = [
            {
                "id": index,
                "title": f"Product {index}",
                "price": index,
                "aliexpress_product_id": f"product-{index}",
            }
            for index in range(1, 26)
        ]

        result = db._rank_filtered_trending_posts(
            posts,
            {"request_type": "trending", "category": "phones"},
            [],
            None,
        )

        self.assertEqual(len(result), 25)

    @patch.object(db, "DATABASE_URL", "postgres://test")
    @patch.object(db, "get_db_connection")
    def test_filtered_trending_search_applies_category_before_grouping(
        self,
        get_connection,
    ):
        connection = get_connection.return_value
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.description = [
            (name,) for name in (
                "id",
                "title",
                "price",
                "content",
                "processing_status",
                "source_link",
                "published_at",
                "photo_file_id",
                "source_channel_id",
                "source_message_id",
                "aliexpress_product_id",
            )
        ]
        rows = [
            (
                1, "iPhone 15 Pro 256GB", 500, "phone", "processed",
                "phone-1", None, None, None, None, "phone-1",
            ),
            (
                2, "iPhone 15 Pro 256GB", 450, "phone", "processed",
                "phone-2", None, None, None, None, "phone-1",
            ),
            (
                3, "UGREEN USB-C Charging Cable for Huawei Phones", 5,
                "cable", "processed", "cable-1", None, None, None, None,
                "cable-1",
            ),
            (
                4, "Bluetooth Earbuds", 20, "audio", "processed",
                "audio-1", None, None, None, None, "audio-1",
            ),
        ]
        cursor.fetchall.return_value = rows

        result = db.search_channel_posts(
            {
                "request_type": "trending",
                "category": "phones",
                "keywords": ["هواتف", "phones"],
            },
            limit=10,
        )

        self.assertEqual([post["id"] for post in result], [2])
        query, params = cursor.execute.call_args.args
        self.assertNotIn("ROW_NUMBER()", query)
        self.assertNotIn("LIMIT", query)
        self.assertEqual(params, [])

    @patch.object(db, "DATABASE_URL", "postgres://test")
    @patch.object(db, "get_db_connection")
    def test_near_price_search_scans_all_eligible_offers_and_ranks_by_distance(
        self,
        get_connection,
    ):
        connection = get_connection.return_value
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.description = [
            (name,) for name in (
                "id", "title", "price", "content", "processing_status",
                "source_link", "published_at", "photo_file_id",
                "source_channel_id", "source_message_id",
                "aliexpress_product_id", "shipping_country", "category",
                "first_level_category_name", "second_level_category_name",
                "category_source",
            )
        ]
        rows = [
            (
                post_id,
                f"Recent product model {post_id}",
                160,
                "",
                "processed",
                None,
                None,
                None,
                None,
                None,
                f"recent-{post_id}",
                None,
                "other",
                None,
                None,
                "ai",
            )
            for post_id in range(1, 2001)
        ]
        rows.append(
            (
                2001, "Older exact product", 100, "", "processed", None, None,
                None, None, None, "older-exact", None, "other", None, None,
                "ai",
            )
        )
        cursor.fetchall.return_value = rows

        result = db.search_channel_posts(
            {
                "request_type": "trending",
                "price_mode": "near",
                "target_price": 100,
            },
            limit=10,
        )

        query, params = cursor.execute.call_args.args
        self.assertNotIn("LIMIT", query)
        self.assertEqual(params, [])
        self.assertEqual(result[0]["id"], 2001)

    @patch.object(db, "DATABASE_URL", "postgres://test")
    @patch.object(db, "get_db_connection")
    def test_cleanup_uses_two_day_retention_window(self, get_connection):
        connection = get_connection.return_value
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.rowcount = 2

        deleted = db.delete_expired_channel_posts()

        self.assertEqual(deleted, 2)
        query = cursor.execute.call_args.args[0]
        self.assertIn("CURRENT_TIMESTAMP - INTERVAL '2 days'", query)
        self.assertNotIn("processing_status = 'processed'", query)

    def test_category_and_typo_matching_stay_in_the_requested_product_family(self):
        from core.db import _post_match_score

        phone_intent = {
            "request_type": "category_price_range",
            "category": "phones",
        }
        self.assertIsNotNone(
            _post_match_score(
                {"title": "iPhone 15 Pro"},
                phone_intent,
                ["هواتف", "phones"],
            )
        )
        self.assertIsNone(
            _post_match_score(
                {"title": "Bluetooth Earbuds"},
                phone_intent,
                ["هواتف", "phones"],
            )
        )

        product_intent = {"request_type": "product_best"}
        self.assertIsNotNone(
            _post_match_score(
                {"title": "iPhone 15 Pro"},
                product_intent,
                ["iphnoe 15"],
            )
        )

    def test_shipping_country_is_an_required_post_filter(self):
        from core.db import _post_match_score

        intent = {
            "request_type": "category_price_range",
            "category": "phones",
            "shipping_country": "Algeria",
        }
        self.assertIsNotNone(
            _post_match_score(
                {
                    "title": "iPhone 15 Pro",
                    "shipping_country": "Algeria",
                },
                intent,
                ["هواتف"],
            )
        )
        self.assertIsNone(
            _post_match_score(
                {
                    "title": "iPhone 15 Pro",
                    "shipping_country": "Morocco",
                },
                intent,
                ["هواتف"],
            )
        )
        self.assertIsNone(
            _post_match_score(
                {"title": "iPhone 15 Pro"},
                intent,
                ["هواتف"],
            )
        )

    @patch.object(db, "DATABASE_URL", "postgres://test")
    @patch.object(db, "get_db_connection")
    def test_price_range_returns_cheapest_offer_inside_the_range(
        self,
        get_connection,
    ):
        connection = get_connection.return_value
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.description = [
            (name,) for name in (
                "id",
                "title",
                "price",
                "content",
                "processing_status",
                "source_link",
                "published_at",
                "photo_file_id",
                "source_channel_id",
                "source_message_id",
                "aliexpress_product_id",
                "shipping_country",
            )
        ]
        rows = [
            (
                1, "iPhone 15 Pro", 180, "phone", "processed",
                "phone-1", None, None, None, None, "phone-1", "Algeria",
            ),
            (
                2, "Samsung S24", 120, "phone", "processed",
                "phone-2", None, None, None, None, "phone-2", "Algeria",
            ),
        ]
        cursor.fetchall.return_value = rows

        result = db.search_channel_posts(
            {
                "request_type": "category_price_range",
                "category": "phones",
                "keywords": ["هواتف", "phones"],
                "min_price": 100,
                "max_price": 200,
            },
            limit=10,
        )

        self.assertEqual([post["id"] for post in result], [2, 1])

    def test_category_search_requires_all_explicit_phone_specs(self):
        from core.db import _post_match_score

        intent = {
            "request_type": "category_price_range",
            "category": "phones",
            "required_specs": [
                {"type": "storage", "value": "256GB"},
                {"type": "ram", "value": "12GB"},
            ],
        }

        self.assertIsNotNone(
            _post_match_score(
                {"title": "Xiaomi 12GB RAM 256GB ROM Smartphone"},
                intent,
                ["هواتف"],
            )
        )
        self.assertIsNone(
            _post_match_score(
                {"title": "Xiaomi 8GB RAM 256GB ROM Smartphone"},
                intent,
                ["هواتف"],
            )
        )
        self.assertIsNone(
            _post_match_score(
                {"title": "UGREEN 6A 100W USB-C Charging Cable for Huawei Phones"},
                {
                    "request_type": "category_cheapest",
                    "category": "phones",
                    "required_specs": [],
                },
                ["هواتف"],
            )
        )

    def test_specific_product_search_rejects_a_different_numeric_model(self):
        from core.db import _post_match_score

        intent = {
            "request_type": "product_best",
            "category": "phones",
        }
        self.assertIsNotNone(
            _post_match_score(
                {"title": "Samsung Galaxy S24 Ultra 256GB"},
                intent,
                ["Samsung", "S24", "Ultra"],
            )
        )
        self.assertIsNone(
            _post_match_score(
                {"title": "Samsung Galaxy S23 Ultra 256GB"},
                intent,
                ["Samsung", "S24", "Ultra"],
            )
        )

    def test_product_category_uses_saved_classification_to_separate_haylou_items(
        self,
    ):
        from core.db import _post_match_score

        intent = {
            "request_type": "product_cheapest",
            "category": "headphones",
        }
        self.assertIsNotNone(
            _post_match_score(
                {
                    "title": "Haylou X1 Neo",
                    "category": "headphones",
                },
                intent,
                ["سماعات", "Haylou"],
            )
        )
        self.assertIsNone(
            _post_match_score(
                {
                    "title": "Haylou RS4 Plus",
                    "category": "watches",
                },
                intent,
                ["سماعات", "Haylou"],
            )
        )

    def test_saved_category_is_used_for_category_and_trending_searches(self):
        from core.db import _post_match_score

        opaque_headphones = {
            "title": "Haylou X1 Neo",
            "category": "headphones",
        }
        category_intent = {
            "request_type": "category_cheapest",
            "category": "headphones",
        }
        trending_intent = {
            "request_type": "trending",
            "category": "headphones",
        }
        self.assertIsNotNone(
            _post_match_score(
                opaque_headphones,
                category_intent,
                ["سماعات"],
            )
        )
        self.assertIsNotNone(
            _post_match_score(
                opaque_headphones,
                trending_intent,
                ["سماعات"],
            )
        )

    def test_saved_category_overrides_compatibility_words_in_title(self):
        from core.db import _post_match_score

        self.assertIsNotNone(
            _post_match_score(
                {
                    "title": "Haylou earbuds for iPhone",
                    "category": "headphones",
                },
                {
                    "request_type": "category_cheapest",
                    "category": "headphones",
                },
                ["سماعات"],
            )
        )
        self.assertIsNotNone(
            _post_match_score(
                {
                    "title": "USB cable for phones",
                    "category": "accessories",
                },
                {
                    "request_type": "category_cheapest",
                    "category": "accessories",
                },
                ["اكسسوارات"],
            )
        )

    def test_official_aliexpress_category_overrides_ai_title_classification(self):
        from core.db import _post_match_score

        accessory = {
            "title": "Premium mobile phone protective product",
            "category": "phones",
            "first_level_category_name": "Phones & Telecommunications",
            "second_level_category_name": "Mobile Phone Accessories",
        }
        self.assertIsNone(
            _post_match_score(
                accessory,
                {
                    "request_type": "category_cheapest",
                    "category": "phones",
                },
                ["هواتف"],
            )
        )
        self.assertIsNotNone(
            _post_match_score(
                accessory,
                {
                    "request_type": "category_cheapest",
                    "category": "accessories",
                },
                ["اكسسوارات"],
            )
        )

    def test_official_category_alternatives_are_or_with_price_still_and(self):
        from core.db import _post_match_score

        intent = {
            "request_type": "category_price_range",
            "category": "phones",
            "official_categories": ["Mobile Phones", "Tablets"],
            "max_price": 100,
        }
        phone = {
            "title": "Phone offer",
            "first_level_category_name": "Phones & Telecommunications",
            "second_level_category_name": "Mobile Phones",
            "price": 80,
        }
        tablet = {
            "title": "Tablet offer",
            "first_level_category_name": "Consumer Electronics",
            "second_level_category_name": "Tablets",
            "price": 80,
        }
        over_budget_phone = {**phone, "price": 150}
        unrelated = {
            "title": "Kitchen offer",
            "first_level_category_name": "Home Appliances",
            "second_level_category_name": "Kitchen Appliances",
            "price": 80,
        }

        self.assertIsNotNone(_post_match_score(phone, intent, []))
        self.assertIsNotNone(_post_match_score(tablet, intent, []))
        self.assertIsNone(_post_match_score(over_budget_phone, intent, []))
        self.assertIsNone(_post_match_score(unrelated, intent, []))

    def test_exact_product_with_unmapped_official_category_uses_title_category(self):
        from core.db import _post_match_score

        cached_phrase = "HAYLOU_Solar_Neo_2_Voice_Calling_Smartwatch"
        post = {
            "title": (
                "HAYLOU Solar Neo 2 Voice Calling Smartwatch – 1.43 HD "
                "AMOLED display"
            ),
            "category": "other",
            "first_level_category_name": "Consumer Electronics",
            "second_level_category_name": "Smart Electronics",
        }
        exact_intent = {
            "request_type": "product_best",
            "category": "watches",
            "keywords": [cached_phrase],
            "keyword_groups": [[cached_phrase]],
        }

        self.assertIsNotNone(
            _post_match_score(post, exact_intent, [cached_phrase])
        )

        wrong_category_intent = {
            **exact_intent,
            "category": "phones",
        }
        self.assertIsNone(
            _post_match_score(post, wrong_category_intent, [cached_phrase])
        )

        non_exact_intent = {
            **exact_intent,
            "keywords": ["Solar Calling Smartwatch"],
            "keyword_groups": [["Solar Calling Smartwatch"]],
        }
        self.assertIsNone(
            _post_match_score(
                post,
                non_exact_intent,
                non_exact_intent["keywords"],
            )
        )

        category_intent = {
            "request_type": "category_cheapest",
            "category": "watches",
        }
        self.assertIsNone(
            _post_match_score(post, category_intent, ["watches"])
        )

    def test_post_filter_enforces_both_price_range_boundaries(self):
        from core.db import _post_match_score

        intent = {
            "request_type": "category_price_range",
            "category": "phones",
            "min_price": 100,
            "max_price": 150,
        }
        self.assertIsNotNone(
            _post_match_score(
                {"title": "Samsung phone", "price": 125},
                intent,
                ["هواتف"],
            )
        )
        for price in (99.99, 150.01, None):
            with self.subTest(price=price):
                self.assertIsNone(
                    _post_match_score(
                        {"title": "Samsung phone", "price": price},
                        intent,
                        ["هواتف"],
                    )
                )

    def test_redmi_numeric_model_does_not_match_other_redmi_models(self):
        from core.db import _post_match_score

        intent = {
            "request_type": "product_best",
            "category": "phones",
        }
        self.assertIsNotNone(
            _post_match_score(
                {"title": "Xiaomi Redmi 17 5G Smartphone"},
                intent,
                ["redmi", "17"],
            )
        )
        for title in ("Redmi 16 Pro", "Redmi Note 14", "Redmi 13C"):
            with self.subTest(title=title):
                self.assertIsNone(
                    _post_match_score(
                        {"title": title},
                        intent,
                        ["redmi", "17"],
                    )
                )

    def test_other_category_is_isolated_by_saved_category(self):
        from core.db import _post_match_score

        intent = {
            "request_type": "category_cheapest",
            "category": "other",
        }
        self.assertIsNotNone(
            _post_match_score(
                {"title": "Special item", "category": "other"},
                intent,
                ["other"],
            )
        )
        self.assertIsNone(
            _post_match_score(
                {"title": "iPhone 15", "category": "phones"},
                intent,
                ["other"],
            )
        )

    def test_legacy_haylou_models_are_classified_without_an_ai_call(self):
        from core.db import _classify_legacy_post_category

        self.assertEqual(
            _classify_legacy_post_category("Haylou X1 Neo"),
            "headphones",
        )
        self.assertEqual(
            _classify_legacy_post_category("Haylou RS4 Plus"),
            "watches",
        )
        self.assertEqual(
            _classify_legacy_post_category("USB cable for phones"),
            "accessories",
        )
        for title in (
            "iPhone 15 Pro with free charger",
            "Samsung Galaxy S24 phone case included",
            "Xiaomi Redmi Note 13 USB cable included",
        ):
            with self.subTest(title=title):
                self.assertEqual(
                    _classify_legacy_post_category(title),
                    "phones",
                )
        self.assertEqual(
            _classify_legacy_post_category("Case for iPhone 15"),
            "accessories",
        )
        for title in (
            "Baseus Case for iPhone 15",
            "UGREEN cable for iPhone 15",
            "Spigen cover for Samsung S24",
            "Anker charger for Samsung Galaxy",
        ):
            with self.subTest(title=title):
                self.assertEqual(
                    _classify_legacy_post_category(title),
                    "accessories",
                )
    def test_below_price_dedup_keeps_the_offer_nearest_to_the_limit(self):
        from core.db import _deduplicate_ranked_posts

        result = _deduplicate_ranked_posts(
            [
                {"id": 1, "aliexpress_product_id": "same", "price": 20},
                {"id": 2, "aliexpress_product_id": "same", "price": 90},
            ],
            10,
            {"price_mode": "below", "max_price": 100},
        )

        self.assertEqual([post["id"] for post in result], [2])

    def test_broad_dedup_groups_same_model_across_different_listing_ids(self):
        from core.db import _deduplicate_ranked_posts

        result = _deduplicate_ranked_posts(
            [
                {
                    "id": 1,
                    "aliexpress_product_id": "listing-a",
                    "title": "Global Xiaomi Redmi Note 13 Pro 5G 8GB 256GB",
                    "price": 180,
                },
                {
                    "id": 2,
                    "aliexpress_product_id": "listing-b",
                    "title": "Xiaomi Redmi Note13 Pro 5G Smartphone 256GB",
                    "price": 165,
                },
            ],
            10,
            {},
        )

        self.assertEqual([post["id"] for post in result], [2])

    def test_broad_model_identity_ignores_listing_specs_and_brand_aliases(self):
        from core.db import _broad_product_identity

        equivalent_pairs = (
            (
                "Xiaomi Redmi Note 13 Pro 5G 8GB 256GB Android 14 6.67 inch",
                "Redmi Note13 Pro Global Version 256GB",
            ),
            (
                "Apple iPhone 15 Pro Max 256GB",
                "iPhone 15 Pro Max Global Unlocked",
            ),
            (
                "Samsung Galaxy S24 Ultra Android 14 6.8 inch",
                "Samsung S24 Ultra 12GB 256GB",
            ),
            (
                "Redmi Note 13 Pro+ 5G",
                "Xiaomi Redmi Note13 Pro Plus 5G Smartphone",
            ),
        )
        for first_title, second_title in equivalent_pairs:
            with self.subTest(first_title=first_title):
                self.assertEqual(
                    _broad_product_identity({"title": first_title}),
                    _broad_product_identity({"title": second_title}),
                )

    def test_directional_price_ordering_uses_the_requested_anchor(self):
        from core.db import _price_order_key
        from decimal import Decimal

        below_posts = [{"price": 80}, {"price": 100}]
        self.assertEqual(
            [post["price"] for post in sorted(
                below_posts,
                key=lambda post: _price_order_key(
                    post,
                    {"price_mode": "below"},
                    1.0,
                    0,
                ),
            )],
            [100, 80],
        )
        above_posts = [{"price": 120}, {"price": 100}]
        self.assertEqual(
            [post["price"] for post in sorted(
                above_posts,
                key=lambda post: _price_order_key(
                    post,
                    {"price_mode": "above"},
                    1.0,
                    0,
                ),
            )],
            [100, 120],
        )
        near_posts = [{"price": 80}, {"price": 120}, {"price": 100}]
        self.assertEqual(
            [post["price"] for post in sorted(
                [{"price": Decimal("80.00")}, {"price": Decimal("120.00")}, {"price": Decimal("100.00")}],
                key=lambda post: _price_order_key(
                    post,
                    {"price_mode": "near", "target_price": 100},
                    1.0,
                    0,
                ),
            )],
            [100, 80, 120],
        )
if __name__ == "__main__":
    unittest.main()
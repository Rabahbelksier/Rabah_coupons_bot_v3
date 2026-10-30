import unittest
import os
import re
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

# The application configuration validates these values during import.
for name in ("APP_KEY", "APP_SECRET", "TRACKING_ID", "TELEGRAM_TOKEN"):
    os.environ.setdefault(name, "test")

from telegram import ReplyKeyboardMarkup

from handlers.admin import (
    ADMIN_CALLBACK_PATTERN,
    TELEGRAM_MESSAGE_LIMIT,
    _parse_cart_line,
    _format_error_log_file,
    _format_error_period_report,
    _format_ai_duration,
    _format_ai_reset_line,
    _format_ai_usage_line,
    _format_channel_queue_status,
    _cleanup_report_text,
    _render_admin_report_sections,
    _scan_inaccessible_users,
    admin_callback,
    admin_actions_keyboard,
    admin_broadcast_confirmation_keyboard,
    admin_cleanup_confirmation_keyboard,
    admin_command,
    admin_menu_keyboard,
    admin_tools_back_keyboard,
    admin_tools_keyboard,
    admin_users_keyboard,
    ai_service_keyboard,
    ai_service_details_keyboard,
    format_cart_row,
    feature_toggle_keyboard,
    handle_admin_message,
    logs_keyboard,
    _render_admin_message,
    send_feature_status,
    send_admin_menu,
    send_admin_users_menu,
    send_logs_report,
)
from handlers.ai_search import AI_SEARCH_HELP_TEXT
from handlers.admin_publishing import (
    handle_manual_publish_callback,
    handle_manual_publish_message,
    manual_publish_preview_keyboard,
    parse_manual_button,
    publishing_methods_keyboard,
)


class AdminCartTests(unittest.TestCase):
    def test_formats_configured_limits_and_unknown_daily_tokens(self):
        self.assertEqual(
            _format_ai_usage_line(
                "rpd",
                {"used": 579, "limit": 150_000, "exact": True},
            ),
            "RPD: 579/150,000",
        )
        self.assertEqual(
            _format_ai_usage_line(
                "tpm",
                {"used": 0, "limit": 4_000_000, "exact": True},
            ),
            "TPM: 0/4,000K",
        )
        self.assertEqual(
            _format_ai_usage_line(
                "tpd",
                {"used": 533_600, "limit": None, "exact": False},
            ),
            "TPD: 533.6K/",
        )

    def test_formats_ai_provider_return_countdowns(self):
        self.assertEqual(_format_ai_duration(40), "40 ثا")
        self.assertEqual(_format_ai_duration(8 * 60 * 60), "8 ساعات")
        self.assertEqual(
            _format_ai_reset_line({
                "next_reset_seconds": 40,
                "quota_exhausted": True,
            }),
            "⏱️ العودة: بعد 40 ثا",
        )
        self.assertEqual(
            _format_ai_reset_line({
                "name": "Gemini",
                "available": True,
                "configured": True,
                "enabled": True,
            }, active_provider="Gemini"),
            "⏱️ الحالة: نشط",
        )
        self.assertEqual(
            _format_ai_reset_line({
                "name": "Groq",
                "available": True,
                "configured": True,
                "enabled": True,
                "next_reset_seconds": 8 * 60 * 60,
            }, active_provider="Gemini"),
            "⏱️ الحالة: جاهز",
        )
        self.assertEqual(
            _format_ai_reset_line({
                "name": "Cloudflare",
                "available": False,
                "configured": False,
                "enabled": True,
                "next_reset_seconds": None,
            }),
            "⏱️ الحالة: غير جاهز",
        )

    def test_admin_callback_pattern_accepts_ai_provider_actions(self):
        for provider in (
            "groq",
            "openrouter",
            "gemini",
            "deepseek",
            "cloudflare",
        ):
            for action in ("enable", "disable"):
                callback = f"admin_service_{provider}_{action}"
                with self.subTest(callback=callback):
                    self.assertIsNotNone(
                        re.fullmatch(ADMIN_CALLBACK_PATTERN, callback)
                    )
                for request_kind in ("channel", "user"):
                    scoped_callback = (
                        f"admin_service_{request_kind}_{provider}_{action}"
                    )
                    with self.subTest(callback=scoped_callback):
                        self.assertIsNotNone(
                            re.fullmatch(
                                ADMIN_CALLBACK_PATTERN,
                                scoped_callback,
                            )
                        )
        for callback in ("admin_service", "admin_service_details"):
            with self.subTest(callback=callback):
                self.assertIsNotNone(
                    re.fullmatch(ADMIN_CALLBACK_PATTERN, callback)
                )

    def test_admin_callback_pattern_rejects_unknown_provider_actions(self):
        for callback in (
            "admin_service_unknown_disable",
            "admin_service_gemini_delete",
            "admin_service_gemini_disable_extra",
            "admin_service_gemini_test",
            "admin_service_test",
        ):
            with self.subTest(callback=callback):
                self.assertIsNone(
                    re.fullmatch(ADMIN_CALLBACK_PATTERN, callback)
                )

    def test_admin_callback_pattern_keeps_existing_admin_actions(self):
        for callback in (
            "admin_menu",
            "admin_ai_disable",
            "admin_activity_month",
            "admin_logs_week",
            "admin_edit_12",
            "admin_add",
            "admin_exit",
        ):
            with self.subTest(callback=callback):
                self.assertIsNotNone(
                    re.fullmatch(ADMIN_CALLBACK_PATTERN, callback)
                )

    def test_admin_callback_pattern_accepts_grouped_menu_actions(self):
        for callback in (
            "admin_tools",
            "admin_users",
            "admin_queue",
            "admin_publish",
            "admin_publish_manual",
            "admin_publish_auto",
            "admin_publish_addbutton",
            "admin_publish_send",
            "admin_publish_back",
        ):
            with self.subTest(callback=callback):
                self.assertIsNotNone(
                    re.fullmatch(ADMIN_CALLBACK_PATTERN, callback)
                )

    def test_channel_queue_report_has_three_counts_and_stall_alert(self):
        report = _format_channel_queue_status({
            "pending": 7,
            "processing": 2,
            "processed": 40,
            "oldest_pending_seconds": 90,
            "oldest_processing_seconds": 601,
        })

        self.assertIn("pending: 7", report)
        self.assertIn("processing: 2", report)
        self.assertIn("processed: 40", report)
        self.assertIn("عمر أقدم pending: 2 د", report)
        self.assertIn("عمر أقدم processing: 11 د", report)
        self.assertIn("⚠️ تنبيه", report)

    def test_admin_callback_pattern_accepts_short_error_report_actions(self):
        for callback in (
            "admin_logs_hour",
            "admin_logs_quarter",
            "admin_logs_file_hour",
            "admin_logs_file_quarter",
        ):
            with self.subTest(callback=callback):
                self.assertIsNotNone(
                    re.fullmatch(ADMIN_CALLBACK_PATTERN, callback)
                )

    def test_logs_keyboard_has_direct_downloads_for_all_four_periods(self):
        buttons = [
            (button.text, button.callback_data)
            for row in logs_keyboard().inline_keyboard
            for button in row
        ]

        self.assertIn(("logs اليوم", "admin_logs_today"), buttons)
        self.assertIn(("logs هذا الأسبوع", "admin_logs_week"), buttons)
        self.assertIn(("logs آخر ساعة", "admin_logs_file_hour"), buttons)
        self.assertIn(("logs آخر ربع ساعة", "admin_logs_file_quarter"), buttons)

    def test_formats_an_error_period_report_with_a_clear_header(self):
        logs = [{
            "created_at": "2026-09-13 20:00:00",
            "level": "ERROR",
            "logger_name": "test",
            "message": "failed",
        }]

        report = _format_error_period_report(
            "quarter",
            logs,
            "الملخص:\nحدث خطأ واحد.",
        )

        self.assertIn("━━ تقرير أخطاء آخر ربع ساعة ━━", report)
        self.assertIn("الفترة: آخر 15 دقيقة", report)
        self.assertIn("عدد الأخطاء: 1", report)
        self.assertIn("الملخص:\nحدث خطأ واحد.", report)

    def test_formats_empty_error_log_file_without_changing_message(self):
        self.assertEqual(
            _format_error_log_file([]),
            "لا توجد أخطاء مسجلة في هذه الفترة.\n",
        )

    @patch("handlers.admin.send_logs_file", new_callable=AsyncMock)
    def test_short_error_report_buttons_download_only_requested_period(
        self,
        send_file,
    ):
        context = SimpleNamespace(user_data={"admin_mode": True})
        message = SimpleNamespace()
        query = SimpleNamespace(
            data="admin_logs_file_quarter",
            answer=AsyncMock(),
            message=message,
        )

        import asyncio
        asyncio.run(admin_callback(
            SimpleNamespace(callback_query=query),
            context,
        ))

        send_file.assert_awaited_once_with(
            message,
            "quarter",
        )

    @patch("handlers.admin.send_logs_file", new_callable=AsyncMock)
    def test_legacy_short_error_callback_still_downloads_directly(
        self,
        send_file,
    ):
        context = SimpleNamespace(user_data={"admin_mode": True})
        message = SimpleNamespace()
        query = SimpleNamespace(
            data="admin_logs_file_hour",
            answer=AsyncMock(),
            message=message,
        )

        import asyncio
        asyncio.run(admin_callback(
            SimpleNamespace(callback_query=query),
            context,
        ))

        send_file.assert_awaited_once_with(message, "hour")

    def test_ai_service_keyboard_has_independent_task_columns(self):
        keyboard = ai_service_keyboard([
            {
                "name": "Groq",
                "enabled": True,
                "enabled_by_request_kind": {
                    "channel": False,
                    "user": True,
                },
            },
            {"name": "OpenRouter", "enabled": True},
            {"name": "Gemini", "enabled": True},
            {"name": "DeepSeek", "enabled": True},
            {"name": "Cloudflare", "enabled": True},
        ])
        provider_rows = keyboard.inline_keyboard[:5]
        self.assertEqual(len(provider_rows), 5)
        self.assertEqual(
            provider_rows[0][0].callback_data,
            "admin_service_channel_groq_enable",
        )
        self.assertEqual(
            provider_rows[0][1].callback_data,
            "admin_service_user_deepseek_disable",
        )
        self.assertEqual(
            provider_rows[1][0].callback_data,
            "admin_service_channel_openrouter_disable",
        )
        self.assertEqual(
            provider_rows[4][0].text,
            "⛔ Cloudflare (غير متاح للمنشورات)",
        )
        self.assertEqual(
            provider_rows[4][0].callback_data,
            "admin_service_channel_cloudflare_disable",
        )
        self.assertEqual(
            provider_rows[4][1].callback_data,
            "admin_service_user_openrouter_disable",
        )
        self.assertEqual(
            keyboard.inline_keyboard[5][0].text,
            "عرض تفاصيل AI Service",
        )
        self.assertEqual(
            keyboard.inline_keyboard[5][0].callback_data,
            "admin_service_details",
        )
        self.assertEqual(
            len(provider_rows[0]),
            2,
        )
        self.assertEqual(
            len(provider_rows[4]),
            2,
        )

    def test_admin_menu_has_current_and_activity_actions_including_exit(self):
        keyboard = admin_menu_keyboard()

        labels = [button.text for row in keyboard.inline_keyboard for button in row]
        self.assertIn("المنتجات الإضافية", labels)
        self.assertIn("الأدوات", labels)
        self.assertIn("المستخدمين", labels)
        self.assertNotIn("أداة الذكاء الاصطناعي", labels)
        self.assertNotIn("AI Service", labels)
        self.assertNotIn("أداة السلة الذكية", labels)
        self.assertNotIn("نشاط المستخدمين", labels)
        self.assertNotIn("إشعار المستخدمين", labels)
        self.assertIn("تقارير الأخطاء", labels)
        self.assertEqual(labels[-1], "خروج")

    def test_admin_grouped_submenus_keep_existing_actions(self):
        tools = admin_tools_keyboard()
        users = admin_users_keyboard()

        self.assertEqual(
            [
                (button.text, button.callback_data)
                for row in tools.inline_keyboard
                for button in row
            ],
            [
                ("أداة الذكاء الاصطناعي", "admin_ai"),
                ("AI Service", "admin_service"),
                ("تراكم منشورات Telegram", "admin_queue"),
                ("أداة السلة الذكية", "admin_smart"),
                ("النشر في القناة", "admin_publish"),
                ("العودة للقائمة", "admin_menu"),
            ],
        )
        self.assertEqual(
            [
                (button.text, button.callback_data)
                for row in users.inline_keyboard
                for button in row
            ],
            [
                ("نشاط المستخدمين", "admin_activity"),
                ("إشعار المستخدمين", "admin_broadcast"),
                ("تنظيف المستخدمين", "admin_cleanup"),
                ("العودة للقائمة", "admin_menu"),
            ],
        )

    def test_tool_keyboards_return_to_tools_menu(self):
        with self.subTest(keyboard="ai"):
            keyboard = feature_toggle_keyboard("ai_search", True)
            self.assertEqual(
                (keyboard.inline_keyboard[-1][0].text,
                 keyboard.inline_keyboard[-1][0].callback_data),
                ("العودة إلى قائمة الأدوات", "admin_tools"),
            )

        with self.subTest(keyboard="smart"):
            keyboard = feature_toggle_keyboard("smart_cart", False)
            self.assertEqual(
                (keyboard.inline_keyboard[-1][0].text,
                 keyboard.inline_keyboard[-1][0].callback_data),
                ("العودة إلى قائمة الأدوات", "admin_tools"),
            )

        with self.subTest(keyboard="service"):
            keyboard = ai_service_keyboard([
                {"name": "Gemini", "enabled": True},
            ])
            self.assertEqual(
                (keyboard.inline_keyboard[-1][0].text,
                 keyboard.inline_keyboard[-1][0].callback_data),
                ("العودة إلى قائمة الأدوات", "admin_tools"),
            )

        with self.subTest(keyboard="service_details"):
            keyboard = ai_service_details_keyboard()
            self.assertEqual(
                (keyboard.inline_keyboard[-1][0].text,
                 keyboard.inline_keyboard[-1][0].callback_data),
                ("العودة إلى قائمة الأدوات", "admin_tools"),
            )

        with self.subTest(keyboard="queue"):
            keyboard = admin_tools_back_keyboard()
            self.assertEqual(
                (keyboard.inline_keyboard[-1][0].text,
                 keyboard.inline_keyboard[-1][0].callback_data),
                ("العودة إلى قائمة الأدوات", "admin_tools"),
            )

    def test_publish_methods_offer_manual_and_automatic_paths(self):
        buttons = [
            (button.text, button.callback_data)
            for row in publishing_methods_keyboard().inline_keyboard
            for button in row
        ]
        self.assertEqual(
            buttons[:2],
            [
                ("النشر اليدوي", "admin_publish_manual"),
                ("النشر التلقائي", "admin_publish_auto"),
            ],
        )

    def test_publish_callback_opens_methods_menu_and_manual_prompt(self):
        context = SimpleNamespace(user_data={"admin_mode": True})
        message = SimpleNamespace(
            chat=SimpleNamespace(id=123),
            message_id=10,
            photo=None,
            edit_text=AsyncMock(),
            edit_reply_markup=AsyncMock(),
            reply_text=AsyncMock(),
        )

        import asyncio

        async def exercise():
            menu_query = SimpleNamespace(
                data="admin_publish",
                answer=AsyncMock(),
                message=message,
            )
            await admin_callback(SimpleNamespace(callback_query=menu_query), context)
            manual_query = SimpleNamespace(
                data="admin_publish_manual",
                answer=AsyncMock(),
                message=message,
            )
            with patch("handlers.admin.manual_publish_channel_id", return_value="@deals"):
                await admin_callback(
                    SimpleNamespace(callback_query=manual_query),
                    context,
                )
            return menu_query, manual_query

        menu_query, manual_query = asyncio.run(exercise())

        self.assertEqual(message.edit_text.await_count, 2)
        self.assertEqual(
            message.edit_text.await_args_list[0].kwargs["reply_markup"]
            .inline_keyboard[0][0].callback_data,
            "admin_publish_manual",
        )
        self.assertEqual(
            message.edit_text.await_args_list[1].args[0],
            "قم بإرسال المنشور مع الصورة (اختياري):",
        )
        self.assertEqual(
            context.user_data["admin_state"]["action"],
            "manual_publish_waiting",
        )
        menu_query.answer.assert_awaited_once()
        manual_query.answer.assert_awaited_once()

    def test_manual_publish_back_clears_draft_and_returns_to_methods(self):
        context = SimpleNamespace(
            user_data={
                "admin_mode": True,
                "admin_state": {
                    "action": "manual_publish_waiting",
                    "prompt_chat_id": 123,
                    "prompt_message_id": 10,
                },
            },
        )
        message = SimpleNamespace(
            photo=None,
            edit_text=AsyncMock(),
            edit_reply_markup=AsyncMock(),
            reply_text=AsyncMock(),
        )
        query = SimpleNamespace(
            data="admin_publish_back",
            answer=AsyncMock(),
            message=message,
        )

        import asyncio
        asyncio.run(admin_callback(SimpleNamespace(callback_query=query), context))

        query.answer.assert_awaited_once()
        self.assertNotIn("admin_state", context.user_data)
        self.assertEqual(
            message.edit_text.await_args.kwargs["reply_markup"]
            .inline_keyboard[0][0].callback_data,
            "admin_publish_manual",
        )

    @patch("handlers.admin.get_auto_publish_report", return_value={
        "published_today": 1,
        "published_yesterday": 2,
        "published_this_month": 3,
        "published_last_month": 4,
        "published_total": 10,
        "failed_today": 1,
        "claimed": 2,
    })
    @patch("handlers.admin.is_feature_enabled", return_value=True)
    def test_auto_publish_view_shows_requested_periods_and_controls(
        self,
        _is_feature_enabled,
        _get_auto_publish_report,
    ):
        message = SimpleNamespace(
            edit_text=AsyncMock(),
            reply_text=AsyncMock(),
        )

        import asyncio
        asyncio.run(send_feature_status(message, "auto_publish", edit=True))

        text = message.edit_text.await_args.args[0]
        for line in (
            "اليوم: 1",
            "أمس: 2",
            "هذا الشهر: 3",
            "الشهر الماضي: 4",
            "الإجمالي: 10",
        ):
            self.assertIn(line, text)
        callbacks = [
            button.callback_data
            for row in message.edit_text.await_args.kwargs["reply_markup"]
            .inline_keyboard
            for button in row
        ]
        self.assertIn("admin_publish_disable", callbacks)
        self.assertIn("admin_publish_report", callbacks)
        self.assertIn("admin_publish", callbacks)

    def test_manual_button_parser_requires_safe_web_url(self):
        self.assertEqual(
            parse_manual_button("  العرض  | https://example.com/deal "),
            {"name": "العرض", "url": "https://example.com/deal"},
        )
        for invalid in ("زر فقط", "زر | javascript:alert(1)", " | https://example.com"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    parse_manual_button(invalid)

    def test_manual_preview_keyboard_separates_link_buttons_from_controls(self):
        keyboard = manual_publish_preview_keyboard([
            {"name": "المتجر", "url": "https://example.com"},
        ])
        self.assertEqual(keyboard.inline_keyboard[0][0].url, "https://example.com")
        self.assertEqual(
            [row[0].callback_data for row in keyboard.inline_keyboard[1:]],
            ["admin_publish_addbutton", "admin_publish_send"],
        )

    def test_manual_text_post_becomes_preview_without_channel_send(self):
        context = SimpleNamespace(
            user_data={"admin_mode": True, "admin_state": {
                "action": "manual_publish_waiting",
                "prompt_chat_id": 123,
                "prompt_message_id": 10,
            }},
            bot=SimpleNamespace(
                edit_message_text=AsyncMock(),
                send_message=AsyncMock(),
                send_photo=AsyncMock(),
                delete_message=AsyncMock(),
            ),
        )
        message = SimpleNamespace(
            text="منشور تجريبي",
            caption=None,
            photo=[],
            reply_text=AsyncMock(),
        )

        import asyncio
        handled = asyncio.run(handle_manual_publish_message(
            SimpleNamespace(message=message),
            context,
        ))

        self.assertTrue(handled)
        context.bot.edit_message_text.assert_awaited_once()
        self.assertEqual(
            context.bot.edit_message_text.await_args.kwargs["text"],
            "منشور تجريبي",
        )
        self.assertEqual(
            context.user_data["admin_state"]["action"],
            "manual_publish_ready",
        )
        context.bot.send_message.assert_not_awaited()
        context.bot.send_photo.assert_not_awaited()

    def test_manual_photo_post_previews_caption_and_removes_prompt(self):
        context = SimpleNamespace(
            user_data={"admin_mode": True, "admin_state": {
                "action": "manual_publish_waiting",
                "prompt_chat_id": 123,
                "prompt_message_id": 10,
            }},
            bot=SimpleNamespace(
                send_photo=AsyncMock(return_value=SimpleNamespace(
                    chat_id=123,
                    message_id=77,
                )),
                delete_message=AsyncMock(),
                edit_message_text=AsyncMock(),
                send_message=AsyncMock(),
            ),
        )
        message = SimpleNamespace(
            text=None,
            caption="نص الصورة",
            photo=[SimpleNamespace(file_id="photo-id")],
            reply_text=AsyncMock(),
        )

        import asyncio
        asyncio.run(handle_manual_publish_message(
            SimpleNamespace(message=message),
            context,
        ))

        context.bot.send_photo.assert_awaited_once()
        self.assertEqual(
            context.bot.send_photo.await_args.kwargs["caption"],
            "نص الصورة",
        )
        context.bot.delete_message.assert_awaited_once_with(
            chat_id=123,
            message_id=10,
        )
        state = context.user_data["admin_state"]
        self.assertEqual(state["preview_message_id"], 77)
        self.assertEqual(state["photo"], "photo-id")

    def test_manual_button_input_returns_to_the_updated_preview(self):
        context = SimpleNamespace(
            user_data={"admin_state": {
                "action": "manual_publish_ready",
                "preview_chat_id": 123,
                "preview_message_id": 77,
                "photo": None,
                "text": "منشور",
                "buttons": [],
            }},
            bot=SimpleNamespace(
                edit_message_text=AsyncMock(),
                edit_message_caption=AsyncMock(),
            ),
        )
        query = SimpleNamespace(
            data="admin_publish_addbutton",
            answer=AsyncMock(),
            message=SimpleNamespace(reply_text=AsyncMock()),
        )
        message = SimpleNamespace(
            text="المتجر | https://example.com",
            caption=None,
            photo=[],
            reply_text=AsyncMock(),
        )

        import asyncio

        async def exercise():
            await handle_manual_publish_callback(query, context)
            self.assertEqual(
                context.user_data["admin_state"]["action"],
                "manual_publish_button_input",
            )
            await handle_manual_publish_message(
                SimpleNamespace(message=message),
                context,
            )

        asyncio.run(exercise())

        state = context.user_data["admin_state"]
        self.assertEqual(state["action"], "manual_publish_ready")
        self.assertEqual(
            state["buttons"],
            [{"name": "المتجر", "url": "https://example.com"}],
        )
        self.assertEqual(
            context.bot.edit_message_text.await_args.kwargs["reply_markup"]
            .inline_keyboard[0][0].url,
            "https://example.com",
        )

    def test_manual_photo_send_preserves_caption_and_custom_button_only(self):
        context = SimpleNamespace(
            user_data={
                "admin_state": {
                    "action": "manual_publish_ready",
                    "preview_chat_id": 123,
                    "preview_message_id": 77,
                    "photo": "photo-id",
                    "text": "وصف الصورة",
                    "buttons": [{"name": "الرابط", "url": "https://example.com"}],
                },
            },
            bot=SimpleNamespace(
                send_photo=AsyncMock(),
                send_message=AsyncMock(),
            ),
        )
        message = SimpleNamespace(
            edit_reply_markup=AsyncMock(),
            reply_text=AsyncMock(),
        )
        query = SimpleNamespace(
            data="admin_publish_send",
            answer=AsyncMock(),
            message=message,
        )

        with patch(
            "handlers.admin_publishing.AUTO_PUBLISH_CHANNEL_LINK",
            "https://t.me/deals_channel",
        ):
            import asyncio
            asyncio.run(handle_manual_publish_callback(query, context))

        context.bot.send_photo.assert_awaited_once()
        sent = context.bot.send_photo.await_args.kwargs
        self.assertEqual(sent["photo"], "photo-id")
        self.assertEqual(sent["caption"], "وصف الصورة")
        self.assertEqual(
            sent["reply_markup"].inline_keyboard[0][0].url,
            "https://example.com",
        )
        self.assertIsNone(
            sent["reply_markup"].inline_keyboard[0][0].callback_data
        )
        context.bot.send_message.assert_not_awaited()

    def test_manual_publish_callback_sends_only_custom_markup_and_clears_state(self):
        context = SimpleNamespace(
            user_data={
                "admin_mode": True,
                "admin_state": {
                    "action": "manual_publish_ready",
                    "preview_chat_id": 123,
                    "preview_message_id": 77,
                    "photo": None,
                    "text": "نص المنشور",
                    "buttons": [{"name": "الرابط", "url": "https://example.com"}],
                },
            },
            bot=SimpleNamespace(
                send_message=AsyncMock(),
                send_photo=AsyncMock(),
            ),
        )
        message = SimpleNamespace(
            edit_reply_markup=AsyncMock(),
            reply_text=AsyncMock(),
        )
        query = SimpleNamespace(
            data="admin_publish_send",
            answer=AsyncMock(),
            message=message,
        )

        with patch(
            "handlers.admin_publishing.AUTO_PUBLISH_CHANNEL_LINK",
            "https://t.me/deals_channel",
        ):
            import asyncio
            asyncio.run(handle_manual_publish_callback(query, context))

        query.answer.assert_awaited_once()
        context.bot.send_message.assert_awaited_once()
        sent = context.bot.send_message.await_args.kwargs
        self.assertEqual(sent["chat_id"], "@deals_channel")
        self.assertEqual(sent["text"], "نص المنشور")
        channel_buttons = sent["reply_markup"].inline_keyboard
        self.assertEqual(
            [[button.text for button in row] for row in channel_buttons],
            [["الرابط"]],
        )
        self.assertTrue(all(
            button.callback_data is None
            for row in channel_buttons
            for button in row
        ))
        self.assertNotIn("admin_state", context.user_data)

    def test_manual_publish_send_failure_keeps_ready_draft(self):
        context = SimpleNamespace(
            user_data={
                "admin_state": {
                    "action": "manual_publish_ready",
                    "preview_chat_id": 123,
                    "preview_message_id": 77,
                    "photo": None,
                    "text": "نص المنشور",
                    "buttons": [],
                },
            },
            bot=SimpleNamespace(
                send_message=AsyncMock(side_effect=RuntimeError("failed")),
                send_photo=AsyncMock(),
            ),
        )
        message = SimpleNamespace(reply_text=AsyncMock())
        query = SimpleNamespace(
            data="admin_publish_send",
            answer=AsyncMock(),
            message=message,
        )

        with patch(
            "handlers.admin_publishing.AUTO_PUBLISH_CHANNEL_LINK",
            "https://t.me/deals_channel",
        ):
            import asyncio
            asyncio.run(handle_manual_publish_callback(query, context))

        self.assertEqual(
            context.user_data["admin_state"]["action"],
            "manual_publish_ready",
        )
        message.reply_text.assert_awaited_once()

    @patch("handlers.admin.get_total_user_count", return_value=42)
    def test_admin_grouped_menu_navigation_updates_existing_message(
        self,
        get_total_user_count,
    ):
        context = SimpleNamespace(user_data={"admin_mode": True})
        message = SimpleNamespace(
            edit_text=AsyncMock(),
            reply_text=AsyncMock(),
        )

        async def navigate():
            await admin_callback(
                SimpleNamespace(
                    callback_query=SimpleNamespace(
                        data="admin_tools",
                        answer=AsyncMock(),
                        message=message,
                    )
                ),
                context,
            )
            await admin_callback(
                SimpleNamespace(
                    callback_query=SimpleNamespace(
                        data="admin_users",
                        answer=AsyncMock(),
                        message=message,
                    )
                ),
                context,
            )
            await admin_callback(
                SimpleNamespace(
                    callback_query=SimpleNamespace(
                        data="admin_menu",
                        answer=AsyncMock(),
                        message=message,
                    )
                ),
                context,
            )

        import asyncio
        asyncio.run(navigate())

        self.assertEqual(message.edit_text.await_count, 3)
        message.reply_text.assert_not_awaited()
        self.assertIsInstance(
            message.edit_text.await_args_list[0].kwargs["reply_markup"],
            type(admin_tools_keyboard()),
        )
        self.assertIsInstance(
            message.edit_text.await_args_list[1].kwargs["reply_markup"],
            type(admin_users_keyboard()),
        )
        self.assertIn(
            "إجمالي مستخدمي البوت: 42",
            message.edit_text.await_args_list[1].args[0],
        )
        get_total_user_count.assert_called_once_with()
        self.assertIsInstance(
            message.edit_text.await_args_list[2].kwargs["reply_markup"],
            type(admin_menu_keyboard()),
        )

    @patch("handlers.admin.get_total_user_count", return_value=1234)
    def test_users_menu_shows_total_and_preserves_keyboard(
        self,
        get_total_user_count,
    ):
        message = SimpleNamespace(
            edit_text=AsyncMock(),
            reply_text=AsyncMock(),
        )

        import asyncio
        asyncio.run(send_admin_users_menu(message, edit=True))

        message.edit_text.assert_awaited_once()
        text = message.edit_text.await_args.args[0]
        self.assertTrue(text.startswith("اختر وظيفة المستخدمين التي تريد فتحها:"))
        self.assertIn("إجمالي مستخدمي البوت: 1,234", text)
        markup = message.edit_text.await_args.kwargs["reply_markup"]
        self.assertEqual(
            [
                button.callback_data
                for row in markup.inline_keyboard
                for button in row
            ],
            [
                "admin_activity",
                "admin_broadcast",
                "admin_cleanup",
                "admin_menu",
            ],
        )
        get_total_user_count.assert_called_once_with()
        message.reply_text.assert_not_awaited()

    @patch(
        "handlers.admin.get_total_user_count",
        side_effect=RuntimeError("database unavailable"),
    )
    def test_users_menu_still_renders_when_total_count_fails(
        self,
        get_total_user_count,
    ):
        message = SimpleNamespace(
            edit_text=AsyncMock(),
            reply_text=AsyncMock(),
        )

        import asyncio
        asyncio.run(send_admin_users_menu(message, edit=True))

        message.edit_text.assert_awaited_once()
        self.assertIn(
            "إجمالي مستخدمي البوت: غير متاح",
            message.edit_text.await_args.args[0],
        )
        self.assertIsInstance(
            message.edit_text.await_args.kwargs["reply_markup"],
            type(admin_users_keyboard()),
        )
        get_total_user_count.assert_called_once_with()

    @patch("handlers.admin.is_feature_enabled", return_value=True)
    def test_tool_navigation_returns_to_tools_menu(self, is_feature_enabled):
        context = SimpleNamespace(user_data={"admin_mode": True})
        message = SimpleNamespace(
            edit_text=AsyncMock(),
            reply_text=AsyncMock(),
        )

        async def navigate():
            await admin_callback(
                SimpleNamespace(
                    callback_query=SimpleNamespace(
                        data="admin_tools",
                        answer=AsyncMock(),
                        message=message,
                    )
                ),
                context,
            )
            await admin_callback(
                SimpleNamespace(
                    callback_query=SimpleNamespace(
                        data="admin_ai",
                        answer=AsyncMock(),
                        message=message,
                    )
                ),
                context,
            )
            await admin_callback(
                SimpleNamespace(
                    callback_query=SimpleNamespace(
                        data="admin_tools",
                        answer=AsyncMock(),
                        message=message,
                    )
                ),
                context,
            )

        import asyncio
        asyncio.run(navigate())

        self.assertEqual(message.edit_text.await_count, 3)
        self.assertIsInstance(
            message.edit_text.await_args_list[0].kwargs["reply_markup"],
            type(admin_tools_keyboard()),
        )
        self.assertEqual(
            message.edit_text.await_args_list[1].kwargs["reply_markup"]
            .inline_keyboard[-1][0].callback_data,
            "admin_tools",
        )
        self.assertIsInstance(
            message.edit_text.await_args_list[2].kwargs["reply_markup"],
            type(admin_tools_keyboard()),
        )
        is_feature_enabled.assert_called_once_with("ai_search")

    def test_each_tool_navigation_exposes_tools_return_path(self):
        context = SimpleNamespace(user_data={"admin_mode": True})
        message = SimpleNamespace(
            edit_text=AsyncMock(),
            reply_text=AsyncMock(),
        )

        async def click(data):
            await admin_callback(
                SimpleNamespace(
                    callback_query=SimpleNamespace(
                        data=data,
                        answer=AsyncMock(),
                        message=message,
                    )
                ),
                context,
            )

        async def navigate():
            await click("admin_tools")
            with patch("handlers.admin.is_feature_enabled", return_value=True):
                await click("admin_ai")
            await click("admin_tools")
            with patch(
                "handlers.admin.get_ai_service_status",
                return_value={
                    "mode": "local",
                    "active_provider": "local",
                    "providers": [],
                },
            ):
                await click("admin_service")
            await click("admin_tools")
            with patch(
                "handlers.admin.get_channel_queue_status",
                return_value={
                    "pending": 0,
                    "processing": 0,
                    "processed": 0,
                    "oldest_pending_seconds": None,
                    "oldest_processing_seconds": None,
                },
            ):
                await click("admin_queue")
            await click("admin_tools")
            with patch("handlers.admin.is_feature_enabled", return_value=True):
                await click("admin_smart")

        import asyncio
        asyncio.run(navigate())

        tool_messages = [
            message.edit_text.await_args_list[index]
            for index in (1, 3, 5, 7)
        ]
        self.assertEqual(
            [
                call.kwargs["reply_markup"].inline_keyboard[-1][0].callback_data
                for call in tool_messages
            ],
            ["admin_tools"] * 4,
        )

    @patch("handlers.admin.get_ai_service_status")
    @patch("handlers.admin.is_feature_enabled", side_effect=[True, False])
    def test_admin_full_menu_journey_reuses_one_message_without_extra_sends(
        self,
        is_feature_enabled,
        get_ai_service_status,
    ):
        get_ai_service_status.return_value = {
            "mode": "local",
            "active_provider": "local",
            "providers": [],
        }
        context = SimpleNamespace(user_data={"admin_mode": True})
        message = SimpleNamespace(
            edit_text=AsyncMock(),
            reply_text=AsyncMock(),
        )

        async def click(callback_data):
            query = SimpleNamespace(
                data=callback_data,
                answer=AsyncMock(),
                message=message,
            )
            await admin_callback(
                SimpleNamespace(callback_query=query),
                context,
            )
            query.answer.assert_awaited_once()

        async def journey():
            await send_admin_menu(message)

            await click("admin_tools")
            await click("admin_ai")
            await click("admin_menu")

            await click("admin_tools")
            await click("admin_smart")
            await click("admin_menu")

            await click("admin_tools")
            await click("admin_service")
            await click("admin_menu")

            await click("admin_users")
            await click("admin_activity")
            await click("admin_menu")

            await click("admin_users")
            await click("admin_broadcast")
            self.assertEqual(
                context.user_data["admin_state"],
                {"action": "broadcast"},
            )
            await click("admin_menu")

        import asyncio
        asyncio.run(journey())

        self.assertEqual(is_feature_enabled.call_count, 2)
        self.assertEqual(
            is_feature_enabled.call_args_list[0].args,
            ("ai_search",),
        )
        self.assertEqual(
            is_feature_enabled.call_args_list[1].args,
            ("smart_cart",),
        )
        get_ai_service_status.assert_called_once_with()
        message.reply_text.assert_awaited_once_with(
            "اضغط على الزر الموافق لطلبك:",
            reply_markup=admin_menu_keyboard(),
        )
        self.assertEqual(message.edit_text.await_count, 15)
        self.assertNotIn("admin_state", context.user_data)
        self.assertIsInstance(
            message.edit_text.await_args.kwargs["reply_markup"],
            type(admin_menu_keyboard()),
        )

    @patch(
        "handlers.admin.analyze_error_logs",
        side_effect=lambda logs, label: f"تحليل {label}: {len(logs)}",
    )
    @patch(
        "handlers.admin.get_error_logs",
        side_effect=lambda period: [{
            "created_at": f"2026-09-13 {period}",
            "level": "ERROR",
            "logger_name": f"isolated-{period}",
            "message": f"failure-{period}",
        }],
    )
    @patch("handlers.admin.get_admin_cart_rows", return_value=[])
    def test_cart_and_error_reports_return_to_menu_without_extra_messages(
        self,
        get_admin_cart_rows,
        get_error_logs,
        analyze_error_logs,
    ):
        context = SimpleNamespace(user_data={"admin_mode": True})
        message = SimpleNamespace(
            edit_text=AsyncMock(),
            reply_text=AsyncMock(),
        )

        async def click(callback_data):
            query = SimpleNamespace(
                data=callback_data,
                answer=AsyncMock(),
                message=message,
            )
            await admin_callback(
                SimpleNamespace(callback_query=query),
                context,
            )
            query.answer.assert_awaited_once()

        async def journey():
            await send_admin_menu(message)
            await click("admin_cart")
            await click("admin_menu")
            await click("admin_logs")
            await click("admin_menu")

        import asyncio
        asyncio.run(journey())

        get_admin_cart_rows.assert_called_once_with()
        self.assertCountEqual(
            get_error_logs.call_args_list,
            [
                unittest.mock.call("today"),
                unittest.mock.call("week"),
                unittest.mock.call("hour"),
                unittest.mock.call("quarter"),
            ],
        )
        self.assertCountEqual(
            analyze_error_logs.call_args_list,
            [
                unittest.mock.call([{
                    "created_at": "2026-09-13 today",
                    "level": "ERROR",
                    "logger_name": "isolated-today",
                    "message": "failure-today",
                }], "اليوم"),
                unittest.mock.call([{
                    "created_at": "2026-09-13 week",
                    "level": "ERROR",
                    "logger_name": "isolated-week",
                    "message": "failure-week",
                }], "هذا الأسبوع"),
                unittest.mock.call([{
                    "created_at": "2026-09-13 hour",
                    "level": "ERROR",
                    "logger_name": "isolated-hour",
                    "message": "failure-hour",
                }], "آخر ساعة"),
                unittest.mock.call([{
                    "created_at": "2026-09-13 quarter",
                    "level": "ERROR",
                    "logger_name": "isolated-quarter",
                    "message": "failure-quarter",
                }], "آخر ربع ساعة"),
            ],
        )
        message.reply_text.assert_awaited_once_with(
            "اضغط على الزر الموافق لطلبك:",
            reply_markup=admin_menu_keyboard(),
        )
        self.assertEqual(message.edit_text.await_count, 4)
        self.assertTrue(all(
            call.kwargs["reply_markup"] is not None
            for call in message.edit_text.await_args_list
        ))
        self.assertIsInstance(
            message.edit_text.await_args.kwargs["reply_markup"],
            type(admin_menu_keyboard()),
        )

    def test_broadcast_callback_pattern_accepts_confirmation_actions(self):
        for callback in (
            "admin_broadcast_confirm",
            "admin_broadcast_cancel",
        ):
            with self.subTest(callback=callback):
                self.assertIsNotNone(
                    re.fullmatch(ADMIN_CALLBACK_PATTERN, callback)
                )

    def test_cleanup_callback_pattern_accepts_actions(self):
        for callback in (
            "admin_cleanup",
            "admin_cleanup_confirm",
            "admin_cleanup_cancel",
        ):
            with self.subTest(callback=callback):
                self.assertIsNotNone(
                    re.fullmatch(ADMIN_CALLBACK_PATTERN, callback)
                )

    def test_cleanup_confirmation_keyboard_has_confirm_and_cancel(self):
        buttons = admin_cleanup_confirmation_keyboard().inline_keyboard[0]

        self.assertEqual(
            [(button.text, button.callback_data) for button in buttons],
            [
                ("تأكيد التنظيف", "admin_cleanup_confirm"),
                ("إلغاء", "admin_cleanup_cancel"),
            ],
        )

    @patch("handlers.admin.get_user_chat_ids", return_value=[101, 202, 303, 404])
    def test_cleanup_scan_uses_silent_chat_lookup_and_keeps_uncertain_errors(
        self,
        get_chat_ids,
    ):
        from telegram.error import BadRequest, Forbidden

        bot = SimpleNamespace(
            get_chat=AsyncMock(side_effect=[
                SimpleNamespace(id=101),
                Forbidden("bot was blocked by the user"),
                BadRequest("Bad Request: chat not found"),
                RuntimeError("temporary network failure"),
            ]),
            send_message=AsyncMock(),
            send_photo=AsyncMock(),
        )

        import asyncio
        result = asyncio.run(_scan_inaccessible_users(bot))

        get_chat_ids.assert_called_once_with()
        self.assertEqual(result["total_count"], 4)
        self.assertEqual(result["inaccessible_ids"], [202, 303])
        self.assertEqual(result["verification_failures"], 1)
        self.assertEqual(bot.get_chat.await_count, 4)
        bot.send_message.assert_not_awaited()
        bot.send_photo.assert_not_awaited()

    def test_cleanup_report_includes_counts_and_uncertain_verifications(self):
        report = _cleanup_report_text({
            "total_count": 8,
            "inaccessible_ids": [2, 4, 6],
            "verification_failures": 1,
        })

        self.assertIn("العدد الكلي: 8", report)
        self.assertIn("عدد المستخدمين غير القابلين للوصول: 3", report)
        self.assertIn("العدد بعد التنظيف: 5", report)
        self.assertIn("تعذر التحقق من: 1 مستخدم", report)

    @patch(
        "handlers.admin._scan_inaccessible_users",
        return_value={
            "total_count": 4,
            "inaccessible_ids": [202, 303],
            "verification_failures": 1,
        },
    )
    def test_cleanup_shows_progress_and_waits_for_confirmation(
        self,
        scan_inaccessible_users,
    ):
        context = SimpleNamespace(
            user_data={"admin_mode": True},
            bot=SimpleNamespace(
                send_message=AsyncMock(),
                send_photo=AsyncMock(),
            ),
        )
        message = SimpleNamespace(
            edit_text=AsyncMock(),
            reply_text=AsyncMock(),
        )
        query = SimpleNamespace(
            data="admin_cleanup",
            answer=AsyncMock(),
            message=message,
        )

        import asyncio
        asyncio.run(admin_callback(
            SimpleNamespace(callback_query=query),
            context,
        ))

        query.answer.assert_awaited_once()
        scan_inaccessible_users.assert_awaited_once_with(context.bot)
        self.assertEqual(
            context.user_data["admin_state"],
            {
                "action": "cleanup_pending",
                "total_count": 4,
                "inaccessible_ids": [202, 303],
                "verification_failures": 1,
            },
        )
        self.assertEqual(message.edit_text.await_count, 2)
        self.assertEqual(
            message.edit_text.await_args_list[0].args[0],
            "جاري استخراج معلومات المستخدمين⏳",
        )
        self.assertIn(
            "عدد المستخدمين غير القابلين للوصول: 2",
            message.edit_text.await_args_list[1].args[0],
        )
        self.assertEqual(
            [
                button.callback_data
                for row in message.edit_text.await_args_list[1]
                .kwargs["reply_markup"].inline_keyboard
                for button in row
            ],
            ["admin_cleanup_confirm", "admin_cleanup_cancel"],
        )
        context.bot.send_message.assert_not_awaited()
        context.bot.send_photo.assert_not_awaited()

    @patch("handlers.admin.get_user_chat_ids", return_value=[101, 404])
    @patch("handlers.admin.delete_user_chat_ids", return_value=2)
    def test_cleanup_confirmation_deletes_only_verified_ids_and_reports_result(
        self,
        delete_user_chat_ids,
        get_chat_ids,
    ):
        context = SimpleNamespace(
            user_data={
                "admin_mode": True,
                "admin_state": {
                    "action": "cleanup_pending",
                    "total_count": 4,
                    "inaccessible_ids": [202, 303],
                    "verification_failures": 1,
                },
            },
            bot=SimpleNamespace(
                send_message=AsyncMock(),
                send_photo=AsyncMock(),
            ),
        )
        message = SimpleNamespace(
            edit_reply_markup=AsyncMock(),
            reply_text=AsyncMock(),
        )
        query = SimpleNamespace(
            data="admin_cleanup_confirm",
            answer=AsyncMock(),
            message=message,
        )

        import asyncio
        asyncio.run(admin_callback(
            SimpleNamespace(callback_query=query),
            context,
        ))

        query.answer.assert_awaited_once()
        message.edit_reply_markup.assert_awaited_once_with(reply_markup=None)
        delete_user_chat_ids.assert_called_once_with([202, 303])
        get_chat_ids.assert_called_once_with()
        self.assertIn("عدد المستخدمين المحذوفين: 2",
                      message.reply_text.await_args.args[0])
        self.assertIn("عدد المستخدمين المتبقين: 2",
                      message.reply_text.await_args.args[0])
        self.assertNotIn("admin_state", context.user_data)
        context.bot.send_message.assert_not_awaited()
        context.bot.send_photo.assert_not_awaited()

    @patch("handlers.admin.delete_user_chat_ids")
    def test_cleanup_cancel_clears_pending_state_without_deleting(
        self,
        delete_user_chat_ids,
    ):
        context = SimpleNamespace(
            user_data={
                "admin_mode": True,
                "admin_state": {
                    "action": "cleanup_pending",
                    "total_count": 1,
                    "inaccessible_ids": [101],
                    "verification_failures": 0,
                },
            },
            bot=SimpleNamespace(
                send_message=AsyncMock(),
                send_photo=AsyncMock(),
            ),
        )
        message = SimpleNamespace(
            edit_reply_markup=AsyncMock(),
            edit_text=AsyncMock(),
            reply_text=AsyncMock(),
        )
        query = SimpleNamespace(
            data="admin_cleanup_cancel",
            answer=AsyncMock(),
            message=message,
        )

        import asyncio
        asyncio.run(admin_callback(
            SimpleNamespace(callback_query=query),
            context,
        ))

        query.answer.assert_awaited_once()
        message.edit_reply_markup.assert_awaited_once_with(reply_markup=None)
        delete_user_chat_ids.assert_not_called()
        self.assertNotIn("admin_state", context.user_data)
        self.assertIsInstance(
            message.edit_text.await_args.kwargs["reply_markup"],
            type(admin_users_keyboard()),
        )

    def test_broadcast_confirmation_keyboard_has_confirm_and_cancel(self):
        buttons = admin_broadcast_confirmation_keyboard().inline_keyboard[0]

        self.assertEqual(
            [(button.text, button.callback_data) for button in buttons],
            [
                ("تأكيد الإرسال", "admin_broadcast_confirm"),
                ("إلغاء", "admin_broadcast_cancel"),
            ],
        )

    def test_broadcast_submission_previews_without_sending(self):
        context = SimpleNamespace(
            user_data={
                "admin_mode": True,
                "admin_state": {"action": "broadcast"},
            },
            bot=SimpleNamespace(
                send_message=AsyncMock(),
                send_photo=AsyncMock(),
            ),
        )
        message = SimpleNamespace(
            text="إشعار تجريبي",
            caption=None,
            photo=[],
            chat=SimpleNamespace(id=123),
            copy=AsyncMock(),
            reply_text=AsyncMock(),
        )

        with patch("handlers.admin.get_user_chat_ids") as get_chat_ids:
            import asyncio
            handled = asyncio.run(handle_admin_message(
                SimpleNamespace(message=message),
                context,
            ))

        self.assertTrue(handled)
        get_chat_ids.assert_not_called()
        context.bot.send_message.assert_not_awaited()
        context.bot.send_photo.assert_not_awaited()
        message.copy.assert_awaited_once()
        preview_markup = message.copy.await_args.kwargs["reply_markup"]
        self.assertEqual(
            [
                button.callback_data
                for row in preview_markup.inline_keyboard
                for button in row
            ],
            ["admin_broadcast_confirm", "admin_broadcast_cancel"],
        )
        self.assertEqual(
            context.user_data["admin_state"],
            {
                "action": "broadcast_pending",
                "text": "إشعار تجريبي",
                "photo": None,
            },
        )

    def test_broadcast_cancel_returns_to_admin_menu_without_sending(self):
        context = SimpleNamespace(
            user_data={
                "admin_mode": True,
                "admin_state": {
                    "action": "broadcast_pending",
                    "text": "إشعار لن يرسل",
                    "photo": None,
                },
            },
            bot=SimpleNamespace(
                send_message=AsyncMock(),
                send_photo=AsyncMock(),
            ),
        )
        message = SimpleNamespace(
            edit_reply_markup=AsyncMock(),
            edit_text=AsyncMock(),
            reply_text=AsyncMock(),
        )
        query = SimpleNamespace(
            data="admin_broadcast_cancel",
            answer=AsyncMock(),
            message=message,
        )

        import asyncio
        asyncio.run(admin_callback(
            SimpleNamespace(callback_query=query),
            context,
        ))

        query.answer.assert_awaited_once()
        message.edit_reply_markup.assert_awaited_once_with(reply_markup=None)
        message.edit_text.assert_awaited_once()
        context.bot.send_message.assert_not_awaited()
        context.bot.send_photo.assert_not_awaited()
        self.assertNotIn("admin_state", context.user_data)

    @patch("handlers.admin.get_user_chat_ids", return_value=[101, 202])
    def test_broadcast_confirmation_preserves_long_text_and_counts(
        self,
        get_chat_ids,
    ):
        text = "x" * 4097
        context = SimpleNamespace(
            user_data={
                "admin_mode": True,
                "admin_state": {
                    "action": "broadcast_pending",
                    "text": text,
                    "photo": None,
                },
            },
            bot=SimpleNamespace(send_message=AsyncMock()),
        )
        message = SimpleNamespace(
            chat=SimpleNamespace(id=123),
            edit_reply_markup=AsyncMock(),
            reply_text=AsyncMock(),
        )
        query = SimpleNamespace(
            data="admin_broadcast_confirm",
            answer=AsyncMock(),
            message=message,
        )

        import asyncio
        asyncio.run(admin_callback(
            SimpleNamespace(callback_query=query),
            context,
        ))

        get_chat_ids.assert_called_once_with()
        self.assertEqual(context.bot.send_message.await_count, 4)
        self.assertEqual(
            [len(call.kwargs["text"]) for call in
             context.bot.send_message.await_args_list],
            [4096, 1, 4096, 1],
        )
        self.assertIn(
            "تم إرسال الإشعار إلى 2 مستخدم، وتعذر الإرسال إلى 0 مستخدم.",
            message.reply_text.await_args.args[0],
        )
        self.assertNotIn("admin_state", context.user_data)

    @patch("handlers.admin.get_user_chat_ids", return_value=[101, 202])
    def test_broadcast_confirmation_preserves_photo_caption_and_failure_count(
        self,
        get_chat_ids,
    ):
        text = "x" * 1025
        context = SimpleNamespace(
            user_data={
                "admin_mode": True,
                "admin_state": {
                    "action": "broadcast_pending",
                    "text": text,
                    "photo": "photo-file-id",
                },
            },
            bot=SimpleNamespace(
                send_photo=AsyncMock(side_effect=[
                    None,
                    RuntimeError("blocked user"),
                ]),
                send_message=AsyncMock(),
            ),
        )
        message = SimpleNamespace(
            chat=SimpleNamespace(id=123),
            edit_reply_markup=AsyncMock(),
            reply_text=AsyncMock(),
        )
        query = SimpleNamespace(
            data="admin_broadcast_confirm",
            answer=AsyncMock(),
            message=message,
        )

        import asyncio
        asyncio.run(admin_callback(
            SimpleNamespace(callback_query=query),
            context,
        ))

        get_chat_ids.assert_called_once_with()
        self.assertEqual(context.bot.send_photo.await_count, 2)
        self.assertEqual(
            context.bot.send_photo.await_args_list[0].kwargs["caption"],
            text[:1024],
        )
        context.bot.send_message.assert_awaited_once_with(
            chat_id=101,
            text=text[1024:],
        )
        self.assertIn(
            "تم إرسال الإشعار إلى 1 مستخدم، وتعذر الإرسال إلى 1 مستخدم.",
            message.reply_text.await_args.args[0],
        )
        self.assertNotIn("admin_state", context.user_data)

    def test_cart_actions_return_to_admin_menu_instead_of_exiting(self):
        keyboard = admin_actions_keyboard()

        self.assertEqual(
            keyboard.inline_keyboard[-1][0].text,
            "العودة إلى القائمة",
        )
        self.assertEqual(
            keyboard.inline_keyboard[-1][0].callback_data,
            "admin_menu",
        )

    def test_parses_the_admin_cart_template(self):
        row = _parse_cart_line(
            "1 | https://example.com/item?a=1&b=2 | 45.2 | 20 | 2 | 0"
        )

        self.assertEqual(row["id"], 1)
        self.assertEqual(row["link"], "https://example.com/item?a=1&b=2")
        self.assertEqual(row["price"], Decimal("45.2"))
        self.assertEqual(row["final_price"], Decimal("20"))
        self.assertEqual(row["stor"], Decimal("2"))
        self.assertEqual(row["shipping"], Decimal("0"))

    def test_formats_link_label_and_url_as_html(self):
        message = format_cart_row({
            "id": 1,
            "linkcart": "https://example.com/item?a=1&b=2",
            "pricecart": Decimal("45.20"),
            "pricefinalecart": Decimal("20.00"),
            "stor": Decimal("2"),
            "ship": Decimal("0"),
        })

        self.assertIn("1 | <a href=\"https://example.com/item?a=1&amp;b=2\">link</a>", message)
        self.assertIn(
            "<a href=\"https://example.com/item?a=1&amp;b=2\">"
            "https://example.com/item?a=1&amp;b=2</a>",
            message,
        )
        self.assertIn("| 45.2 | 20 | 2 | 0", message)

    def test_rejects_a_row_with_the_wrong_number_of_columns(self):
        with self.assertRaises(ValueError):
            _parse_cart_line("1 | https://example.com | 45.2 | 20 | 2")

    def test_admin_exit_returns_to_normal_mode_and_clears_ai_state(self):
        context = SimpleNamespace(
            user_data={
                "admin_mode": True,
                "admin_state": {"action": "broadcast"},
                "ai_search_active": True,
                "ai_search_results": [{"id": 1}],
                "ai_search_index": 0,
                "restore_ai_search_keyboard": True,
            },
        )
        message = SimpleNamespace(
            edit_reply_markup=AsyncMock(),
            reply_text=AsyncMock(),
        )
        query = SimpleNamespace(
            data="admin_exit",
            answer=AsyncMock(),
            message=message,
        )

        import asyncio

        asyncio.run(admin_callback(SimpleNamespace(callback_query=query), context))

        query.answer.assert_awaited_once()
        message.edit_reply_markup.assert_awaited_once_with(reply_markup=None)
        message.reply_text.assert_awaited_once()
        self.assertIsInstance(
            message.reply_text.await_args.kwargs["reply_markup"],
            ReplyKeyboardMarkup,
        )
        self.assertNotIn("admin_mode", context.user_data)
        self.assertNotIn("admin_state", context.user_data)
        self.assertNotIn("ai_search_active", context.user_data)
        self.assertNotIn("ai_search_results", context.user_data)
        self.assertNotIn("ai_search_index", context.user_data)
        self.assertNotIn("restore_ai_search_keyboard", context.user_data)

    def test_admin_menu_callback_updates_the_existing_message(self):
        context = SimpleNamespace(user_data={"admin_mode": True})
        message = SimpleNamespace(
            edit_text=AsyncMock(),
            reply_text=AsyncMock(),
        )
        query = SimpleNamespace(
            data="admin_menu",
            answer=AsyncMock(),
            message=message,
        )

        import asyncio
        asyncio.run(admin_callback(SimpleNamespace(callback_query=query), context))

        message.edit_text.assert_awaited_once()
        message.reply_text.assert_not_awaited()
        self.assertIsInstance(
            message.edit_text.await_args.kwargs["reply_markup"],
            type(admin_menu_keyboard()),
        )

    def test_admin_navigation_reuses_one_inline_message(self):
        context = SimpleNamespace(
            user_data={"admin_mode": True},
        )
        message = SimpleNamespace(
            chat=SimpleNamespace(id=123),
            message_id=456,
            edit_text=AsyncMock(),
            reply_text=AsyncMock(),
        )

        async def navigate():
            await admin_callback(
                SimpleNamespace(
                    callback_query=SimpleNamespace(
                        data="admin_menu",
                        answer=AsyncMock(),
                        message=message,
                    )
                ),
                context,
            )
            await admin_callback(
                SimpleNamespace(
                    callback_query=SimpleNamespace(
                        data="admin_activity",
                        answer=AsyncMock(),
                        message=message,
                    )
                ),
                context,
            )
            with patch(
                "handlers.admin.get_activity_stats",
                return_value={"requests": 2, "users": 1},
            ):
                await admin_callback(
                    SimpleNamespace(
                        callback_query=SimpleNamespace(
                            data="admin_activity_month",
                            answer=AsyncMock(),
                            message=message,
                        )
                    ),
                    context,
                )
            with patch("handlers.admin.get_admin_cart_rows", return_value=[]):
                await admin_callback(
                    SimpleNamespace(
                        callback_query=SimpleNamespace(
                            data="admin_cart",
                            answer=AsyncMock(),
                            message=message,
                        )
                    ),
                    context,
                )

        import asyncio
        asyncio.run(navigate())

        self.assertEqual(message.edit_text.await_count, 4)
        message.reply_text.assert_not_awaited()
        self.assertTrue(all(
            call.kwargs["reply_markup"] is not None
            for call in message.edit_text.await_args_list
        ))

    def test_admin_add_refreshes_the_existing_inline_message(self):
        context = SimpleNamespace(
            user_data={"admin_mode": True},
            bot=SimpleNamespace(edit_message_text=AsyncMock()),
        )
        message = SimpleNamespace(
            chat=SimpleNamespace(id=123),
            message_id=456,
            edit_text=AsyncMock(),
            reply_text=AsyncMock(),
        )
        add_query = SimpleNamespace(
            data="admin_add",
            answer=AsyncMock(),
            message=message,
        )

        import asyncio
        asyncio.run(admin_callback(
            SimpleNamespace(callback_query=add_query),
            context,
        ))

        self.assertEqual(
            context.user_data["admin_state"],
            {
                "action": "add",
                "chat_id": 123,
                "message_id": 456,
            },
        )

        input_message = SimpleNamespace(
            text="1 | https://example.com | 45.2 | 20 | 2 | 0",
            reply_text=AsyncMock(),
        )
        with patch("handlers.admin.insert_cart_row"), patch(
            "handlers.admin.get_admin_cart_rows",
            return_value=[{
                "id": 1,
                "linkcart": "https://example.com",
                "pricecart": Decimal("45.2"),
                "pricefinalecart": Decimal("20"),
                "stor": Decimal("2"),
                "ship": Decimal("0"),
            }],
        ):
            handled = asyncio.run(handle_admin_message(
                SimpleNamespace(message=input_message),
                context,
            ))

        self.assertTrue(handled)
        context.bot.edit_message_text.assert_awaited_once()
        input_message.reply_text.assert_not_awaited()
        self.assertNotIn("admin_state", context.user_data)

    def test_admin_command_removes_reply_keyboard_before_menu(self):
        events = []

        class SentMessage:
            message_id = 99

        async def reply_text(text, reply_markup=None):
            events.append(("reply", text, reply_markup))
            return SentMessage()

        async def delete_message(**kwargs):
            events.append(("delete", kwargs))

        context = SimpleNamespace(
            args=["Rabah06651024221997"],
            user_data={"ai_search_active": True},
            bot=SimpleNamespace(delete_message=delete_message),
        )
        message = SimpleNamespace(
            chat=SimpleNamespace(id=123),
            reply_text=reply_text,
        )

        import asyncio
        asyncio.run(admin_command(
            SimpleNamespace(message=message),
            context,
        ))

        self.assertEqual([event[0] for event in events], [
            "reply",
            "delete",
            "reply",
        ])
        self.assertEqual(events[0][1], "\u2063")
        self.assertEqual(events[2][1], "اضغط على الزر الموافق لطلبك:")
        self.assertNotIn("ai_search_active", context.user_data)
        self.assertTrue(context.user_data["admin_mode"])

    def test_admin_message_render_falls_back_when_editing_fails(self):
        message = SimpleNamespace(
            edit_text=AsyncMock(side_effect=RuntimeError("expired message")),
            reply_text=AsyncMock(),
        )

        import asyncio
        asyncio.run(_render_admin_message(
            message,
            "رسالة بديلة",
            edit=True,
        ))

        message.edit_text.assert_awaited_once()
        message.reply_text.assert_awaited_once_with(
            "رسالة بديلة",
            reply_markup=None,
        )

    def test_admin_message_render_splits_long_text_and_keeps_markup_first(self):
        keyboard = admin_menu_keyboard()
        message = SimpleNamespace(
            edit_text=AsyncMock(),
            reply_text=AsyncMock(),
        )

        import asyncio
        asyncio.run(_render_admin_message(
            message,
            "x" * (TELEGRAM_MESSAGE_LIMIT + 1),
            reply_markup=keyboard,
            edit=True,
        ))

        first = message.edit_text.await_args
        self.assertEqual(len(first.args[0]), TELEGRAM_MESSAGE_LIMIT)
        self.assertIs(first.kwargs["reply_markup"], keyboard)
        message.reply_text.assert_awaited_once_with(
            "x",
            reply_markup=None,
        )

    @patch("handlers.admin.get_error_logs", side_effect=lambda period: [])
    @patch(
        "handlers.admin.analyze_error_logs",
        side_effect=lambda logs, label: f"{label}-" + "x" * 5000,
    )
    def test_combined_logs_report_stays_in_one_message_with_four_periods_and_buttons(
        self,
        analyze_error_logs,
        get_error_logs,
    ):
        message = SimpleNamespace(
            edit_text=AsyncMock(),
            reply_text=AsyncMock(),
        )

        import asyncio
        asyncio.run(send_logs_report(message, edit=True))

        report = message.edit_text.await_args.args[0]
        self.assertEqual(message.edit_text.await_count, 1)
        self.assertEqual(message.reply_text.await_count, 0)
        self.assertLessEqual(len(report), TELEGRAM_MESSAGE_LIMIT)
        for label in ("اليوم", "هذا الأسبوع", "آخر ساعة", "آخر ربع ساعة"):
            self.assertIn(f"تقرير أخطاء {label}", report)
        callbacks = [
            button.callback_data
            for row in message.edit_text.await_args.kwargs["reply_markup"].inline_keyboard
            for button in row
        ]
        self.assertEqual(
            callbacks[:4],
            [
                "admin_logs_today",
                "admin_logs_week",
                "admin_logs_file_hour",
                "admin_logs_file_quarter",
            ],
        )
        self.assertEqual(get_error_logs.call_count, 4)
        self.assertEqual(analyze_error_logs.call_count, 4)
        self.assertIsNotNone(message.edit_text.await_args.kwargs["reply_markup"])

    def test_ask_ai_button_leaves_admin_mode_and_enters_ai_mode(self):
        context = SimpleNamespace(
            user_data={
                "admin_mode": True,
                "admin_state": {"action": "broadcast"},
            },
        )
        message = SimpleNamespace(
            text="🤖إسأل الذكاء الاصطناعي🤖",
            chat=SimpleNamespace(id=123),
            reply_text=AsyncMock(return_value=None),
        )
        update = SimpleNamespace(message=message)

        with patch(
            "handlers.admin.is_feature_enabled",
            return_value=True,
        ):
            import asyncio
            handled = asyncio.run(handle_admin_message(update, context))

        self.assertTrue(handled)
        self.assertNotIn("admin_mode", context.user_data)
        self.assertNotIn("admin_state", context.user_data)
        self.assertTrue(context.user_data["ai_search_active"])
        self.assertEqual(
            message.reply_text.await_args_list[-1].args[0],
            AI_SEARCH_HELP_TEXT,
        )
        self.assertEqual(
            message.reply_text.await_args_list[0].args[0],
            "\u2063",
        )

    @patch("handlers.admin.get_ai_service_status")
    def test_ai_service_button_shows_control_message(self, get_status):
        get_status.return_value = {
            "mode": "external",
            "active_provider": "Groq",
            "active_providers": {
                "channel": "OpenRouter",
                "user": "Groq",
            },
            "provider_order": [
                "Groq",
                "OpenRouter",
                "Gemini",
                "DeepSeek",
                "Cloudflare",
            ],
            "providers": [
                {
                    "name": "Gemini",
                    "model": "gemini-test",
                    "configured": True,
                    "enabled": True,
                    "available": False,
                    "reason": "quota exhausted",
                    "eligible_at": "2026-09-10T18:00:00+00:00",
                    "limits": [{
                        "metric": "rpm",
                        "limit": 10,
                        "used": 10,
                        "remaining": 0,
                        "reset_at": "2026-09-10T18:00:00+00:00",
                        "source": "provider",
                        "exact": True,
                    }],
            "next_reset_seconds": 40,
                    "capabilities": {
                        "context_window": 1_048_576,
                        "max_output_tokens": 65_536,
                    },
                    "usage": {
                        "prompt_tokens": 2,
                        "completion_tokens": 1,
                        "total_tokens": 3,
                        "neurons": 0,
                    },
                },
                {
                    "name": "Groq",
                    "model": "groq-test",
                    "configured": True,
                    "enabled": True,
                    "available": True,
                    "reason": None,
                    "eligible_at": None,
                    "limits": [{
                        "metric": "tpm",
                        "limit": 18000,
                        "used": 3,
                        "remaining": 17997,
                        "reset_at": None,
                        "source": "estimated",
                        "exact": False,
                    }],
            "next_reset_seconds": 8 * 60 * 60,
                    "usage": {
                        "prompt_tokens": 2,
                        "completion_tokens": 1,
                        "total_tokens": 3,
                        "neurons": 0,
                    },
                },
            ],
            "next_gemini_probe_at": None,
            "next_recovery_at": "2026-09-10T18:00:00+00:00",
        }
        context = SimpleNamespace(user_data={"admin_mode": True})
        message = SimpleNamespace(
            edit_text=AsyncMock(),
            reply_text=AsyncMock(),
        )
        query = SimpleNamespace(
            data="admin_service",
            answer=AsyncMock(),
            message=message,
        )

        import asyncio

        asyncio.run(admin_callback(SimpleNamespace(callback_query=query), context))

        sent_text = message.edit_text.await_args.args[0]
        self.assertIn("معالجة المنشورات: OpenRouter", sent_text)
        self.assertIn("طلبات المستخدمين: Groq", sent_text)
        self.assertNotIn("حدود الطلبات:", sent_text)
        self.assertNotIn("نافذة السياق:", sent_text)
        keyboard = message.edit_text.await_args.kwargs["reply_markup"]
        self.assertIn(
            "admin_service_details",
            [
                button.callback_data
                for row in keyboard.inline_keyboard
                for button in row
            ],
        )

    @patch("handlers.admin.get_ai_service_status")
    def test_ai_service_details_callback_shows_existing_report(self, get_status):
        get_status.return_value = {
            "mode": "external",
            "active_provider": "Groq",
            "providers": [{
                "name": "Gemini",
                "model": "gemini-test",
                "configured": True,
                "enabled": True,
                "available": False,
                "reason": "quota exhausted",
                "eligible_at": None,
                "limits": [
                    {
                        "metric": "rpm",
                        "used": 1,
                        "limit": 4_000,
                        "remaining": 3_999,
                        "exact": True,
                    },
                    {
                        "metric": "rpd",
                        "used": 2,
                        "limit": 150_000,
                        "remaining": 149_998,
                        "exact": True,
                    },
                    {
                        "metric": "tpm",
                        "used": 5,
                        "limit": 4_000_000,
                        "remaining": 3_999_995,
                        "exact": True,
                    },
                    {
                        "metric": "tpd",
                        "used": 5,
                        "limit": None,
                        "remaining": None,
                        "exact": False,
                    },
                ],
                "capabilities": {},
                "usage": {},
            }],
            "next_recovery_at": None,
        }
        context = SimpleNamespace(user_data={"admin_mode": True})
        message = SimpleNamespace(
            edit_text=AsyncMock(),
            reply_text=AsyncMock(),
        )
        query = SimpleNamespace(
            data="admin_service_details",
            answer=AsyncMock(),
            message=message,
        )

        import asyncio
        asyncio.run(admin_callback(SimpleNamespace(callback_query=query), context))

        get_status.assert_called_once_with(request_kind="channel")
        sent_text = message.edit_text.await_args.args[0]
        self.assertIn("🔴 Gemini Flash-Lite", sent_text)
        self.assertIn("• RPM: 1/4,000", sent_text)
        self.assertIn("• RPD: 2/150,000", sent_text)
        self.assertIn("• TPM: 5/4,000K", sent_text)
        self.assertIn("• TPD: 5/", sent_text)
        self.assertNotIn("quota exhausted", sent_text)
        self.assertNotIn("حدود الطلبات:", sent_text)
        keyboard = message.edit_text.await_args.kwargs["reply_markup"]
        self.assertEqual(
            keyboard.inline_keyboard[0][0].callback_data,
            "admin_service",
        )

    @patch("handlers.admin.get_ai_service_status")
    def test_ai_service_details_shows_deepseek_daily_counts_only(
        self,
        get_status,
    ):
        get_status.return_value = {
            "mode": "external",
            "active_provider": "DeepSeek",
            "providers": [{
                "name": "DeepSeek",
                "model": "deepseek-flash",
                "configured": True,
                "enabled": True,
                "available": True,
                "limits": [],
                "usage": {
                    "observed_requests_day": 1234,
                    "observed_tokens_day": 987654,
                },
            }],
        }
        context = SimpleNamespace(user_data={"admin_mode": True})
        message = SimpleNamespace(
            edit_text=AsyncMock(),
            reply_text=AsyncMock(),
        )
        query = SimpleNamespace(
            data="admin_service_details",
            answer=AsyncMock(),
            message=message,
        )

        import asyncio
        asyncio.run(admin_callback(SimpleNamespace(callback_query=query), context))

        get_status.assert_called_once_with(request_kind="channel")
        sent_text = message.edit_text.await_args.args[0]
        self.assertIn("• RPD: 1,234", sent_text)
        self.assertIn("• TPD: 987,654", sent_text)
        self.assertNotIn("• RPM:", sent_text)
        self.assertNotIn("• الطلبات اليوم:", sent_text)
        self.assertNotIn(
            "• التوكنز اليوم (الإدخال + الإخراج):",
            sent_text,
        )

    @patch("handlers.admin.get_ai_service_status")
    def test_ai_service_details_back_returns_to_control_in_same_message(
        self,
        get_status,
    ):
        status = {
            "mode": "external",
            "active_provider": "Groq",
            "providers": [{
                "name": "Gemini",
                "model": "gemini-test",
                "configured": True,
                "enabled": True,
                "available": True,
                "reason": None,
                "eligible_at": None,
                "limits": [],
                "capabilities": {},
                "usage": {},
            }],
            "next_recovery_at": None,
        }
        get_status.return_value = status
        context = SimpleNamespace(user_data={"admin_mode": True})
        message = SimpleNamespace(
            edit_text=AsyncMock(),
            reply_text=AsyncMock(),
        )

        async def click(callback_data):
            query = SimpleNamespace(
                data=callback_data,
                answer=AsyncMock(),
                message=message,
            )
            await admin_callback(
                SimpleNamespace(callback_query=query),
                context,
            )
            query.answer.assert_awaited_once()

        async def journey():
            await click("admin_tools")
            await click("admin_service")
            await click("admin_service_details")
            await click("admin_service")

        import asyncio
        asyncio.run(journey())

        self.assertEqual(get_status.call_count, 3)
        self.assertEqual(
            get_status.call_args_list[1].kwargs,
            {"request_kind": "channel"},
        )
        self.assertEqual(message.edit_text.await_count, 4)
        message.reply_text.assert_not_awaited()
        control_text = message.edit_text.await_args_list[-1].args[0]
        self.assertIn("حالة AI Service:", control_text)
        control_keyboard = message.edit_text.await_args_list[-1].kwargs[
            "reply_markup"
        ]
        self.assertIn(
            "admin_service_details",
            [
                button.callback_data
                for row in control_keyboard.inline_keyboard
                for button in row
            ],
        )

    @patch("handlers.admin.send_ai_service_control_status", new_callable=AsyncMock)
    @patch("handlers.admin.set_ai_provider_enabled")
    def test_ai_service_provider_callback_persists_and_refreshes(
        self,
        set_provider,
        send_control_status,
    ):
        context = SimpleNamespace(user_data={"admin_mode": True})
        message = SimpleNamespace(reply_text=AsyncMock())
        query = SimpleNamespace(
            data="admin_service_channel_gemini_disable",
            answer=AsyncMock(),
            message=message,
        )

        import asyncio
        asyncio.run(admin_callback(
            SimpleNamespace(callback_query=query),
            context,
        ))

        set_provider.assert_called_once_with("Gemini", False, "channel")
        send_control_status.assert_awaited_once_with(message, edit=True)

    @patch("handlers.admin.send_ai_service_control_status", new_callable=AsyncMock)
    @patch("handlers.admin.set_ai_provider_enabled")
    def test_legacy_provider_button_updates_both_task_settings(
        self,
        set_provider,
        send_control_status,
    ):
        context = SimpleNamespace(user_data={"admin_mode": True})
        message = SimpleNamespace(reply_text=AsyncMock())
        query = SimpleNamespace(
            data="admin_service_gemini_disable",
            answer=AsyncMock(),
            message=message,
        )

        import asyncio
        asyncio.run(admin_callback(
            SimpleNamespace(callback_query=query),
            context,
        ))

        self.assertEqual(set_provider.call_count, 2)
        self.assertEqual(
            set_provider.call_args_list[0].args,
            ("Gemini", False, "channel"),
        )
        self.assertEqual(
            set_provider.call_args_list[1].args,
            ("Gemini", False, "user"),
        )
        send_control_status.assert_awaited_once_with(message, edit=True)

    @patch("handlers.admin.send_ai_service_control_status", new_callable=AsyncMock)
    @patch("handlers.admin.set_ai_provider_enabled")
    def test_channel_cloudflare_button_cannot_change_provider_state(
        self,
        set_provider,
        send_control_status,
    ):
        import asyncio
        for requested_state in ("enable", "disable"):
            with self.subTest(requested_state=requested_state):
                context = SimpleNamespace(user_data={"admin_mode": True})
                message = SimpleNamespace(reply_text=AsyncMock())
                query = SimpleNamespace(
                    data=(
                        "admin_service_channel_cloudflare_"
                        f"{requested_state}"
                    ),
                    answer=AsyncMock(),
                    message=message,
                )

                asyncio.run(admin_callback(
                    SimpleNamespace(callback_query=query),
                    context,
                ))

                query.answer.assert_awaited_once_with(
                    "Cloudflare غير متاح لمعالجة المنشورات.",
                    show_alert=True,
                )
        set_provider.assert_not_called()
        send_control_status.assert_not_awaited()

    @patch("handlers.admin.send_ai_service_control_status", new_callable=AsyncMock)
    @patch("handlers.admin.set_ai_provider_enabled")
    def test_legacy_cloudflare_toggle_changes_user_path_only(
        self,
        set_provider,
        send_control_status,
    ):
        context = SimpleNamespace(
            user_data={"admin_mode": True},
            application=SimpleNamespace(bot_data={}),
        )
        message = SimpleNamespace(
            edit_text=AsyncMock(),
            reply_text=AsyncMock(),
        )
        query = SimpleNamespace(
            data="admin_service_cloudflare_enable",
            answer=AsyncMock(),
            message=message,
        )

        import asyncio
        asyncio.run(admin_callback(
            SimpleNamespace(callback_query=query),
            context,
        ))

        set_provider.assert_called_once_with("Cloudflare", True, "user")
        send_control_status.assert_awaited_once_with(message, edit=True)


if __name__ == "__main__":
    unittest.main()

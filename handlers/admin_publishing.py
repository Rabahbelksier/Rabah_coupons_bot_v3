"""Admin-only manual channel publishing flow."""

import logging
from urllib.parse import urlparse

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from config import AUTO_PUBLISH_CHANNEL_LINK
from utils.telegram import telegram_chat_id

logger = logging.getLogger(__name__)

MANUAL_PUBLISH_WAITING = "manual_publish_waiting"
MANUAL_PUBLISH_BUTTON_INPUT = "manual_publish_button_input"
MANUAL_PUBLISH_READY = "manual_publish_ready"
MANUAL_PUBLISH_SENDING = "manual_publish_sending"
MANUAL_PUBLISH_TEXT_LIMIT = 4096
MANUAL_PUBLISH_CAPTION_LIMIT = 1024
MANUAL_BUTTON_NAME_LIMIT = 64
MANUAL_BUTTON_URL_LIMIT = 2048
MANUAL_BUTTON_LIMIT = 98


def publishing_methods_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("النشر اليدوي", callback_data="admin_publish_manual")],
        [InlineKeyboardButton("النشر التلقائي", callback_data="admin_publish_auto")],
        [InlineKeyboardButton("العودة إلى قائمة الأدوات", callback_data="admin_tools")],
    ])


def manual_publish_back_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("العودة", callback_data="admin_publish_back"),
    ]])


def manual_publish_preview_keyboard(buttons):
    rows = [
        [InlineKeyboardButton(button["name"], url=button["url"])]
        for button in buttons
    ]
    rows.extend([
        [InlineKeyboardButton("إضافة زر", callback_data="admin_publish_addbutton")],
        [InlineKeyboardButton(
            "إرسال إلى القناة",
            callback_data="admin_publish_send",
        )],
    ])
    return InlineKeyboardMarkup(rows)


def manual_publish_channel_id():
    return telegram_chat_id(AUTO_PUBLISH_CHANNEL_LINK)


def parse_manual_button(value):
    parts = str(value or "").split("|", 1)
    if len(parts) != 2:
        raise ValueError("استخدم القالب: buton_name | link")

    name, url = (part.strip() for part in parts)
    if not name or len(name) > MANUAL_BUTTON_NAME_LIMIT:
        raise ValueError(
            f"اسم الزر مطلوب، ويجب ألا يتجاوز "
            f"{MANUAL_BUTTON_NAME_LIMIT} حرفًا."
        )
    if not url or len(url) > MANUAL_BUTTON_URL_LIMIT or any(
        character.isspace() for character in url
    ):
        raise ValueError("أرسل رابطًا صالحًا يبدأ بـ https:// أو http://.")

    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
    except ValueError:
        hostname = None
        parsed = None
    if (
        not parsed
        or parsed.scheme not in {"http", "https"}
        or not hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError("أرسل رابطًا صالحًا يبدأ بـ https:// أو http://.")
    return {"name": name, "url": url}


async def begin_manual_publish(message, context):
    """Open the draft prompt in the current admin menu message."""
    context.user_data["admin_state"] = {
        "action": MANUAL_PUBLISH_WAITING,
        "prompt_chat_id": message.chat.id,
        "prompt_message_id": message.message_id,
    }
    await message.edit_text(
        "قم بإرسال المنشور مع الصورة (اختياري):",
        reply_markup=manual_publish_back_keyboard(),
    )


async def _render_manual_preview(bot, state):
    reply_markup = manual_publish_preview_keyboard(state.get("buttons", []))
    chat_id = state["preview_chat_id"]
    message_id = state["preview_message_id"]
    if state.get("photo"):
        await bot.edit_message_caption(
            chat_id=chat_id,
            message_id=message_id,
            caption=state.get("text") or "",
            reply_markup=reply_markup,
        )
    else:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=state["text"],
            reply_markup=reply_markup,
        )


async def _render_button_input_prompt(bot, state):
    if state.get("photo"):
        await bot.edit_message_caption(
            chat_id=state["preview_chat_id"],
            message_id=state["preview_message_id"],
            caption="أرسل اسم الزر ورابطه بهذا القالب:\nbuton_name | link",
            reply_markup=manual_publish_back_keyboard(),
        )
    else:
        await bot.edit_message_text(
            chat_id=state["preview_chat_id"],
            message_id=state["preview_message_id"],
            text="أرسل اسم الزر ورابطه بهذا القالب:\nbuton_name | link",
            reply_markup=manual_publish_back_keyboard(),
        )


async def _handle_manual_post_message(message, context, state):
    text = (message.text or message.caption or "").strip()
    photo = message.photo[-1].file_id if message.photo else None
    if not text and not photo:
        await message.reply_text("أرسل نص المنشور أو صورة، ويمكن إضافة نص للصورة.")
        return

    if photo and len(text) > MANUAL_PUBLISH_CAPTION_LIMIT:
        await message.reply_text(
            f"نص الصورة يتجاوز حد Telegram البالغ "
            f"{MANUAL_PUBLISH_CAPTION_LIMIT} حرفًا. أعد إرسال المنشور بنص أقصر."
        )
        return
    if not photo and len(text) > MANUAL_PUBLISH_TEXT_LIMIT:
        await message.reply_text(
            f"المنشور النصي يتجاوز حد Telegram البالغ "
            f"{MANUAL_PUBLISH_TEXT_LIMIT} حرفًا. أعد إرساله بنص أقصر."
        )
        return

    preview_state = {
        **state,
        "action": MANUAL_PUBLISH_READY,
        "text": text,
        "photo": photo,
        "buttons": [],
    }
    reply_markup = manual_publish_preview_keyboard([])

    try:
        if photo:
            preview = await context.bot.send_photo(
                chat_id=state["prompt_chat_id"],
                photo=photo,
                caption=text or None,
                reply_markup=reply_markup,
            )
            preview_state.update({
                "preview_chat_id": state["prompt_chat_id"],
                "preview_message_id": preview.message_id,
            })
            try:
                await context.bot.delete_message(
                    chat_id=state["prompt_chat_id"],
                    message_id=state["prompt_message_id"],
                )
            except Exception:
                logger.debug(
                    "Could not remove the manual publish prompt",
                    exc_info=True,
                )
        else:
            try:
                await context.bot.edit_message_text(
                    chat_id=state["prompt_chat_id"],
                    message_id=state["prompt_message_id"],
                    text=text,
                    reply_markup=reply_markup,
                )
                preview_chat_id = state["prompt_chat_id"]
                preview_message_id = state["prompt_message_id"]
            except Exception:
                logger.warning(
                    "Could not edit the manual publish prompt; sending a replacement",
                    exc_info=True,
                )
                preview = await context.bot.send_message(
                    chat_id=state["prompt_chat_id"],
                    text=text,
                    reply_markup=reply_markup,
                )
                preview_chat_id = state["prompt_chat_id"]
                preview_message_id = preview.message_id
                try:
                    await context.bot.delete_message(
                        chat_id=state["prompt_chat_id"],
                        message_id=state["prompt_message_id"],
                    )
                except Exception:
                    logger.debug(
                        "Could not remove the replaced manual publish prompt",
                        exc_info=True,
                    )
            preview_state.update({
                "preview_chat_id": preview_chat_id,
                "preview_message_id": preview_message_id,
            })
    except Exception:
        logger.exception("Could not create the manual publishing preview")
        await message.reply_text(
            "تعذر تجهيز معاينة المنشور. لم يتم إرساله إلى القناة؛ حاول مجددًا."
        )
        return

    context.user_data["admin_state"] = preview_state


async def handle_manual_publish_message(update, context):
    """Process content and button-input messages for a manual draft."""
    state = context.user_data.get("admin_state") or {}
    action = state.get("action")
    if action == MANUAL_PUBLISH_WAITING:
        await _handle_manual_post_message(update.message, context, state)
        return True

    if action == MANUAL_PUBLISH_BUTTON_INPUT:
        try:
            button = parse_manual_button(update.message.text)
        except ValueError as exc:
            await update.message.reply_text(f"⚠️ {exc}")
            return True

        buttons = state.get("buttons", [])
        if len(buttons) >= MANUAL_BUTTON_LIMIT:
            await update.message.reply_text(
                f"يمكن إضافة {MANUAL_BUTTON_LIMIT} زرًا كحد أقصى."
            )
            return True
        preview_state = {
            **state,
            "buttons": [*buttons, button],
            "action": MANUAL_PUBLISH_READY,
        }
        try:
            await _render_manual_preview(context.bot, preview_state)
        except Exception:
            logger.exception("Could not update the manual publish preview")
            await update.message.reply_text(
                "تعذر تحديث المعاينة. بقيت المسودة محفوظة؛ أعد إرسال بيانات الزر."
            )
        else:
            context.user_data["admin_state"] = preview_state
        return True

    if action in {MANUAL_PUBLISH_READY, MANUAL_PUBLISH_SENDING}:
        await update.message.reply_text(
            "استخدم أزرار المعاينة لإضافة زر أو إرسال المنشور."
        )
        return True
    return False


async def _start_button_input(query, context, state):
    state["action"] = MANUAL_PUBLISH_BUTTON_INPUT
    context.user_data["admin_state"] = state
    await _render_button_input_prompt(context.bot, state)


async def _send_manual_post(query, context, state):
    target_chat_id = manual_publish_channel_id()
    if not target_chat_id:
        await query.answer(
            "رابط قناة النشر غير مضبوط أو غير صالح.",
            show_alert=True,
        )
        return

    state["action"] = MANUAL_PUBLISH_SENDING
    context.user_data["admin_state"] = state
    await query.answer()

    custom_buttons = [
        [InlineKeyboardButton(button["name"], url=button["url"])]
        for button in state.get("buttons", [])
    ]
    reply_markup = (
        InlineKeyboardMarkup(custom_buttons)
        if custom_buttons
        else None
    )
    try:
        if state.get("photo"):
            await context.bot.send_photo(
                chat_id=target_chat_id,
                photo=state["photo"],
                caption=state.get("text") or None,
                reply_markup=reply_markup,
            )
        else:
            await context.bot.send_message(
                chat_id=target_chat_id,
                text=state["text"],
                reply_markup=reply_markup,
            )
    except Exception:
        logger.exception("Could not send the manual post to its channel")
        state["action"] = MANUAL_PUBLISH_READY
        context.user_data["admin_state"] = state
        await query.message.reply_text(
            "تعذر إرسال المنشور إلى القناة. بقيت المعاينة محفوظة؛ حاول مجددًا."
        )
        return

    context.user_data.pop("admin_state", None)
    try:
        await query.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.debug("Could not remove manual preview buttons", exc_info=True)
    await query.message.reply_text(
        "✅ تم إرسال المنشور إلى القناة.",
        reply_markup=publishing_methods_keyboard(),
    )


async def handle_manual_publish_callback(query, context):
    """Handle the two actions attached to a manual post preview."""
    action = query.data
    state = context.user_data.get("admin_state") or {}
    if action == "admin_publish_addbutton":
        if state.get("action") != MANUAL_PUBLISH_READY:
            await query.answer("لا توجد معاينة جاهزة لإضافة زر.", show_alert=True)
            return
        await query.answer()
        try:
            await _start_button_input(query, context, state)
        except Exception:
            logger.exception("Could not open manual button input")
            state["action"] = MANUAL_PUBLISH_READY
            context.user_data["admin_state"] = state
            await query.message.reply_text("تعذر فتح إدخال الزر، حاول مرة أخرى.")
        return

    if action == "admin_publish_send":
        if state.get("action") != MANUAL_PUBLISH_READY:
            await query.answer("لا توجد معاينة جاهزة للإرسال.", show_alert=True)
            return
        await _send_manual_post(query, context, state)
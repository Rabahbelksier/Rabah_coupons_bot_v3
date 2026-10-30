import asyncio
import logging
from telegram import Update
from telegram.ext import ContextTypes

from services.queue_manager import enqueue_url
from core.smart_cart import handle_smart_cart_message
from core.db import record_user_activity
from handlers.admin import handle_admin_message
from handlers.ai_search import (
    ai_search_reply_keyboard,
    handle_ai_search_button_message,
    handle_ai_search_message,
    handle_saved_offers_reply_message,
    handle_stale_saved_offers_message,
)
from handlers.help import MORE_DETAILS_BUTTON_TEXT, send_help_menu
from utils.parser import extract_aliexpress_url

logger = logging.getLogger(__name__)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await handle_admin_message(update, context):
        return
    user = update.effective_user
    await asyncio.to_thread(
        record_user_activity,
        update.effective_chat.id,
        getattr(user, "first_name", "") if user else "",
        getattr(user, "last_name", "") if user else "",
    )
    if await handle_smart_cart_message(update, context):
        return
    if update.message.text == MORE_DETAILS_BUTTON_TEXT:
        await send_help_menu(update.message)
        return
    if await handle_ai_search_button_message(update, context):
        return
    if await handle_saved_offers_reply_message(update, context):
        return
    if await handle_stale_saved_offers_message(update, context):
        return
    if await handle_ai_search_message(update, context):
        return
    user_input = update.message.text or ""
    chat_id = update.effective_chat.id
    target_url = extract_aliexpress_url(user_input)
    if not target_url:
        await update.message.reply_text(
            "⚠️ من فضلك قم بإرسال روابط منتجات Aliexpress فقط ",
            reply_markup=(
                None
                if context.user_data.get("ai_search_active")
                else ai_search_reply_keyboard()
            ),
        )
        return

    await enqueue_url(chat_id, target_url, context)

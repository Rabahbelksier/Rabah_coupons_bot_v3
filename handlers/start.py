import asyncio
import logging
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from config import META_ATTRIBUTION_ID
from core.db import save_user
from handlers.ai_search import AI_SEARCH_BUTTON_TEXT, _clear_ai_search_state
from handlers.help import (
    MORE_DETAILS_BUTTON_TEXT,
    MORE_DETAILS_CALLBACK,
    START_TEXT,
)
from services.meta_pixel import send_pending_telegram_start_events

logger = logging.getLogger(__name__)
START_AI_CALLBACK = "start_ai"


def start_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                AI_SEARCH_BUTTON_TEXT,
                callback_data=START_AI_CALLBACK,
            ),
        ],
        [
            InlineKeyboardButton(
                MORE_DETAILS_BUTTON_TEXT,
                callback_data=MORE_DETAILS_CALLBACK,
            ),
        ],
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _clear_ai_search_state(context)
    user = update.effective_user
    attribution_id = context.args[0] if len(context.args) == 1 else None
    pending_events = save_user(
        user.id,
        user.first_name,
        user.last_name or '',
        attribution_id=attribution_id,
        expected_attribution_id=META_ATTRIBUTION_ID,
    )
    event_delivery = None
    if pending_events:
        event_delivery = asyncio.create_task(
            asyncio.to_thread(
                send_pending_telegram_start_events,
                pending_events,
            )
        )

    try:
        await update.message.reply_text(
            START_TEXT,
            reply_markup=start_keyboard(),
        )
    finally:
        if event_delivery:
            await event_delivery


async def start_ai_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from handlers.ai_search import _activate_ai_search

    query = update.callback_query
    await query.answer()
    await _activate_ai_search(query.message, context)

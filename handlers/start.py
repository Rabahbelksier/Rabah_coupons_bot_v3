import logging
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from core.db import save_user
from handlers.ai_search import AI_SEARCH_BUTTON_TEXT, _clear_ai_search_state
from handlers.help import (
    MORE_DETAILS_BUTTON_TEXT,
    MORE_DETAILS_CALLBACK,
    START_TEXT,
)

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
    save_user(user.id, user.first_name, user.last_name or '')
    await update.message.reply_text(
        START_TEXT,
        reply_markup=start_keyboard(),
    )


async def start_ai_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from handlers.ai_search import _activate_ai_search

    query = update.callback_query
    await query.answer()
    await _activate_ai_search(query.message, context)

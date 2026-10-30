from telegram import InlineKeyboardMarkup
from urllib.parse import urlparse


def telegram_chat_id(channel_link):
    """Convert a Telegram channel URL or handle to a Bot API chat ID."""
    value = str(channel_link or "").strip()
    if not value:
        return None
    if value.startswith(("https://", "http://")):
        parsed = urlparse(value)
        value = parsed.path.strip("/").split("/", 1)[0]
    if value.startswith("t.me/"):
        value = value[5:].split("/", 1)[0]
    if value.startswith("@") or value.startswith("-100"):
        return value
    return f"@{value}" if value else None


def markup_without_callback(markup, callback_data):
    """Return a keyboard without the pressed callback button."""
    if not markup:
        return None

    remaining_rows = [
        [
            button
            for button in row
            if getattr(button, "callback_data", None) != callback_data
        ]
        for row in markup.inline_keyboard
    ]
    remaining_rows = [row for row in remaining_rows if row]
    return InlineKeyboardMarkup(remaining_rows) if remaining_rows else None


async def remove_pressed_button(query):
    """Remove only the callback button used by the current query."""
    markup = markup_without_callback(
        getattr(query.message, "reply_markup", None),
        query.data,
    )
    if markup is not None or getattr(query.message, "reply_markup", None):
        await query.edit_message_reply_markup(reply_markup=markup)
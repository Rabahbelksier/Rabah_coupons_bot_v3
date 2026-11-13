import logging


logger = logging.getLogger(__name__)

AI_SEARCH_REPLY_KEYBOARD_ANCHOR_KEY = "ai_search_reply_keyboard_anchor_id"


def remember_ai_search_reply_keyboard(context, message):
    """Remember a message carrying the normal-mode AI reply keyboard."""
    user_data = getattr(context, "user_data", None)
    message_id = getattr(message, "message_id", None)
    if user_data is not None and isinstance(message_id, int) and message_id > 0:
        user_data[AI_SEARCH_REPLY_KEYBOARD_ANCHOR_KEY] = message_id


def clear_ai_search_reply_keyboard_anchor(context):
    user_data = getattr(context, "user_data", None)
    if user_data is not None:
        user_data.pop(AI_SEARCH_REPLY_KEYBOARD_ANCHOR_KEY, None)


async def delete_smart_cart_prompts(context, state):
    """Delete messages that requested input for an unfinished smart cart."""
    chat_id = state.get("chat_id")
    message_ids = state.pop("input_prompt_message_ids", [])
    bot = getattr(context, "bot", None)
    if chat_id is None or bot is None:
        return

    for message_id in message_ids:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception:
            # Missing or expired prompts should not block a mode switch.
            logger.debug("Unable to delete smart cart prompt %s", message_id)


async def cancel_smart_cart(context):
    """Clear an unfinished smart cart when the user switches modes."""
    user_data = getattr(context, "user_data", None)
    if user_data is None:
        return
    state = user_data.get("smart_cart")
    if state:
        await delete_smart_cart_prompts(context, state)
    user_data.pop("smart_cart", None)
    user_data.pop("smart_cart_offers", None)

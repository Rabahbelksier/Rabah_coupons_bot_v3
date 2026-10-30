import asyncio
import logging
import re
import time

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    InputMediaPhoto,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.ext import ContextTypes

from config import AI_USER_REQUEST_TIMEOUT_SECONDS
from core.ai import (
    GeminiError,
    _local_parse_user_request,
    _safe_local_user_request_with_cache,
    parse_user_request,
)
from core.db import (
    count_processed_channel_posts,
    get_channel_post,
    is_feature_enabled,
    save_channel_photo_file_id,
    save_channel_photo_link,
    search_channel_posts,
)
from core.categories import (
    SAVED_OFFER_CATEGORY_COLUMN_FILTERS,
    SAVED_OFFER_CATEGORY_GROUPS_BY_KEY,
    SAVED_OFFER_CATEGORY_FILTERS,
)
from core.product import get_product_info_from_api
from core.scraper import get_product_details_scraping
from utils.parser import extract_aliexpress_url, extract_product_id
from services.ai_priority import ai_request_priority

logger = logging.getLogger(__name__)

AI_SEARCH_BUTTON_TEXT = "🤖إسأل الذكاء الاصطناعي🤖"
AI_SEARCH_DISABLED_TEXT = "🤖 أداة الذكاء الاصطناعي متوقفة مؤقتًا 🔄 يرجى المحاولة لاحقًا"
AI_SEARCH_HELP_TEXT = """🟢 يمكنك أن تطلب مني تجهيز عروض حسب طلبك، فأنا أداة ذكاء اصطناعي وأدعم مثل الرسائل او الطلبات التالية:

• عروض على منتج معين 
• البحث عن عرض من خلال كتابة اسم المنتج
• عروض هواتف علامة معينة بأرخص سعر ممكن
• عروض هواتف بقيمة معينة مثلا بين 50 و100 دولار
• عروض هواتف في حدود مثلا 200$
• عروض منتج معين في حدود مثلا 30$
• عروض منتج معين او فئة منتجات معينة اقل من مثلا 100$
• عروض منتج معين سعره يزيد عن مثلا 100$
• العروض الرائجة اليوم
• عروض الشحن نحو فرنسا (عروض عامة)
• عروض هواتف (او اي منتج معين) تشحن نحو فرنسا

🟢 ليكن في علمك ما دمت أداة ذكاء اصطناعي يمكنك كتابة رسالتك حسب مرادك وأنا سأقوم بتحليلها وفهم ما تريده بالضبط وسأجهز لك عروضا حصرية ليست بقديمة وحسب طلبك

اكتب طلبك الآن. أو اضغط على زر (عروض AI المحفوظة) """

AI_SEARCH_SUPPORTED_TEXT = """هذه الأداة تدعم البحث عن:
• أفضل عرض على منتج معين
• أرخص عروض فئة أو نوع معين
• فئة معينة بازيد/بأقل من سعر محدد
• فئة معينة ضمن نطاق سعري
• فئة معينة في حدود سعر معين
• العروض الرائجة اليوم
• منتج معين يشحن نحو فرنسا
• عروض تشحن نحو فرنسا
• عروض الباندل"""
AI_SEARCH_LOADING_TEXT = "⏳ جاري تجهيز طلبك..."
SAVED_OFFERS_BUTTON_TEXT = "عروض AI المحفوظة"
SAVED_OFFERS_MENU_TEXT = (
    "لقد قام البوت بإعداد وحفظ {count} عرض على مختلف المنتجات باستخدام الذكاء "
    "الاصطناعي، يمكنك تصفحها يدويا عبر هذه الأزرار:"
)
SAVED_OFFERS_PHONE_TEXT = "اختر علامة الهاتف التي تريدها:"
SAVED_OFFERS_CATEGORY_TEXT = "قم باختيار فئة المنتج الذي تريده"
AI_SEARCH_DATABASE_ERROR_TEXT = (
    "⚠️تعذر الوصول إلى العروض حاليًا، حاول مرة أخرى لاحقًا، أو قم باستخدام البحث في عروض Ai المحفوظة"
)
AI_SEARCH_SESSION_EXPIRED_TEXT = (
    "⚠️ انتهت جلسة لوحة العروض بعد إعادة تشغيل البوت. "
    "اضغط على زر الذكاء الاصطناعي لفتح لوحة جديدة."
)
AI_SEARCH_NO_RESULTS_TEXT = (
    "🔍 لم نجد عروضًا متوفرة تطابق طلبك حاليًا ✨ يمكننا تجهيز عروض أخرى وفقًا لطلبك 🛍️"
)
SAVED_OFFERS_PHONE_BRANDS = (
    "Realme",
    "Redmi",
    "Honor",
    "Poco",
    "Xiaomi",
    "Oppo",
    "Vivo",
    "Blackview",
)
SAVED_OFFERS_MAIN_BUTTONS = (
    "عروض هواتف",
    "تشحن نحو فرنسا",
    "عروض الباندل",
    "عروض اخرى",
)
SAVED_OFFERS_BACK_BUTTON_TEXT = "رجوع"
_SAVED_OFFERS_BUTTON_MARKER = "\u2063"
_source_photo_tasks = {}


def _clear_ai_search_state(context):
    context.user_data.pop("ai_search_active", None)
    context.user_data.pop("ai_search_results", None)
    context.user_data.pop("ai_search_index", None)
    context.user_data.pop("restore_ai_search_keyboard", None)
    context.user_data.pop("ai_search_auxiliary_message_ids", None)
    context.user_data.pop("saved_offers_menu", None)


def _store_ai_search_results(context, posts):
    """Keep the complete local result set used by navigation."""
    context.user_data["ai_search_results"] = list(posts)
    context.user_data["ai_search_index"] = 0


def _navigation_lock(context):
    """Serialize navigation clicks for one user's current search."""
    lock = context.user_data.get("ai_search_navigation_lock")
    if lock is None:
        lock = asyncio.Lock()
        context.user_data["ai_search_navigation_lock"] = lock
    return lock


def ask_ai_button():
    return InlineKeyboardButton(
        AI_SEARCH_BUTTON_TEXT,
        callback_data="ask_ai",
    )


def ai_search_reply_keyboard():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(AI_SEARCH_BUTTON_TEXT)],
        ],
        resize_keyboard=True,
    )


def saved_offers_button():
    return InlineKeyboardButton(
        SAVED_OFFERS_BUTTON_TEXT,
        callback_data="saved_offers",
    )


def saved_offers_help_markup():
    return InlineKeyboardMarkup([[saved_offers_button()]])


def _saved_offers_button_texts():
    return {
        *SAVED_OFFERS_MAIN_BUTTONS,
        SAVED_OFFERS_BACK_BUTTON_TEXT,
        *SAVED_OFFERS_PHONE_BRANDS,
        *(label for _category, label in SAVED_OFFER_CATEGORY_FILTERS),
    }


def _saved_offers_keyboard_button(label):
    return KeyboardButton(f"{label}{_SAVED_OFFERS_BUTTON_MARKER}")


def _saved_offers_keyboard(rows):
    return ReplyKeyboardMarkup(
        [
            [_saved_offers_keyboard_button(label) for label in row]
            for row in rows
        ],
        resize_keyboard=True,
    )


def _saved_offers_reply_label(text):
    text = (text or "").strip()
    if not text.endswith(_SAVED_OFFERS_BUTTON_MARKER):
        return None
    label = text[:-len(_SAVED_OFFERS_BUTTON_MARKER)]
    return label if label in _saved_offers_button_texts() else None


def _is_saved_offers_button_text(text):
    return (
        (text or "").strip() in _saved_offers_button_texts()
        or _saved_offers_reply_label(text) is not None
    )


async def _reply_to_expired_ai_session(message, context):
    """Replace Telegram's locally retained keyboard without running a search."""
    _clear_ai_search_state(context)
    await message.reply_text(
        AI_SEARCH_SESSION_EXPIRED_TEXT,
        reply_markup=ai_search_reply_keyboard(),
    )


def _saved_offers_main_markup():
    return _saved_offers_keyboard(
        [
            SAVED_OFFERS_MAIN_BUTTONS[:3],
            SAVED_OFFERS_MAIN_BUTTONS[3:],
        ]
    )


def _saved_offers_phone_markup():
    return _saved_offers_keyboard(
        [
            SAVED_OFFERS_PHONE_BRANDS[index:index + 3]
            for index in range(0, len(SAVED_OFFERS_PHONE_BRANDS), 3)
        ] + [[SAVED_OFFERS_BACK_BUTTON_TEXT]]
    )


def _saved_offers_category_markup():
    labels = [
        label
        for _category, label in SAVED_OFFER_CATEGORY_FILTERS
    ]
    return _saved_offers_keyboard(
        [
            labels[index:index + 3]
            for index in range(0, len(labels), 3)
        ] + [[SAVED_OFFERS_BACK_BUTTON_TEXT]]
    )


def _saved_offer_intent(kind, value=None):
    intent = {
        "request_type": "trending",
        "keywords": [],
        "required_specs": [],
    }
    if kind == "brand":
        return {
            **intent,
            "request_type": "product_best",
            "keywords": [value],
            "category": "phones",
        }
    if kind == "shipping":
        return {
            **intent,
            "shipping_country": "France",
        }
    if kind == "bundle":
        return {
            **intent,
            "offer_type": "bundle",
        }
    if kind == "category":
        if value in {
            category
            for category, _label in SAVED_OFFER_CATEGORY_FILTERS
        }:
            if value in SAVED_OFFER_CATEGORY_COLUMN_FILTERS:
                return {
                    **intent,
                    "saved_category": value,
                }
            return {
                **intent,
                "saved_subcategory": value,
            }
        return {
            **intent,
            "category": value,
        }
    raise ValueError(f"Unknown saved offer filter: {kind}")


async def _activate_ai_search(message, context):
    if not await asyncio.to_thread(is_feature_enabled, "ai_search"):
        await message.reply_text(AI_SEARCH_DISABLED_TEXT)
        return False
    _clear_ai_search_state(context)
    context.user_data["ai_search_active"] = True
    await _hide_saved_offers_reply_keyboard(message, context)
    await message.reply_text(
        AI_SEARCH_HELP_TEXT,
        reply_markup=saved_offers_help_markup(),
    )
    return True


async def _delete_message_if_possible(message, context, chat_id):
    message_id = getattr(message, "message_id", None)
    bot = getattr(context, "bot", None)
    if not message_id or not bot:
        return
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        logger.debug("Could not delete temporary AI search message", exc_info=True)


async def _hide_saved_offers_reply_keyboard(message, context):
    """Remove the AI entry button while inline browsing is active."""
    reply_text = getattr(message, "reply_text", None)
    if not callable(reply_text):
        return
    try:
        # Reply keyboards and inline keyboards cannot be attached to the same
        # Telegram message. Send an invisible removal signal, then delete it
        # so the user only sees the inline browsing menu.
        removal_message = await reply_text(
            "\u2063",
            reply_markup=ReplyKeyboardRemove(),
        )
        await _delete_message_if_possible(
            removal_message,
            context,
            getattr(message.chat, "id", None),
        )
    except Exception:
        logger.debug("Could not hide the saved-offers reply keyboard", exc_info=True)


async def _cache_photo_file_id(post_id, photo_file_id):
    try:
        await asyncio.to_thread(
            save_channel_photo_file_id,
            post_id,
            photo_file_id,
        )
    except Exception:
        logger.exception("Could not cache photo file_id for post %s", post_id)


async def _cache_photo_link(post_id, link_img):
    if post_id is None or not link_img:
        return
    try:
        await asyncio.to_thread(
            save_channel_photo_link,
            post_id,
            link_img,
        )
    except Exception:
        logger.exception("Could not cache image link for post %s", post_id)


def _navigation_keyboard(has_previous=False, has_next=False):
    buttons = []
    if has_previous:
        buttons.append(
            InlineKeyboardButton("السابق", callback_data="ai_previous")
        )
    if has_next:
        buttons.append(
            InlineKeyboardButton("التالي", callback_data="ai_next")
        )
    if not buttons:
        return None
    return InlineKeyboardMarkup([buttons])


def _post_content(post):
    content = (post.get("content") or post.get("title") or "").strip()
    return content or "لا يوجد نص محفوظ لهذا العرض."


def _split_text(text, limit=4096):
    return [
        text[index:index + limit]
        for index in range(0, len(text), limit)
    ] or [""]


async def _send_text_chunks(bot, chat_id, chunks, reply_markup=None):
    sent_messages = []
    for index, chunk in enumerate(chunks):
        sent_messages.append(
            await bot.send_message(
                chat_id=chat_id,
                text=chunk,
                reply_markup=reply_markup if index == 0 else None,
            )
        )
    return sent_messages


async def _send_text_post(bot, chat_id, content, reply_markup=None):
    return await _send_text_chunks(
        bot,
        chat_id,
        _split_text(content),
        reply_markup,
    )


async def _download_source_photo(post, context):
    monitor = context.application.bot_data.get("channel_monitor")
    if not monitor:
        return None
    try:
        return await monitor.download_photo(
            post["source_channel_id"],
            post["source_message_id"],
        )
    except Exception:
        logger.exception("Could not download source photo for post %s", post.get("id"))
        return None


async def _load_source_photo_once(post, context):
    """Share one source download between concurrent result deliveries."""
    source_key = (
        post.get("source_channel_id"),
        post.get("source_message_id"),
    )
    if not all(source_key):
        return await _download_source_photo(post, context)

    task = _source_photo_tasks.get(source_key)
    if task is None or task.done():
        task = asyncio.create_task(_download_source_photo(post, context))
        _source_photo_tasks[source_key] = task

        def _forget(completed):
            if _source_photo_tasks.get(source_key) is completed:
                _source_photo_tasks.pop(source_key, None)

        task.add_done_callback(_forget)
    return await asyncio.shield(task)


async def _load_aliexpress_photo(
    post,
    excluded_origins=(),
    excluded_urls=(),
):
    """Load a product image after Telegram's source photo is unavailable."""
    product_id = post.get("aliexpress_product_id")
    if not product_id:
        product_url = extract_aliexpress_url(
            post.get("content") or post.get("title") or ""
        )
        if product_url:
            try:
                product_id = await asyncio.to_thread(
                    extract_product_id,
                    product_url,
                )
            except Exception:
                logger.exception(
                    "Could not extract AliExpress product id for post %s",
                    post.get("id"),
                )
    if not product_id:
        return None

    excluded_origins = set(excluded_origins or ())
    excluded_urls = set(excluded_urls or ())
    if "aliexpress_api" not in excluded_origins:
        try:
            product = await asyncio.to_thread(
                get_product_info_from_api,
                product_id,
            )
        except Exception:
            logger.exception(
                "Could not load AliExpress API details for post %s",
                post.get("id"),
            )
            product = None
        image_url = product.get("image_url") if product else None
        if image_url and image_url not in excluded_urls:
            post["_resolved_photo_origin"] = "aliexpress_api"
            post["link_img"] = image_url
            await _cache_photo_link(post.get("id"), image_url)
            return image_url

    if "aliexpress_scraping" not in excluded_origins:
        try:
            scraped = await asyncio.to_thread(
                get_product_details_scraping,
                product_id,
            )
        except Exception:
            logger.exception(
                "Could not scrape AliExpress image for post %s",
                post.get("id"),
            )
            scraped = None
        image_url = scraped.get("image_url") if scraped else None
        if image_url and image_url not in excluded_urls:
            post["_resolved_photo_origin"] = "aliexpress_scraping"
            post["link_img"] = image_url
            await _cache_photo_link(post.get("id"), image_url)
            return image_url
    return None


async def _load_post_photo(
    post,
    context,
    allow_source_download=True,
    allow_cached=True,
    allow_cached_link=True,
    excluded_origins=(),
    excluded_urls=(),
):
    excluded_origins = set(excluded_origins or ())
    excluded_urls = set(excluded_urls or ())
    photo = post.get("photo_file_id")
    if photo and allow_cached and "cached" not in excluded_origins:
        post["_resolved_photo_origin"] = "cached"
        return photo, False

    photo_link = post.get("link_img")
    if (
        photo_link
        and allow_cached_link
        and "link_img" not in excluded_origins
        and photo_link not in excluded_urls
    ):
        post["_resolved_photo_origin"] = "link_img"
        return photo_link, False

    if allow_source_download and "telegram" not in excluded_origins:
        photo = await _load_source_photo_once(post, context)
        if photo is not None:
            post["_resolved_photo_origin"] = "telegram"
            return photo, True

    photo = await _load_aliexpress_photo(
        post,
        excluded_origins=excluded_origins,
        excluded_urls=excluded_urls,
    )
    return photo, False


def _photo_to_input_file(photo, post, downloaded_source):
    if not downloaded_source:
        return photo
    return InputFile(
        photo,
        filename=f"channel-post-{post.get('id', 'image')}.jpg",
    )


def _should_cache_resolved_photo(post, downloaded_source):
    return (
        downloaded_source
        or not post.get("photo_file_id")
        or post.get("_resolved_photo_origin")
        in {
            "link_img",
            "aliexpress_api",
            "aliexpress_scraping",
        }
    )


def _caption_for_content(content):
    return content if len(content) <= 1024 else "📦"


async def _record_auxiliary_messages(context, messages):
    context.user_data["ai_search_auxiliary_message_ids"] = [
        message.message_id
        for message in messages
        if getattr(message, "message_id", None)
    ]


async def _send_post_photo_with_fallback(
    bot,
    chat_id,
    context,
    post,
    content,
    reply_markup=None,
    allow_source_download=True,
    excluded_origins=(),
    excluded_urls=(),
):
    """Try each available image source once, in priority order."""
    attempted_origins = set(excluded_origins or ())
    attempted_urls = set(excluded_urls or ())

    while True:
        photo, downloaded_source = await _load_post_photo(
            post,
            context,
            allow_source_download=allow_source_download,
            allow_cached="cached" not in attempted_origins,
            allow_cached_link="link_img" not in attempted_origins,
            excluded_origins=attempted_origins,
            excluded_urls=attempted_urls,
        )
        if not photo:
            return None

        origin = post.get("_resolved_photo_origin")
        if not origin or origin in attempted_origins:
            return None

        try:
            sent = await bot.send_photo(
                chat_id=chat_id,
                photo=_photo_to_input_file(
                    photo,
                    post,
                    downloaded_source,
                ),
                caption=_caption_for_content(content),
                reply_markup=reply_markup,
            )
        except Exception:
            logger.exception(
                "Could not send photo for post %s from %s",
                post.get("id"),
                origin,
            )
            attempted_origins.add(origin)
            if not downloaded_source:
                attempted_urls.add(photo)
            continue

        if (
            getattr(sent, "photo", None)
            and _should_cache_resolved_photo(post, downloaded_source)
        ):
            await _cache_photo_file_id(
                post["id"],
                sent.photo[-1].file_id,
            )
        return sent


async def _delete_auxiliary_messages(bot, chat_id, context):
    message_ids = context.user_data.pop("ai_search_auxiliary_message_ids", [])
    for message_id in message_ids:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception:
            logger.debug(
                "Could not delete auxiliary AI result message %s",
                message_id,
                exc_info=True,
            )


async def send_stored_post(
    chat_id,
    context,
    post,
    has_previous=False,
    has_next=False,
    allow_source_download=True,
):
    """Send the stored post without adding a custom content template."""
    bot = context.bot
    content = _post_content(post)
    navigation = _navigation_keyboard(has_previous, has_next)
    sent = await _send_post_photo_with_fallback(
        bot,
        chat_id,
        context,
        post,
        content,
        navigation,
        allow_source_download=allow_source_download,
    )
    if sent:
        if len(content) > 1024:
            auxiliary = await _send_text_post(bot, chat_id, content)
            await _record_auxiliary_messages(context, auxiliary)
        return sent

    sent_messages = await _send_text_post(bot, chat_id, content, navigation)
    if len(sent_messages) > 1:
        await _record_auxiliary_messages(context, sent_messages[1:])
    return None


async def _replace_stored_post_message(query, context, post, has_previous, has_next):
    bot = context.bot
    message = query.message
    chat_id = message.chat.id
    message_id = message.message_id
    content = _post_content(post)
    # Navigation uses the same photo priority as the first result: use the
    # cached Bot API file_id, then download the source channel photo when
    # needed and cache its file_id after sending it.
    photo, downloaded_source = await _load_post_photo(
        post,
        context,
        allow_source_download=True,
    )
    navigation = _navigation_keyboard(has_previous, has_next)

    await _delete_auxiliary_messages(bot, chat_id, context)

    if photo and getattr(message, "photo", None) and not downloaded_source:
        try:
            edited = await bot.edit_message_media(
                chat_id=chat_id,
                message_id=message_id,
                media=InputMediaPhoto(
                    media=_photo_to_input_file(photo, post, downloaded_source),
                    caption=_caption_for_content(content),
                ),
                reply_markup=navigation,
            )
        except Exception:
            logger.exception(
                "Could not edit stored photo for post %s",
                post.get("id"),
            )
            excluded_origins = {post.get("_resolved_photo_origin")}
            excluded_urls = {photo}
            sent = await _send_post_photo_with_fallback(
                bot,
                chat_id,
                context,
                post,
                content,
                navigation,
                excluded_origins=excluded_origins,
                excluded_urls=excluded_urls,
            )
            if not sent:
                return None
            try:
                await bot.delete_message(
                    chat_id=chat_id,
                    message_id=message_id,
                )
            except Exception:
                logger.warning(
                    "Could not delete replaced AI result message %s",
                    message_id,
                    exc_info=True,
                )
            if len(content) > 1024:
                auxiliary = await _send_text_post(bot, chat_id, content)
                await _record_auxiliary_messages(context, auxiliary)
            return sent
        if getattr(edited, "photo", None):
            if _should_cache_resolved_photo(post, downloaded_source):
                await _cache_photo_file_id(
                    post["id"],
                    edited.photo[-1].file_id,
                )
    elif not photo and not getattr(message, "photo", None):
        chunks = _split_text(content)
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=chunks[0],
            reply_markup=navigation,
        )
        if len(chunks) > 1:
            auxiliary = await _send_text_chunks(bot, chat_id, chunks[1:])
            await _record_auxiliary_messages(context, auxiliary)
    elif photo and downloaded_source:
        # Uploading a newly downloaded file while editing media can fail on
        # Telegram. Send it as a fresh photo message, just like the first
        # result, then cache the Bot API file_id for future navigation.
        try:
            sent = await bot.send_photo(
                chat_id=chat_id,
                photo=_photo_to_input_file(photo, post, downloaded_source),
                caption=_caption_for_content(content),
                reply_markup=navigation,
            )
        except Exception:
            logger.exception(
                "Could not upload replacement photo for post %s",
                post.get("id"),
            )
            sent = await _send_post_photo_with_fallback(
                bot,
                chat_id,
                context,
                post,
                content,
                navigation,
                excluded_origins={post.get("_resolved_photo_origin")},
            )
            if not sent:
                return None
        if getattr(sent, "photo", None):
            if _should_cache_resolved_photo(post, downloaded_source):
                await _cache_photo_file_id(
                    post["id"],
                    sent.photo[-1].file_id,
                )
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception:
            logger.warning(
                "Could not delete replaced AI result message %s",
                message_id,
                exc_info=True,
            )
        if len(content) > 1024:
            auxiliary = await _send_text_post(bot, chat_id, content)
            await _record_auxiliary_messages(context, auxiliary)
        return sent
    else:
        # Telegram cannot convert a text message to media (or vice versa) in
        # place. Delete only this exceptional type-changing message, then
        # render the replacement once.
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
        sent = await send_stored_post(
            chat_id,
            context,
            post,
            has_previous=has_previous,
            has_next=has_next,
            allow_source_download=True,
        )
        return sent

    if photo and len(content) > 1024:
        auxiliary = await _send_text_post(bot, chat_id, content)
        await _record_auxiliary_messages(context, auxiliary)
    return None


async def ask_ai_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await _activate_ai_search(query.message, context)


async def handle_ai_search_button_message(update, context):
    text = (update.effective_message.text or "").strip()
    if context.user_data.get("ai_search_active"):
        return text == AI_SEARCH_BUTTON_TEXT
    if text != AI_SEARCH_BUTTON_TEXT:
        return False

    await _activate_ai_search(update.effective_message, context)
    return True


async def handle_stale_saved_offers_message(update, context):
    """Handle saved-offer reply buttons retained locally across a restart."""
    if context.user_data.get("ai_search_active"):
        return False

    message = update.effective_message
    text = (message.text or "").strip()
    if not _is_saved_offers_button_text(text):
        return False

    await _reply_to_expired_ai_session(message, context)
    return True


def _saved_offers_callback_data_for_reply_label(label, menu):
    if menu == "main":
        return {
            SAVED_OFFERS_MAIN_BUTTONS[0]: "saved_offers:phones",
            SAVED_OFFERS_MAIN_BUTTONS[1]: "saved_offers:shipping",
            SAVED_OFFERS_MAIN_BUTTONS[2]: "saved_offers:bundle",
            SAVED_OFFERS_MAIN_BUTTONS[3]: "saved_offers:other",
        }.get(label)

    if menu == "phones":
        if label == SAVED_OFFERS_BACK_BUTTON_TEXT:
            return "saved_offers:back:main"
        if label in SAVED_OFFERS_PHONE_BRANDS:
            return f"saved_offers:brand:{label.casefold()}"

    if menu == "categories":
        if label == SAVED_OFFERS_BACK_BUTTON_TEXT:
            return "saved_offers:back:main"
        for index, (_category, category_label) in enumerate(
            SAVED_OFFER_CATEGORY_FILTERS
        ):
            if label == category_label:
                return f"saved_offers:category:{index}"

    return None


async def handle_saved_offers_reply_message(update, context):
    """Route only marked reply-keyboard taps to saved-offer actions."""
    message = update.effective_message
    label = _saved_offers_reply_label(getattr(message, "text", None))
    if label is None or not context.user_data.get("ai_search_active"):
        return False

    callback_data = _saved_offers_callback_data_for_reply_label(
        label,
        context.user_data.get("saved_offers_menu"),
    )
    if callback_data is None:
        await _reply_to_expired_ai_session(message, context)
        return True

    await _dispatch_saved_offers_action(message, context, callback_data)
    return True


async def handle_ai_search_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    started_at = time.perf_counter()
    parse_ms = search_ms = first_result_ms = None
    active = bool(context.user_data.get("ai_search_active"))
    if not active:
        return False

    text = (update.effective_message.text or "").strip()
    if extract_aliexpress_url(text):
        _clear_ai_search_state(context)
        return False
    if not await asyncio.to_thread(is_feature_enabled, "ai_search"):
        _clear_ai_search_state(context)
        await update.effective_message.reply_text(
            AI_SEARCH_DISABLED_TEXT,
            reply_markup=ai_search_reply_keyboard(),
        )
        return True
    if re.search(r"https?://", text, re.IGNORECASE):
        _clear_ai_search_state(context)
        await update.effective_message.reply_text(
            "⚠️ من فضلك قم بإرسال روابط منتجات Aliexpress فقط ",
            reply_markup=ai_search_reply_keyboard(),
        )
        return True

    loading_message = await update.effective_message.reply_text(AI_SEARCH_LOADING_TEXT)
    try:
        parse_started_at = time.perf_counter()
        try:
            async with ai_request_priority.user_request():
                intent = await asyncio.wait_for(
                    asyncio.to_thread(parse_user_request, text),
                    timeout=AI_USER_REQUEST_TIMEOUT_SECONDS,
                )
            parse_ms = (time.perf_counter() - parse_started_at) * 1000
        except asyncio.TimeoutError:
            parse_ms = (time.perf_counter() - parse_started_at) * 1000
            logger.warning(
                "AI user request parsing timed out after %.1fs; "
                "using cache-restricted local parser",
                AI_USER_REQUEST_TIMEOUT_SECONDS,
            )
            intent = _safe_local_user_request_with_cache(text)
        except GeminiError:
            logger.exception("AI search request failed")
            await update.effective_message.reply_text(
                "تعذر تشغيل البحث بالذكاء الاصطناعي حاليًا، حاول مرة أخرى لاحقًا."
            )
            return True
        except Exception:
            logger.exception("Unexpected AI search error")
            await update.effective_message.reply_text(
                "⚠️حدث خطأ أثناء فهم طلب البحث."
            )
            return True

        if intent["request_type"] == "unsupported":
            if intent.get("no_results"):
                await update.effective_message.reply_text(
                    AI_SEARCH_NO_RESULTS_TEXT,
                    reply_markup=saved_offers_help_markup(),
                )
            else:
                await update.effective_message.reply_text(
                    AI_SEARCH_SUPPORTED_TEXT
                )
            return True

        search_started_at = time.perf_counter()
        try:
            posts = await asyncio.to_thread(search_channel_posts, intent)
            search_ms = (time.perf_counter() - search_started_at) * 1000
        except Exception:
            logger.exception("AI search database query failed")
            await update.effective_message.reply_text(
                AI_SEARCH_DATABASE_ERROR_TEXT,
                reply_markup=saved_offers_help_markup(),
            )
            return True
        if not posts:
            await update.effective_message.reply_text(
                AI_SEARCH_NO_RESULTS_TEXT,
                reply_markup=saved_offers_help_markup(),
            )
            return True

        # Keep the ranked posts in memory for the whole search session.
        _store_ai_search_results(context, posts)
        await send_stored_post(
            update.effective_chat.id,
            context,
            posts[0],
            has_previous=False,
            has_next=len(posts) > 1,
        )
        first_result_ms = (time.perf_counter() - started_at) * 1000
        return True
    finally:
        await _delete_message_if_possible(
            loading_message,
            context,
            update.effective_chat.id,
        )
        logger.info(
            "AI search timing parse_ms=%s search_ms=%s first_result_ms=%s "
            "total_ms=%.1f",
            f"{parse_ms:.1f}" if parse_ms is not None else "n/a",
            f"{search_ms:.1f}" if search_ms is not None else "n/a",
            f"{first_result_ms:.1f}" if first_result_ms is not None else "n/a",
            (time.perf_counter() - started_at) * 1000,
        )


async def _show_saved_offers_main(message, context=None):
    try:
        count = await asyncio.to_thread(count_processed_channel_posts)
    except Exception:
        logger.exception("Could not count saved offers")
        await message.reply_text(AI_SEARCH_DATABASE_ERROR_TEXT)
        return

    if context is not None:
        context.user_data["saved_offers_menu"] = "main"
    await message.reply_text(
        SAVED_OFFERS_MENU_TEXT.format(count=count),
        reply_markup=_saved_offers_main_markup(),
    )


async def _show_saved_offers_phones(message, context=None):
    if context is not None:
        context.user_data["saved_offers_menu"] = "phones"
    await message.reply_text(
        SAVED_OFFERS_PHONE_TEXT,
        reply_markup=_saved_offers_phone_markup(),
    )


async def _show_saved_offers_categories(message, context=None):
    if context is not None:
        context.user_data["saved_offers_menu"] = "categories"
    await message.reply_text(
        SAVED_OFFERS_CATEGORY_TEXT,
        reply_markup=_saved_offers_category_markup(),
    )


async def _search_saved_offers(message, context, intent):
    try:
        posts = await asyncio.to_thread(search_channel_posts, intent)
    except Exception:
        logger.exception("Saved offers search failed")
        await message.reply_text(AI_SEARCH_DATABASE_ERROR_TEXT)
        return

    if not posts:
        _store_ai_search_results(context, [])
        await message.reply_text(AI_SEARCH_NO_RESULTS_TEXT)
        return

    _store_ai_search_results(context, posts)
    try:
        await send_stored_post(
            message.chat.id,
            context,
            posts[0],
            has_previous=False,
            has_next=len(posts) > 1,
        )
    except Exception:
        logger.exception("Could not display saved offer")
        await message.reply_text(AI_SEARCH_DATABASE_ERROR_TEXT)


async def _dispatch_saved_offers_action(message, context, data):
    if data == "saved_offers":
        await _show_saved_offers_main(message, context)
        return
    if data == "saved_offers:phones":
        await _show_saved_offers_phones(message, context)
        return
    if data == "saved_offers:other":
        await _show_saved_offers_categories(message, context)
        return
    if data == "saved_offers:shipping":
        await _search_saved_offers(
            message,
            context,
            _saved_offer_intent("shipping"),
        )
        return
    if data == "saved_offers:bundle":
        await _search_saved_offers(
            message,
            context,
            _saved_offer_intent("bundle"),
        )
        return
    if data == "saved_offers:back:main":
        await _show_saved_offers_main(message, context)
        return

    if data.startswith("saved_offers:back:phones"):
        await _show_saved_offers_phones(message, context)
        return

    if data.startswith("saved_offers:brand:"):
        brand = data.rsplit(":", 1)[-1]
        await _search_saved_offers(
            message,
            context,
            _saved_offer_intent("brand", brand),
        )
        return

    if data.startswith("saved_offers:category:"):
        try:
            category_index = int(data.rsplit(":", 1)[-1])
            category = SAVED_OFFER_CATEGORY_FILTERS[
                category_index
            ][0]
        except (ValueError, IndexError):
            await message.edit_text(AI_SEARCH_DATABASE_ERROR_TEXT)
            return
        await _search_saved_offers(
            message,
            context,
            _saved_offer_intent("category", category),
        )
        return

    await message.edit_text(AI_SEARCH_DATABASE_ERROR_TEXT)


async def saved_offers_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = str(query.data or "")

    if data == "saved_offers:show":
        # Ignore a stale inline button from the former restore flow. Telegram
        # owns restoring a keyboard that the user collapsed locally.
        return

    if not context.user_data.get("ai_search_active"):
        await _reply_to_expired_ai_session(query.message, context)
        return

    await _dispatch_saved_offers_action(query.message, context, data)


async def _navigate_ai_results(update, context, direction):
    query = update.callback_query
    await query.answer()

    if not context.user_data.get("ai_search_results"):
        await _reply_to_expired_ai_session(query.message, context)
        return

    if not await asyncio.to_thread(is_feature_enabled, "ai_search"):
        _clear_ai_search_state(context)
        await query.message.reply_text(
            AI_SEARCH_DISABLED_TEXT,
            reply_markup=ai_search_reply_keyboard(),
        )
        return

    async with _navigation_lock(context):
        results = context.user_data.get("ai_search_results", [])
        current_index = context.user_data.get("ai_search_index", 0)
        index = current_index + direction
        if index < 0 or index >= len(results):
            return

        result = results[index]
        # Accept the former id-only state as a compatibility path for an
        # update already in flight during a deployment. New searches store
        # complete posts and avoid this database round trip.
        if isinstance(result, dict):
            post = result
        else:
            post = await asyncio.to_thread(get_channel_post, result)
        if not post:
            await query.message.reply_text("⚠️تعذر العثور على العرض التالي.")
            return

        try:
            await _replace_stored_post_message(
                query,
                context,
                post,
                has_previous=index > 0,
                has_next=index < len(results) - 1,
            )
        except Exception:
            logger.exception("Could not display AI search result at index %s", index)
            await query.message.reply_text(
                "⚠️تعذر عرض هذا العرض حاليًا، حاول الضغط على التالي مرة أخرى."
            )
            return

        # Advance only after the new result has been rendered successfully.
        context.user_data["ai_search_index"] = index


async def ai_next_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _navigate_ai_results(update, context, 1)


async def ai_previous_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _navigate_ai_results(update, context, -1)
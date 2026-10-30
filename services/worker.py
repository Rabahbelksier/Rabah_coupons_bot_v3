import html
import logging
import asyncio
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from utils.parser import extract_product_id
from core.product import get_product_info_from_api
from core.affiliate import generate_affiliate_links
from core.db import is_feature_enabled
from core.scraper import get_product_details_scraping
from utils.app_promotion import app_promotion_button
from handlers.ai_search import ai_search_reply_keyboard, ask_ai_button

logger = logging.getLogger(__name__)


async def send_reply_keyboard_anchor(chat_id: int, context):
    """Attach the locally hideable AI keyboard to a durable chat message."""
    if context.user_data.get("ai_search_active"):
        return None
    try:
        return await context.bot.send_message(
            chat_id=chat_id,
            text="\u2063",
            reply_markup=ai_search_reply_keyboard(),
        )
    except Exception:
        logger.debug("Could not retain the AI reply keyboard", exc_info=True)
        return None


async def _delete_reply_keyboard_anchor(bot, chat_id, anchor_message):
    """Delete the invisible keyboard anchor after an offer is delivered."""
    if anchor_message is None:
        return True

    message_id = getattr(anchor_message, "message_id", None)
    if not message_id:
        return False
    try:
        await bot.delete_message(
            chat_id=chat_id,
            message_id=message_id,
        )
        return True
    except Exception:
        logger.debug(
            "Could not remove the AI keyboard anchor after delivering offers",
            exc_info=True,
        )
        return False


async def process_link_for_user(
    chat_id: int,
    url: str,
    context,
    reply_keyboard_anchor=None,
):
    """Process one product URL and report whether its keyboard anchor is gone."""
    bot = context.bot
    loading_msg = None
    anchor_removed = False

    try:
        product_id = await asyncio.to_thread(extract_product_id, url)
    except Exception:
        logger.exception("Could not extract the product id from %s", url)
        await bot.send_message(
            chat_id=chat_id,
            text="❌ تعذر قراءة رابط المنتج، حاول إرسال الرابط مرة أخرى.",
        )
        return False
    if not product_id:
        await bot.send_message(
            chat_id=chat_id,
            text="❌ انسخ رابط المنتج من تطبيق aliexpress او الموقع",
        )
        return False

    loading_msg = await bot.send_message(
        chat_id=chat_id,
        text="⏳ جاري البحث عن العروض",
    )

    try:
        product_task = asyncio.to_thread(get_product_info_from_api, product_id)
        links_task = asyncio.to_thread(generate_affiliate_links, product_id)

        product, links = await asyncio.gather(product_task, links_task)

        if not product or (not product.get('title') or product.get('title') == 'غير متوفر'):
            logger.info(f"API returned insufficient data for {product_id}, trying scraping...")
            scraped = await asyncio.to_thread(get_product_details_scraping, product_id)
            if not product:
                product = {'title': None, 'image_url': None}
            if scraped.get('title'):
                product['title'] = (
                    product.get('title')
                    if product.get('title') and product.get('title') != 'غير متوفر'
                    else scraped['title']
                )
            if scraped.get('image_url') and not product.get('image_url'):
                product['image_url'] = scraped['image_url']

        title = product.get('title') if product and product.get('title') else None
        image_url = product.get('image_url') if product else None
        if image_url:
            product_images = context.user_data.setdefault("product_images", {})
            product_images.pop(str(product_id), None)
            product_images[str(product_id)] = image_url
            while len(product_images) > 20:
                product_images.pop(next(iter(product_images)))

        ai_enabled, smart_cart_enabled = await asyncio.gather(
            asyncio.to_thread(is_feature_enabled, "ai_search"),
            asyncio.to_thread(is_feature_enabled, "smart_cart"),
        )
        keyboard = [
            [InlineKeyboardButton("📋 تفاصيل المنتج الكاملة", callback_data=f"details_{product_id}")],
            [InlineKeyboardButton("الكوبونات", callback_data="show_coupons")],
            [app_promotion_button()],
        ]
        if smart_cart_enabled:
            keyboard.insert(
                1,
                [
                    InlineKeyboardButton(
                        "🛒 استعمال طريقة السلة الذكية AI",
                        callback_data=f"smart_cart_{product_id}",
                    )
                ],
            )
        if ai_enabled:
            keyboard.insert(2 if smart_cart_enabled else 1, [ask_ai_button()])
        reply_markup = InlineKeyboardMarkup(keyboard)

        if title:
            response_text = f"📦تخفيض على:\n<code>{html.escape(title)}</code>\n\n" + "\n\n".join(links)
        else:
            response_text = "📦 تخفيض على منتج AliExpress\n\n" + "\n\n".join(links)

        sent_photo = None
        if image_url:
            try:
                sent_photo = await bot.send_photo(
                    chat_id=chat_id,
                    photo=image_url,
                    caption=response_text,
                    parse_mode="HTML",
                    reply_markup=reply_markup,
                )
            except Exception as photo_err:
                logger.error(f"Failed to send photo: {photo_err}")
                await bot.send_message(chat_id=chat_id, text=response_text, parse_mode="HTML", reply_markup=reply_markup)
        else:
            await bot.send_message(chat_id=chat_id, text=response_text, parse_mode="HTML", reply_markup=reply_markup)

        anchor_removed = await _delete_reply_keyboard_anchor(
            bot,
            chat_id,
            reply_keyboard_anchor,
        )

        if sent_photo and getattr(sent_photo, "photo", None):
            product_image_file_ids = context.user_data.setdefault(
                "product_image_file_ids", {}
            )
            product_image_file_ids.pop(str(product_id), None)
            product_image_file_ids[str(product_id)] = sent_photo.photo[-1].file_id
            while len(product_image_file_ids) > 20:
                product_image_file_ids.pop(next(iter(product_image_file_ids)))

    except Exception as e:
        logger.error(f"Error processing link {url}: {e}")
        await bot.send_message(
            chat_id=chat_id,
            text="❌ حدث خطأ أثناء معالجة طلبك، اتصل بالأدمن @Rabahbelksier",
        )
        return anchor_removed

    finally:
        try:
            if loading_msg:
                await bot.delete_message(
                    chat_id=chat_id,
                    message_id=loading_msg.message_id,
                )
        except Exception:
            pass

    return anchor_removed

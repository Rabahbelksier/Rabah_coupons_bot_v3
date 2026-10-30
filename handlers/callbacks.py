import asyncio
import logging
from telegram import Update
from telegram.ext import ContextTypes

from core.affiliate import generate_affiliate_link
from core.coupons import get_available_coupons, get_best_coupon_for_price
from core.product import get_full_product_details_from_api
from utils.formatter import format_product_message
from utils.app_promotion import (
    APP_PROMOTION_IMAGE_URL,
    APP_PROMOTION_TEXT,
    app_download_keyboard,
)
from utils.telegram import remove_pressed_button
from core.smart_cart import other_smart_cart_callback, select_coupon, start_smart_cart
from handlers.ai_search import ai_next_callback, ask_ai_callback

logger = logging.getLogger(__name__)


async def app_promotion_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await remove_pressed_button(query)
    await query.message.reply_photo(
        photo=APP_PROMOTION_IMAGE_URL,
        caption=APP_PROMOTION_TEXT,
        reply_markup=app_download_keyboard(),
    )


async def product_details_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await remove_pressed_button(query)

    product_id = query.data.split('_')[1]

    try:
        status_message = await query.message.reply_text("⏳ جاري جلب التفاصيل الكاملة من AliExpress...")

        info = await asyncio.to_thread(
            get_full_product_details_from_api,
            product_id,
        )
        if not info:
            await status_message.edit_text("⚠️ لا يمكن جلب تفاصيل هذا المنتج")
            return

        coupons = await asyncio.to_thread(get_available_coupons)
        coupon = get_best_coupon_for_price(coupons, info.get("target_sale_price"))

        store_url = info.get("shop_url")
        if store_url and store_url != "غير متوفر":
            generated_store_url = await asyncio.to_thread(
                generate_affiliate_link,
                store_url,
            )
            if generated_store_url:
                info["shop_url"] = generated_store_url

        await status_message.edit_text(
            format_product_message(info, coupon=coupon),
            parse_mode='Markdown',
            disable_web_page_preview=True,
        )

    except Exception as e:
        logger.error(f"Error in details callback: {e}")
        await query.message.reply_text("❌ حدث خطأ غير متوقع أثناء جلب التفاصيل.")


async def smart_cart_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    product_id = update.callback_query.data.split("_")[-1]
    await start_smart_cart(update, context, product_id)


async def other_smart_cart_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await other_smart_cart_callback(update, context)

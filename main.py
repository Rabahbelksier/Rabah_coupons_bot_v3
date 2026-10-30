import os
import atexit
import logging
import asyncio
import threading
import requests
from flask import Flask, request, Response, jsonify
from telegram import BotCommand, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from config import (
    CHANNEL_MONITOR_ENABLED,
    ERROR_LOG_CLEANUP_INTERVAL_SECONDS,
    PUBLIC_URL,
    TOKEN,
    PORT,
)
from core.db import delete_expired_error_logs, init_db
from handlers.help import (
    help_command,
    help_topic_callback,
    more_details_callback,
)
from handlers.start import start, start_ai_callback
from handlers.coupons import coupons_callback, coupons_command
from handlers.admin import (
    ADMIN_CALLBACK_PATTERN,
    admin_callback,
    admin_command,
)
from handlers.messages import handle_message
from handlers.callbacks import (
    other_smart_cart_button_callback,
    app_promotion_callback,
    product_details_callback,
    select_coupon,
    smart_cart_callback,
)
from handlers.ai_search import (
    ai_next_callback,
    ai_previous_callback,
    ask_ai_callback,
    saved_offers_callback,
)
from core.scraper import get_product_details_scraping
from core.ai import (
    rebuild_and_sync_gemini_keyword_cache_on_startup,
    start_ai_service_background_worker,
    stop_ai_service_background_worker,
)
from services.channel_monitor import ChannelMonitor
from services.auto_publisher import AutoPublisher
from services.telegram_runtime import TelegramRuntime
from utils.error_logging import install_error_log_handler

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)
_gemini_phrase_cache_startup_lock = threading.Lock()
_gemini_phrase_cache_startup_scheduled = False

app = Flask(__name__)

SCRAPE_API_KEY = os.getenv('SCRAPE_API_KEY', '')

def _build_telegram_app(is_recovery=False):
    """Build the Telegram application without changing handler behavior."""
    global channel_monitor
    if is_recovery:
        # The monitor owns tasks bound to the old event loop. Give recovery a
        # fresh monitor so optional channel work can resume on the new loop.
        channel_monitor = ChannelMonitor()

    application = (
        Application.builder()
        .token(TOKEN)
        .updater(None)
        .concurrent_updates(16)
        .build()
    )
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("coupons", coupons_command))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(
        MessageHandler(
            (filters.TEXT | filters.PHOTO) & ~filters.COMMAND,
            handle_message,
        )
    )
    application.add_handler(
        CallbackQueryHandler(product_details_callback, pattern="^details_")
    )
    application.add_handler(
        CallbackQueryHandler(smart_cart_callback, pattern="^smart_cart_")
    )
    application.add_handler(
        CallbackQueryHandler(select_coupon, pattern="^smart_coupon_")
    )
    application.add_handler(
        CallbackQueryHandler(
            app_promotion_callback,
            pattern="^app_promotion$",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            other_smart_cart_button_callback,
            pattern="^smart_other_",
        )
    )
    application.add_handler(
        CallbackQueryHandler(coupons_callback, pattern="^show_coupons$")
    )
    application.add_handler(
        CallbackQueryHandler(
            help_topic_callback,
            pattern="^help_(basic|smart_cart|ai_info)$",
        )
    )
    application.add_handler(
        CallbackQueryHandler(more_details_callback, pattern="^more_details$")
    )
    application.add_handler(
        CallbackQueryHandler(start_ai_callback, pattern="^start_ai$")
    )
    application.add_handler(
        CallbackQueryHandler(
            admin_callback,
            pattern=ADMIN_CALLBACK_PATTERN,
        )
    )
    application.add_handler(
        CallbackQueryHandler(ask_ai_callback, pattern="^ask_ai$")
    )
    application.add_handler(
        CallbackQueryHandler(
            saved_offers_callback,
            pattern="^saved_offers(?:$|:)",
        )
    )
    application.add_handler(
        CallbackQueryHandler(ai_next_callback, pattern="^ai_next$")
    )
    application.add_handler(
        CallbackQueryHandler(ai_previous_callback, pattern="^ai_previous$")
    )
    return application

channel_monitor = ChannelMonitor()
auto_publisher = AutoPublisher()
_webhook_refresh_stop = threading.Event()
_webhook_refresh_thread = None
WEBHOOK_REFRESH_INTERVAL_SECONDS = 5 * 60

BOT_COMMANDS = [
    BotCommand("start", "بدء استخدام البوت"),
    BotCommand("help", "شرح طريقة استعمال البوت"),
    BotCommand("coupons", "عرض الكوبونات والرموز الترويجية"),
]


def _schedule_background(coroutine, operation):
    return _runtime.submit(coroutine, operation)


async def _configure_bot_commands(application):
    await application.bot.set_my_commands(BOT_COMMANDS)
    logger.info("Telegram command menu configured")


async def _start_channel_monitor():
    await channel_monitor.start()


async def _start_auto_publisher(application):
    await auto_publisher.start(application)


async def _cleanup_error_logs_loop():
    """Prune old persisted errors without depending on channel monitoring."""
    while True:
        try:
            deleted_rows = await asyncio.to_thread(delete_expired_error_logs)
            if deleted_rows:
                logger.info(
                    "Deleted %d error log(s) older than the retention window",
                    deleted_rows,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            # A cleanup outage must not affect error recording or bot requests.
            logger.exception("Error log cleanup failed")
        await asyncio.sleep(ERROR_LOG_CLEANUP_INTERVAL_SECONDS)


def _stop_background_services():
    _webhook_refresh_stop.set()
    try:
        _runtime.stop()
    except Exception:
        logger.exception("Telegram runtime shutdown failed")
    try:
        stop_ai_service_background_worker()
    except Exception:
        logger.exception("AI quota worker shutdown failed")
    try:
        auto_publisher.stop()
    except Exception:
        logger.exception("Automatic publisher shutdown failed")


def _on_telegram_ready(application):
    global _gemini_phrase_cache_startup_scheduled
    application.bot_data["channel_monitor"] = channel_monitor
    application.bot_data["auto_publisher"] = auto_publisher
    try:
        start_ai_service_background_worker()
    except Exception:
        logger.exception("AI quota worker startup failed")

    with _gemini_phrase_cache_startup_lock:
        should_prepare_phrase_cache = not _gemini_phrase_cache_startup_scheduled
        _gemini_phrase_cache_startup_scheduled = True
    if should_prepare_phrase_cache:
        _schedule_background(
            asyncio.to_thread(
                rebuild_and_sync_gemini_keyword_cache_on_startup
            ),
            "Gemini phrase cache startup rebuild and sync",
        )
    _schedule_background(
        _configure_bot_commands(application),
        "Telegram command menu configuration",
    )
    if CHANNEL_MONITOR_ENABLED:
        _schedule_background(
            _start_channel_monitor(),
            "Channel monitor startup",
        )
    else:
        logger.info(
            "Channel monitor is disabled in this environment; set "
            "CHANNEL_MONITOR_ENABLED=true to enable it"
        )
    _schedule_background(
        _start_auto_publisher(application),
        "Automatic channel publisher startup",
    )
    _schedule_background(
        _cleanup_error_logs_loop(),
        "Error log cleanup",
    )


_runtime = TelegramRuntime(
    _build_telegram_app,
    _on_telegram_ready,
    logger,
)


def _ensure_ready():
    return _runtime.ensure_ready()


@app.route('/')
def index():
    return 'Bot is running'


@app.route(f'/{TOKEN}', methods=['POST'])
def webhook():
    try:
        loop = _ensure_ready()
        application = _runtime.application
        json_data = request.get_json(force=True)
        update = Update.de_json(json_data, application.bot)
        _runtime.submit(
            application.process_update(update),
            "Telegram update processing",
        )
        return Response(status=200)
    except Exception as e:
        logger.error("Webhook error: %s", e, exc_info=True)
        # Telegram retries webhook deliveries after a non-2xx response. Do
        # not acknowledge an update that could not be scheduled.
        return Response(status=503)


@app.route('/scrape', methods=['GET'])
def scrape_product():
    key = request.args.get('key', '')
    if SCRAPE_API_KEY and key != SCRAPE_API_KEY:
        return jsonify({'error': 'Unauthorized'}), 401

    product_id = request.args.get('product_id', '').strip()
    if not product_id:
        return jsonify({'error': 'product_id required'}), 400

    logger.info(f"Scrape request for product_id: {product_id}")
    result = get_product_details_scraping(product_id)
    return jsonify({
        'title': result.get('title'),
        'image_url': result.get('image_url')
    })


def set_webhook():
    if PUBLIC_URL:
        webhook_url = f"{PUBLIC_URL}/{TOKEN}"
        url = f"https://api.telegram.org/bot{TOKEN}/setWebhook"
        try:
            response = requests.post(
                url,
                json={
                    "url": webhook_url,
                    "allowed_updates": [
                        "message",
                        "callback_query",
                        "channel_post",
                        "edited_channel_post",
                    ],
                },
                timeout=15,
            )
            response.raise_for_status()
            logger.info("Telegram webhook configured: %s", response.json())
            return True
        except Exception:
            logger.exception("Telegram webhook configuration failed")
            return False
    else:
        logger.warning(
            "PUBLIC_URL, RENDER_EXTERNAL_URL, or RAILWAY_PUBLIC_DOMAIN is not set; "
            "webhook not configured"
        )
        return False


def _webhook_refresh_loop():
    while not _webhook_refresh_stop.wait(WEBHOOK_REFRESH_INTERVAL_SECONDS):
        set_webhook()


def _start_webhook_refresh():
    global _webhook_refresh_thread
    if not PUBLIC_URL:
        return
    _webhook_refresh_thread = threading.Thread(
        target=_webhook_refresh_loop,
        name="telegram-webhook-refresh",
        daemon=True,
    )
    _webhook_refresh_thread.start()


init_db()
install_error_log_handler()
set_webhook()
atexit.register(_stop_background_services)
_ensure_ready()
_start_webhook_refresh()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=PORT)

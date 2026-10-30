import asyncio
import html
import io
import logging
import math
from decimal import Decimal, InvalidOperation

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, Forbidden
from telegram.ext import ContextTypes

from config import AUTO_PUBLISH_CHANNEL_LINK
from core.db import (
    delete_user_chat_ids,
    get_activity_stats,
    get_admin_cart_rows,
    get_auto_publish_report,
    get_error_logs,
    get_channel_queue_status,
    get_user_chat_ids,
    get_total_user_count,
    insert_cart_row,
    is_feature_enabled,
    set_feature_enabled,
    update_cart_row,
)
from core.ai import (
    analyze_error_logs,
    get_ai_service_status,
    set_ai_provider_enabled,
)
from core.ai_service import CHANNEL_PROVIDER_ORDER, USER_PROVIDER_ORDER
from handlers.admin_publishing import (
    begin_manual_publish,
    handle_manual_publish_callback,
    handle_manual_publish_message,
    manual_publish_channel_id,
    publishing_methods_keyboard,
)
from handlers.ai_search import (
    AI_SEARCH_BUTTON_TEXT,
    _activate_ai_search,
    _clear_ai_search_state,
    _hide_saved_offers_reply_keyboard,
    ai_search_reply_keyboard,
)

logger = logging.getLogger(__name__)
# This adds a disabled UI placeholder only; Cloudflare remains outside
# CHANNEL_PROVIDER_ORDER so it can never enter the channel analysis route.
_CHANNEL_CONTROL_PROVIDER_ORDER = (*CHANNEL_PROVIDER_ORDER, "Cloudflare")

ADMIN_CALLBACK_PATTERN = (
    r"^(?:"
    r"admin_service_(?:(?:channel|user)_)?"
    r"(?:gemini|groq|cloudflare|openrouter|deepseek)_"
    r"(?:enable|disable)"
    r"|admin_service_details"
    r"|admin_broadcast_(?:confirm|cancel)"
    r"|admin_cleanup_(?:confirm|cancel)"
    r"|admin_(?:menu|cart|ai|smart|publish|service|queue|activity|broadcast|cleanup|logs|tools|users)"
    r"(?:_(?:enable|disable|report|today|month|previous|week|hour|quarter|"
    r"manual|auto|addbutton|send|back))?"
    r"|admin_logs_file_(?:hour|quarter)"
    r"|admin_(?:edit|add|exit)(?:_[0-9]+)?"
    r")$"
)

ADMIN_ACCESS_CODE = "Rabah06651024221997"
ADMIN_CONTACT_TEXT = "تواصل مع الادمن @Rabahbelksier"
ADMIN_ROW_TEMPLATE = "id | link | pricecart | pricefinalcart | stor | ship"
TELEGRAM_MESSAGE_LIMIT = 4096


def _split_text(text, limit=TELEGRAM_MESSAGE_LIMIT):
    """Split text into Telegram-safe chunks without cutting a line when possible."""
    text = str(text or "")
    if not text:
        return [""]
    if limit <= 0:
        raise ValueError("The message limit must be positive.")

    chunks = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break

        split_at = remaining.rfind("\n", 0, limit + 1)
        if split_at <= 0:
            split_at = remaining.rfind(" ", 0, limit + 1)
        if split_at <= 0:
            split_at = limit

        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:]
        if remaining.startswith("\n") or remaining.startswith(" "):
            remaining = remaining[1:]
    return chunks


def _split_report_sections(sections, limit=TELEGRAM_MESSAGE_LIMIT):
    """Split report sections while keeping each section out of its neighbor."""
    chunks = []
    for section in sections:
        chunks.extend(_split_text(section, limit=limit))
    return chunks or [""]


def _format_value(value):
    if value is None:
        return "-"
    if isinstance(value, Decimal):
        text = format(value, "f")
        text = text.rstrip("0").rstrip(".")
        return text or "0"
    return str(value)


def format_cart_row(row):
    """Format one row using HTML so the fixed `link` label is clickable and blue."""
    url = str(row.get("linkcart") or "").strip()
    escaped_url = html.escape(url, quote=True)
    escaped_visible_url = html.escape(url)
    link_value = f'<a href="{escaped_url}">link</a>'
    if not url:
        link_value = "link"
        escaped_visible_url = "-"

    values = [
        _format_value(row.get("id")),
        link_value,
        _format_value(row.get("pricecart")),
        _format_value(row.get("pricefinalecart")),
        _format_value(row.get("stor")),
        _format_value(row.get("ship")),
    ]
    row_line = (
        f"{values[0]} | {values[1]} | {values[2]} | {values[3]} | "
        f"{values[4]} | {values[5]}"
    )
    url_line = (
        f'<a href="{escaped_url}">{escaped_visible_url}</a>'
        if url
        else escaped_visible_url
    )
    return f"{row_line}\n{url_line}"


def admin_actions_keyboard(row_ids=()):
    rows = [
        [
            InlineKeyboardButton(
                "تعديل",
                callback_data=f"admin_edit_{row_id}",
            ),
        ]
        for row_id in row_ids
    ]
    rows.extend([
        [InlineKeyboardButton("إضافة", callback_data="admin_add")],
        [InlineKeyboardButton("العودة إلى القائمة", callback_data="admin_menu")],
    ])
    return InlineKeyboardMarkup(rows)


def admin_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("المنتجات الإضافية", callback_data="admin_cart")],
        [InlineKeyboardButton("الأدوات", callback_data="admin_tools")],
        [InlineKeyboardButton("المستخدمين", callback_data="admin_users")],
        [InlineKeyboardButton("تقارير الأخطاء", callback_data="admin_logs")],
        [InlineKeyboardButton("خروج", callback_data="admin_exit")],
    ])


def admin_tools_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("أداة الذكاء الاصطناعي", callback_data="admin_ai")],
        [InlineKeyboardButton("AI Service", callback_data="admin_service")],
        [InlineKeyboardButton("تراكم منشورات Telegram", callback_data="admin_queue")],
        [InlineKeyboardButton("أداة السلة الذكية", callback_data="admin_smart")],
        [InlineKeyboardButton("النشر في القناة", callback_data="admin_publish")],
        [InlineKeyboardButton("العودة للقائمة", callback_data="admin_menu")],
    ])


def admin_users_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("نشاط المستخدمين", callback_data="admin_activity")],
        [InlineKeyboardButton("إشعار المستخدمين", callback_data="admin_broadcast")],
        [InlineKeyboardButton("تنظيف المستخدمين", callback_data="admin_cleanup")],
        [InlineKeyboardButton("العودة للقائمة", callback_data="admin_menu")],
    ])


def admin_back_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("العودة للقائمة", callback_data="admin_menu")
    ]])


def admin_tools_back_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("العودة إلى قائمة الأدوات", callback_data="admin_tools")
    ]])


def admin_broadcast_confirmation_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "تأكيد الإرسال",
            callback_data="admin_broadcast_confirm",
        ),
        InlineKeyboardButton(
            "إلغاء",
            callback_data="admin_broadcast_cancel",
        ),
    ]])


def admin_cleanup_confirmation_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "تأكيد التنظيف",
            callback_data="admin_cleanup_confirm",
        ),
        InlineKeyboardButton(
            "إلغاء",
            callback_data="admin_cleanup_cancel",
        ),
    ]])


def ai_service_keyboard(providers):
    rows = []
    provider_ids = {
        "Gemini": "gemini",
        "Groq": "groq",
        "Cloudflare": "cloudflare",
        "OpenRouter": "openrouter",
        "DeepSeek": "deepseek",
    }
    provider_by_name = {
        provider["name"]: provider
        for provider in providers
    }

    def toggle_button(provider_name, request_kind, priority):
        provider = provider_by_name.get(provider_name, {})
        enabled_by_kind = provider.get("enabled_by_request_kind") or {}
        enabled = enabled_by_kind.get(
            request_kind,
            provider.get("enabled", True),
        )
        action = "disable" if enabled else "enable"
        icon = "✅" if enabled else "⛔"
        provider_id = provider_ids[provider_name]
        return InlineKeyboardButton(
            f"{icon} {priority}. {provider_name}",
            callback_data=(
                f"admin_service_{request_kind}_{provider_id}_{action}"
            ),
        )

    row_count = max(
        len(_CHANNEL_CONTROL_PROVIDER_ORDER),
        len(USER_PROVIDER_ORDER),
    )
    for row_index in range(row_count):
        row = []
        if row_index < len(_CHANNEL_CONTROL_PROVIDER_ORDER):
            channel_provider = _CHANNEL_CONTROL_PROVIDER_ORDER[row_index]
            if channel_provider == "Cloudflare":
                row.append(InlineKeyboardButton(
                    "⛔ Cloudflare (غير متاح للمنشورات)",
                    callback_data="admin_service_channel_cloudflare_disable",
                ))
            else:
                row.append(
                    toggle_button(
                        channel_provider,
                        "channel",
                        row_index + 1,
                    )
                )
        if row_index < len(USER_PROVIDER_ORDER):
            row.append(
                toggle_button(
                    USER_PROVIDER_ORDER[row_index],
                    "user",
                    row_index + 1,
                )
            )
        rows.append(row)
    rows.append([
        InlineKeyboardButton(
            "عرض تفاصيل AI Service",
            callback_data="admin_service_details",
        ),
    ])
    rows.append([
        InlineKeyboardButton(
            "العودة إلى قائمة الأدوات",
            callback_data="admin_tools",
        )
    ])
    return InlineKeyboardMarkup(rows)


def ai_service_details_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "العودة إلى تحكم AI Service",
            callback_data="admin_service",
        )],
        [InlineKeyboardButton(
            "العودة إلى قائمة الأدوات",
            callback_data="admin_tools",
        )],
    ])


def activity_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("اليوم", callback_data="admin_activity_today"),
            InlineKeyboardButton("هذا الشهر", callback_data="admin_activity_month"),
        ],
        [InlineKeyboardButton("الشهر الماضي", callback_data="admin_activity_previous")],
        [InlineKeyboardButton("العودة للقائمة", callback_data="admin_menu")],
    ])


def logs_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("logs اليوم", callback_data="admin_logs_today"),
            InlineKeyboardButton("logs هذا الأسبوع", callback_data="admin_logs_week"),
        ],
        [
            InlineKeyboardButton(
                "logs آخر ساعة",
                callback_data="admin_logs_file_hour",
            ),
            InlineKeyboardButton(
                "logs آخر ربع ساعة",
                callback_data="admin_logs_file_quarter",
            ),
        ],
        [InlineKeyboardButton("العودة للقائمة", callback_data="admin_menu")],
    ])


_LOG_PERIOD_LABELS = {
    "today": "اليوم",
    "week": "هذا الأسبوع",
    "hour": "آخر ساعة",
    "quarter": "آخر ربع ساعة",
}
_LOG_PERIOD_WINDOWS = {
    "today": "آخر 24 ساعة",
    "week": "آخر 7 أيام",
    "hour": "آخر 60 دقيقة",
    "quarter": "آخر 15 دقيقة",
}
_LOG_FILE_LABELS = {
    "today": "today",
    "week": "week",
    "hour": "last-hour",
    "quarter": "last-15-minutes",
}


def short_logs_report_keyboard(period):
    label = _LOG_PERIOD_LABELS[period]
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                f"تحميل logs {label}",
                callback_data=f"admin_logs_file_{period}",
            ),
        ],
        [
            InlineKeyboardButton("العودة إلى تقارير الأخطاء", callback_data="admin_logs"),
            InlineKeyboardButton("العودة للقائمة", callback_data="admin_menu"),
        ],
    ])


def feature_toggle_keyboard(feature_key, enabled):
    action = "disable" if enabled else "enable"
    label = "تعطيل" if enabled else "تفعيل"
    callback_prefixes = {
        "ai_search": "ai",
        "smart_cart": "smart",
        "auto_publish": "publish",
    }
    callback_prefix = callback_prefixes[feature_key]
    rows = [
        [InlineKeyboardButton(label, callback_data=f"admin_{callback_prefix}_{action}")],
    ]
    if feature_key == "auto_publish":
        rows.append([
            InlineKeyboardButton(
                "تحديث الإحصاءات",
                callback_data="admin_publish_report",
            ),
        ])
        rows.append([
            InlineKeyboardButton(
                "العودة إلى خيارات النشر",
                callback_data="admin_publish",
            ),
        ])
    rows.append([InlineKeyboardButton(
        "العودة إلى قائمة الأدوات",
        callback_data="admin_tools",
    )])
    return InlineKeyboardMarkup(rows)


async def _render_admin_message(
    message,
    text,
    reply_markup=None,
    *,
    edit=False,
    parse_mode=None,
    disable_web_page_preview=None,
):
    chunks = _split_text(text)
    kwargs = {"reply_markup": reply_markup}
    if parse_mode is not None:
        kwargs["parse_mode"] = parse_mode
    if disable_web_page_preview is not None:
        kwargs["disable_web_page_preview"] = disable_web_page_preview

    async def send_follow_up_chunks():
        for chunk in chunks[1:]:
            try:
                await message.reply_text(
                    chunk,
                    reply_markup=None,
                    **{
                        key: value
                        for key, value in kwargs.items()
                        if key != "reply_markup"
                    },
                )
            except Exception:
                logger.exception("Could not send a follow-up admin report chunk")
                break

    if edit:
        try:
            await message.edit_text(chunks[0], **kwargs)
        except Exception as exc:
            if "not modified" in str(exc).lower():
                await send_follow_up_chunks()
                return
            logger.warning(
                "Could not edit admin message; sending a replacement",
                exc_info=True,
            )
            try:
                await message.reply_text(chunks[0], **kwargs)
            except Exception:
                logger.exception("Could not send replacement admin report")
                return
    else:
        try:
            await message.reply_text(chunks[0], **kwargs)
        except Exception:
            logger.exception("Could not send admin report")
            return
    await send_follow_up_chunks()


async def _render_admin_report_sections(
    message,
    sections,
    reply_markup=None,
    *,
    edit=False,
):
    """Render a report with one or more independent, Telegram-safe sections."""
    chunks = _split_report_sections(sections)
    if len(chunks) == 1:
        await _render_admin_message(
            message,
            chunks[0],
            reply_markup=reply_markup,
            edit=edit,
        )
        return

    first = chunks[0]
    if edit:
        try:
            await message.edit_text(first, reply_markup=reply_markup)
        except Exception as exc:
            if "not modified" not in str(exc).lower():
                logger.warning(
                    "Could not edit the first admin report chunk; "
                    "sending a replacement",
                    exc_info=True,
                )
                try:
                    await message.reply_text(
                        first,
                        reply_markup=reply_markup,
                    )
                except Exception:
                    logger.exception("Could not send replacement admin report")
                    return
    else:
        try:
            await message.reply_text(first, reply_markup=reply_markup)
        except Exception:
            logger.exception("Could not send admin report")
            return

    for chunk in chunks[1:]:
        try:
            await message.reply_text(chunk, reply_markup=None)
        except Exception:
            logger.exception("Could not send a follow-up admin report chunk")
            break


async def send_admin_menu(message, *, edit=False):
    await _render_admin_message(
        message,
        "اضغط على الزر الموافق لطلبك:",
        reply_markup=admin_menu_keyboard(),
        edit=edit,
    )


async def send_admin_tools_menu(message, *, edit=False):
    await _render_admin_message(
        message,
        "اختر الأداة التي تريد إدارتها:",
        reply_markup=admin_tools_keyboard(),
        edit=edit,
    )


async def send_admin_publish_menu(message, *, edit=False):
    text = "اختر طريقة النشر:"
    reply_markup = publishing_methods_keyboard()
    if edit and getattr(message, "photo", None):
        try:
            await message.edit_reply_markup(reply_markup=None)
        except Exception:
            logger.debug("Could not remove the preview keyboard", exc_info=True)
        await message.reply_text(text, reply_markup=reply_markup)
        return
    await _render_admin_message(
        message,
        text,
        reply_markup=reply_markup,
        edit=edit,
    )


async def send_admin_users_menu(message, *, edit=False):
    try:
        user_count = await asyncio.to_thread(get_total_user_count)
        count_text = f"{user_count:,}"
    except Exception:
        logger.exception("Could not read total bot users for admin menu")
        count_text = "غير متاح"

    await _render_admin_message(
        message,
        "اختر وظيفة المستخدمين التي تريد فتحها:\n"
        f"\nإجمالي مستخدمي البوت: {count_text}",
        reply_markup=admin_users_keyboard(),
        edit=edit,
    )


async def send_feature_status(message, feature_key, *, edit=False):
    enabled = await asyncio.to_thread(is_feature_enabled, feature_key)
    title = {
        "ai_search": "أداة الذكاء الاصطناعي",
        "smart_cart": "أداة السلة الذكية",
        "auto_publish": "النشر التلقائي",
    }.get(feature_key, feature_key)
    if enabled:
        text = f"{title} مفعلة قم بتعطيلها إن أردت ذلك:"
    else:
        text = f"{title} معطلة قم بتفعيلها إن أردت ذلك:"
    if feature_key == "auto_publish":
        channel_text = (
            AUTO_PUBLISH_CHANNEL_LINK
            if AUTO_PUBLISH_CHANNEL_LINK
            else "غير مضبوط"
        )
        report = await asyncio.to_thread(get_auto_publish_report)
        text += (
            f"\nالقناة: {channel_text}"
            "\n\nالمنشورات الآلية الناجحة:\n"
            f"اليوم: {report['published_today']}\n"
            f"أمس: {report['published_yesterday']}\n"
            f"هذا الشهر: {report['published_this_month']}\n"
            f"الشهر الماضي: {report['published_last_month']}\n"
            f"الإجمالي: {report['published_total']}\n"
            "\nحالة المحاولات:\n"
            f"المحاولات الفاشلة: {report['failed_today']}\n"
            f"المحاولات المعلقة: {report['claimed']}"
        )
    await _render_admin_message(
        message,
        text,
        reply_markup=feature_toggle_keyboard(feature_key, enabled),
        edit=edit,
    )


async def send_ai_service_control_status(message, *, edit=False):
    status = await asyncio.to_thread(get_ai_service_status)
    active_providers = status.get("active_providers") or {
        "channel": None,
        "user": status.get("active_provider"),
    }
    user_provider = active_providers.get("user")
    channel_provider = active_providers.get("channel")
    user_mode = (
        "الخدمة المحلية لطلبات المستخدمين فقط"
        if user_provider == "Local service" or status.get("mode") == "local"
        else "خدمات الذكاء الاصطناعي الخارجية"
    )
    await _render_admin_message(
        message,
        "حالة AI Service:\n"
        "الأزرار: معالجة المنشورات (يسار) | طلبات المستخدمين (يمين)\n"
        f"المزود الحالي لمعالجة المنشورات: "
        f"{channel_provider or 'لا يوجد مزود متاح'}\n"
        f"المزود الحالي لطلبات المستخدمين: "
        f"{user_provider or 'لا يوجد مزود متاح'}\n"
        f"وضع طلبات المستخدمين: {user_mode}",
        reply_markup=ai_service_keyboard(status["providers"]),
        edit=edit,
    )


async def send_ai_service_status(message, *, edit=False):
    status = await asyncio.to_thread(
        get_ai_service_status,
        request_kind="channel",
    )
    provider_lines = []
    display_names = {
        "Gemini": "Gemini Flash-Lite",
        "Groq": "Groq — Qwen 3.8-27B",
        "Cloudflare": "Cloudflare Workers AI",
        "OpenRouter": "OpenRouter — Free Models",
        "DeepSeek": "DeepSeek V4.1-Flash",
    }
    for provider in status["providers"]:
        limits = {
            limit.get("metric"): limit
            for limit in (provider.get("limits") or [])
        }
        icon = (
            "🟢"
            if (
                provider.get("enabled", True)
                and provider.get("configured", False)
                and provider.get("available", False)
            )
            else "🔴"
        )
        lines = [f"{icon} {display_names.get(provider['name'], provider['name'])}"]
        if provider["name"] == "DeepSeek":
            usage = provider.get("usage") or {}
            lines.extend([
                "• RPD: "
                f"{_format_ai_observed_count(usage.get('observed_requests_day'))}",
                "• TPD: "
                f"{_format_ai_observed_count(usage.get('observed_tokens_day'))}",
            ])
        else:
            lines.extend(
                f"• {_format_ai_usage_line(metric, limits.get(metric))}"
                for metric in _AI_SERVICE_DISPLAY_METRICS.get(
                    provider["name"],
                    (),
                )
            )
        lines.append(
            f"• {_format_ai_reset_line(provider, status.get('active_provider'))}"
        )
        provider_lines.append("\n".join(lines))

    await _render_admin_message(
        message,
        "\n\n".join(provider_lines),
        reply_markup=ai_service_details_keyboard(),
        edit=edit,
    )


def _format_queue_age(seconds):
    return "لا يوجد" if seconds is None else _format_ai_duration(seconds)


def _format_channel_queue_status(status):
    oldest_processing = status.get("oldest_processing_seconds")
    stalled = (
        oldest_processing is not None
        and oldest_processing >= 10 * 60
    )
    alert = (
        "\n\n⚠️ تنبيه: توجد منشورات في processing منذ مدة غير طبيعية."
        if stalled
        else ""
    )
    return (
        "حالة معالجة منشورات Telegram:\n"
        f"pending: {status.get('pending', 0)}\n"
        f"processing: {status.get('processing', 0)}\n"
        f"processed: {status.get('processed', 0)}\n\n"
        f"عمر أقدم pending: {_format_queue_age(status.get('oldest_pending_seconds'))}\n"
        f"عمر أقدم processing: {_format_queue_age(oldest_processing)}"
        f"{alert}"
    )


async def send_channel_queue_status(message, *, edit=False):
    status = await asyncio.to_thread(get_channel_queue_status)
    await _render_admin_message(
        message,
        _format_channel_queue_status(status),
        reply_markup=admin_tools_back_keyboard(),
        edit=edit,
    )


_AI_SERVICE_DISPLAY_METRICS = {
    "Gemini": ("rpm", "rpd", "tpm", "tpd"),
    "Groq": ("rpm", "rpd", "tpm", "tpd"),
    "Cloudflare": ("npd",),
    "OpenRouter": ("rpm", "rpd", "tpm", "tpd"),
}


def _format_ai_usage_line(metric, limit):
    labels = {
        "rpm": "RPM",
        "rpd": "RPD",
        "tpm": "TPM",
        "tpd": "TPD",
        "npd": "Neurons",
    }
    limit = limit or {}
    used = _format_ai_quota_number(limit.get("used", 0), metric)
    maximum = limit.get("limit")
    if maximum is None:
        maximum_text = ""
    else:
        approximate = (
            "≈"
            if (
                not limit.get("exact", False)
                or limit.get("source") in {"estimated", "observed"}
            )
            else ""
        )
        maximum_text = approximate + _format_ai_quota_number(
            maximum,
            metric,
        )
    return f"{labels.get(metric, metric.upper())}: {used}/{maximum_text}"


def _format_ai_observed_count(value):
    try:
        return f"{max(0, int(value or 0)):,}"
    except (TypeError, ValueError, OverflowError):
        return "0"


def _format_ai_reset_line(provider, active_provider=None):
    seconds = provider.get("next_reset_seconds")
    if provider.get("quota_exhausted") and seconds is not None:
        seconds = max(0, float(seconds))
        if seconds <= 0:
            return "⏱️ العودة: الآن"
        return f"⏱️ العودة: بعد {_format_ai_duration(seconds)}"
    if provider.get("name") == active_provider or provider.get("active"):
        return "⏱️ الحالة: نشط"
    if (
        provider.get("available")
        and provider.get("configured")
        and provider.get("enabled", True)
    ):
        return "⏱️ الحالة: جاهز"
    return "⏱️ الحالة: غير جاهز"


def _format_ai_duration(seconds):
    seconds = max(1, math.ceil(seconds))
    if seconds < 60:
        return f"{seconds} ثا"
    minutes = math.ceil(seconds / 60)
    if minutes < 60:
        return f"{minutes} د"
    hours = math.ceil(minutes / 60)
    if hours < 24:
        return f"{hours} ساعة" if hours == 1 else f"{hours} ساعات"
    days = math.ceil(hours / 24)
    return f"{days} يوم" if days == 1 else f"{days} أيام"


def _format_ai_time(value):
    if not value:
        return "غير معروف"
    return str(value).replace("T", " ").replace("+00:00", " UTC")


def _format_ai_number(value):
    if value is None:
        return "غير متاح"
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return f"{value:,}"


def _format_ai_quota_number(value, metric):
    if value in (None, ""):
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number.is_integer():
        number = int(number)
    if metric in {"tpm", "tpd", "npd"} and abs(number) >= 1000:
        scaled = number / 1000
        if float(scaled).is_integer():
            return f"{int(scaled):,}K"
        return f"{scaled:,.1f}K"
    return f"{number:,}" if isinstance(number, int) else f"{number:,.1f}"


def _format_ai_limit(limit):
    metric = str(limit.get("metric") or "unknown").upper()
    maximum = _format_ai_number(limit.get("limit"))
    used = _format_ai_number(limit.get("used"))
    remaining = _format_ai_number(limit.get("remaining"))
    source = (
        "من المزود"
        if limit.get("source") == "provider" and limit.get("exact")
        else "حد موثق والمتبقي محلي"
        if limit.get("source") == "documented"
        else "تقديري"
    )
    reset = _format_ai_time(limit.get("reset_at"))
    return (
        f"{metric}: الحد {maximum} | المستهلك {used} | "
        f"المتبقي {remaining} | {source} | الاستعادة {reset}"
    )


def _format_ai_limit_or_missing(metric, limits):
    limit = limits.get(metric)
    if not limit:
        return f"{metric.upper()}: غير معلن من المزود"
    return _format_ai_limit(limit)


async def _sync_channel_monitor(context, enabled):
    if not context.application:
        return
    monitor = context.application.bot_data.get("channel_monitor")
    if not monitor:
        return
    try:
        if enabled:
            await monitor.start()
        else:
            await monitor.stop()
    except Exception:
        logger.exception("Could not sync channel monitor with AI feature state")


async def send_admin_cart(message, *, edit=False):
    """Render the cart and all of its actions in one message."""
    rows = await asyncio.to_thread(get_admin_cart_rows)
    text, keyboard = _admin_cart_payload(rows)
    await _render_admin_message(
        message,
        text,
        reply_markup=keyboard,
        edit=edit,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


def _admin_cart_payload(rows, notice=None):
    if not rows:
        text = "لا توجد أسطر حاليا في جدول cart."
    else:
        text = "جدول cart:\n\n" + "\n\n".join(
            format_cart_row(row)
            for row in rows
        )
    if notice:
        text = f"{notice}\n\n{text}"
    return text, admin_actions_keyboard(row["id"] for row in rows)


async def _edit_admin_cart_message(bot, state, notice=None):
    rows = await asyncio.to_thread(get_admin_cart_rows)
    text, keyboard = _admin_cart_payload(rows, notice=notice)
    await bot.edit_message_text(
        chat_id=state["chat_id"],
        message_id=state["message_id"],
        text=text,
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=keyboard,
    )


async def send_activity_menu(message, *, edit=False):
    await _render_admin_message(
        message,
        "اختر الفترة التي تريد حساب نشاط المستخدمين فيها:",
        reply_markup=activity_keyboard(),
        edit=edit,
    )


async def send_activity_report(message, period, *, edit=False):
    labels = {
        "today": "اليوم",
        "month": "هذا الشهر",
        "previous": "الشهر الماضي",
    }
    stats = await asyncio.to_thread(get_activity_stats, period)
    await _render_admin_message(
        message,
        f"إحصائيات {labels[period]}:\n"
        f"عدد الطلبات: {stats['requests']}\n"
        f"عدد المستخدمين النشطين: {stats['users']}",
        reply_markup=activity_keyboard(),
        edit=edit,
    )


def _format_error_log_file(logs):
    if not logs:
        return "لا توجد أخطاء مسجلة في هذه الفترة.\n"
    return "\n\n".join(
        f"[{item['created_at']}] {item['level']} {item['logger_name']}\n"
        f"{item['message']}"
        for item in logs
    ) + "\n"


def _format_error_period_report(period, logs, analysis):
    label = _LOG_PERIOD_LABELS[period]
    window = _LOG_PERIOD_WINDOWS[period]
    return (
        f"━━ تقرير أخطاء {label} ━━\n"
        f"الفترة: {window}\n"
        f"عدد الأخطاء: {len(logs)}\n\n"
        f"{analysis}"
    )


def _compact_error_analysis(analysis, limit=600):
    """Keep one period's summary readable while fitting four periods in one message."""
    analysis = str(analysis or "").strip()
    if len(analysis) <= limit:
        return analysis
    lines = []
    current_length = 0
    for line in analysis.splitlines():
        line = line.rstrip()
        if not line:
            continue
        extra_length = len(line) + (1 if lines else 0)
        if current_length + extra_length > limit - 24:
            break
        lines.append(line)
        current_length += extra_length
    compact = "\n".join(lines).strip()
    return (compact or analysis[:limit - 1].rstrip()) + "\n…"


async def send_logs_report(message, period=None, *, edit=False):
    if period:
        logs = await asyncio.to_thread(get_error_logs, period)
        label = _LOG_PERIOD_LABELS[period]
        analysis = await asyncio.to_thread(analyze_error_logs, logs, label)
        await _render_admin_message(
            message,
            _format_error_period_report(period, logs, analysis),
            reply_markup=(
                short_logs_report_keyboard(period)
                if period in {"hour", "quarter"}
                else logs_keyboard()
            ),
            edit=edit,
        )
        return
    periods = ("today", "week", "hour", "quarter")
    logs_values = await asyncio.gather(*(
        asyncio.to_thread(get_error_logs, period)
        for period in periods
    ))
    logs_by_period = dict(zip(periods, logs_values))
    analysis_values = await asyncio.gather(*(
        asyncio.to_thread(
            analyze_error_logs,
            logs_by_period[period],
            _LOG_PERIOD_LABELS[period],
        )
        for period in periods
    ))
    analysis_by_period = dict(zip(periods, analysis_values))
    reports = [
        _format_error_period_report(
            period,
            logs_by_period[period],
            _compact_error_analysis(analysis_by_period[period]),
        )
        for period in periods
    ]
    report = (
        "تقرير أخطاء الخادم\n\n"
        + "\n\n".join(reports)
        + "\n\nاستخدم أزرار logs لتحميل الفترة كاملة:"
    )
    # The per-period cap keeps this specific report in one Telegram message.
    await _render_admin_message(
        message,
        report[:TELEGRAM_MESSAGE_LIMIT],
        reply_markup=logs_keyboard(),
        edit=edit,
    )


async def send_logs_file(message, period):
    logs = await asyncio.to_thread(get_error_logs, period)
    file_label = _LOG_FILE_LABELS[period]
    label = _LOG_PERIOD_LABELS[period]
    payload = _format_error_log_file(logs).encode("utf-8")
    try:
        await message.delete()
    except Exception:
        logger.debug("Could not replace logs report with file", exc_info=True)
    await message.reply_document(
        document=io.BytesIO(payload),
        filename=f"server-logs-{file_label}.txt",
        caption=f"ملف logs {label}",
        reply_markup=logs_keyboard(),
    )


def _parse_cart_line(text):
    parts = [part.strip() for part in text.split("|")]
    if len(parts) != 6:
        raise ValueError(
            "أرسل السطر بهذا الشكل:\n"
            f"{ADMIN_ROW_TEMPLATE}\n"
        )

    if not parts[0]:
        raise ValueError("قيمة id مطلوبة.")
    try:
        row_id = int(parts[0])
    except ValueError as exc:
        raise ValueError("قيمة id يجب أن تكون رقما صحيحا.") from exc
    if row_id <= 0:
        raise ValueError("قيمة id يجب أن تكون أكبر من صفر.")

    if not parts[1]:
        raise ValueError("الرابط مطلوب.")

    numeric_values = []
    for label, raw_value in zip(
        ("pricecart", "pricefinalcart", "stor", "ship"),
        parts[2:],
    ):
        try:
            value = Decimal(raw_value)
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"قيمة {label} يجب أن تكون رقما.") from exc
        if not value.is_finite() or value < 0:
            raise ValueError(f"قيمة {label} يجب أن تكون رقما موجبا أو صفرا.")
        numeric_values.append(value)

    if numeric_values[0] == 0:
        raise ValueError("قيمة pricecart يجب أن تكون أكبر من صفر.")

    return {
        "id": row_id,
        "link": parts[1],
        "price": numeric_values[0],
        "final_price": numeric_values[1],
        "stor": numeric_values[2],
        "shipping": numeric_values[3],
    }


async def _send_admin_input_prompt(message, action, *, edit=False):
    operation = "تعديل" if action == "edit" else "إضافة"
    await _render_admin_message(
        message,
        f"أرسل سطر {operation} كاملا بهذا القالب:\n"
        f"{ADMIN_ROW_TEMPLATE}\n"
        ,
        reply_markup=admin_back_keyboard(),
        edit=edit,
    )


async def _send_broadcast_preview(message, text, photo):
    reply_markup = admin_broadcast_confirmation_keyboard()
    copy_message = getattr(message, "copy", None)
    if copy_message is not None:
        await copy_message(
            chat_id=message.chat.id,
            reply_markup=reply_markup,
        )
        return

    if photo:
        await message.reply_photo(
            photo=photo,
            caption=text or None,
            reply_markup=reply_markup,
        )
    else:
        await message.reply_text(text, reply_markup=reply_markup)


async def _send_admin_broadcast(bot, text, photo):
    sent_count = 0
    failed_count = 0
    for chat_id in await asyncio.to_thread(get_user_chat_ids):
        try:
            if photo:
                caption = text[:1024]
                await bot.send_photo(
                    chat_id=chat_id,
                    photo=photo,
                    caption=caption or None,
                )
                if len(text) > 1024:
                    await bot.send_message(
                        chat_id=chat_id,
                        text=text[1024:],
                    )
            else:
                for start in range(0, len(text), 4096):
                    await bot.send_message(
                        chat_id=chat_id,
                        text=text[start:start + 4096],
                    )
            sent_count += 1
        except Exception:
            failed_count += 1
            logger.exception("Could not send admin broadcast to %s", chat_id)
    return sent_count, failed_count


def _is_inaccessible_user_error(error):
    """Return True only for Telegram errors proving the chat is unavailable."""
    if isinstance(error, Forbidden):
        return True
    if not isinstance(error, BadRequest):
        return False

    error_text = str(error).lower()
    return any(
        marker in error_text
        for marker in (
            "chat not found",
            "user not found",
            "peer_id_invalid",
            "chat_id is empty",
        )
    )


async def _scan_inaccessible_users(bot):
    chat_ids = await asyncio.to_thread(get_user_chat_ids)
    inaccessible_ids = []
    verification_failures = 0

    for chat_id in chat_ids:
        try:
            await bot.get_chat(chat_id=chat_id)
        except Exception as error:
            if _is_inaccessible_user_error(error):
                inaccessible_ids.append(chat_id)
            else:
                verification_failures += 1
                logger.warning(
                    "Could not verify user %s during cleanup",
                    chat_id,
                    exc_info=True,
                )

    return {
        "total_count": len(chat_ids),
        "inaccessible_ids": inaccessible_ids,
        "verification_failures": verification_failures,
    }


def _cleanup_report_text(result):
    total_count = result.get("total_count", 0)
    inaccessible_count = len(result.get("inaccessible_ids", ()))
    remaining_count = max(total_count - inaccessible_count, 0)
    verification_failures = result.get("verification_failures", 0)
    text = (
        "معلومات تنظيف المستخدمين:\n"
        f"العدد الكلي: {total_count}\n"
        f"عدد المستخدمين غير القابلين للوصول: {inaccessible_count}\n"
        f"العدد بعد التنظيف: {remaining_count}"
    )
    if verification_failures:
        text += (
            "\n"
            f"تعذر التحقق من: {verification_failures} مستخدم، "
            "ولم يتم ترشيحهم للحذف."
        )
    return text


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    supplied_code = " ".join(context.args).strip()
    if supplied_code != ADMIN_ACCESS_CODE:
        await update.message.reply_text(ADMIN_CONTACT_TEXT)
        return

    await _hide_saved_offers_reply_keyboard(update.message, context)
    _clear_ai_search_state(context)
    context.user_data["admin_mode"] = True
    context.user_data.pop("admin_state", None)
    await send_admin_menu(update.message)


async def handle_admin_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text only while the current user is in an admin session."""
    if not context.user_data.get("admin_mode"):
        return False

    message = update.message
    if (message.text or "").strip() == AI_SEARCH_BUTTON_TEXT:
        context.user_data.pop("admin_mode", None)
        context.user_data.pop("admin_state", None)
        _clear_ai_search_state(context)
        await _activate_ai_search(message, context)
        return True

    state = context.user_data.get("admin_state")
    if not state:
        await message.reply_text(
            "أنت في وضع الإدارة. استخدم زر تعديل أو إضافة أو اضغط العودة إلى القائمة."
        )
        return True

    if await handle_manual_publish_message(update, context):
        return True

    if state.get("action") == "broadcast":
        text = (message.text or message.caption or "").strip()
        photo = None
        if message.photo:
            photo = message.photo[-1].file_id
        if not text and not photo:
            await message.reply_text(
                "أرسل نص الإشعار، أو أرسل صورة مع نص اختياري في الوصف."
            )
            return True

        context.user_data["admin_state"] = {
            "action": "broadcast_pending",
            "text": text,
            "photo": photo,
        }
        await _send_broadcast_preview(message, text, photo)
        return True

    if state.get("action") == "broadcast_pending":
        await message.reply_text(
            "راجع معاينة الإشعار ثم اضغط تأكيد الإرسال أو إلغاء."
        )
        return True

    try:
        parsed = _parse_cart_line(message.text)
        if state["action"] == "edit":
            if parsed["id"] != state["row_id"]:
                raise ValueError(
                    f"يجب أن يبقى id مساويا للسطر الذي اخترته ({state['row_id']})."
                )
            await asyncio.to_thread(
                update_cart_row,
                parsed["id"],
                parsed["link"],
                parsed["price"],
                parsed["final_price"],
                parsed["stor"],
                parsed["shipping"],
            )
            success_text = "تم تعديل السطر وحفظه في قاعدة البيانات."
        else:
            await asyncio.to_thread(
                insert_cart_row,
                parsed["id"],
                parsed["link"],
                parsed["price"],
                parsed["final_price"],
                parsed["stor"],
                parsed["shipping"],
            )
            success_text = "تمت إضافة السطر وحفظه في قاعدة البيانات."
    except ValueError as exc:
        await message.reply_text(f"⚠️ {exc}")
        return True
    except Exception:
        logger.exception("Failed to save admin cart row")
        await message.reply_text(
            "❌ تعذر حفظ السطر. تأكد من أن id غير مكرر عند الإضافة ثم حاول مجددا."
        )
        return True

    context.user_data.pop("admin_state", None)
    try:
        if state.get("message_id"):
            await _edit_admin_cart_message(
                context.bot,
                state,
                notice=success_text,
            )
        else:
            await message.reply_text(success_text)
            await send_admin_cart(message)
    except Exception:
        logger.exception("Failed to refresh cart after admin mutation")
        await message.reply_text(
            "تم الحفظ، لكن تعذر إعادة عرض جدول cart حاليا."
        )
    return True


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    action_parts = query.data.split("_")
    action = action_parts[1] if len(action_parts) > 1 else ""

    if not context.user_data.get("admin_mode"):
        await query.answer("انتهت جلسة الإدارة، أرسل الأمر من جديد.", show_alert=True)
        return

    if query.data in {
        "admin_service_channel_cloudflare_enable",
        "admin_service_channel_cloudflare_disable",
    }:
        await query.answer(
            "Cloudflare غير متاح لمعالجة المنشورات.",
            show_alert=True,
        )
        return

    if query.data in {"admin_publish_addbutton", "admin_publish_send"}:
        await handle_manual_publish_callback(query, context)
        return

    if query.data == "admin_broadcast_cancel":
        await query.answer()
        context.user_data.pop("admin_state", None)
        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except Exception:
            logger.debug(
                "Could not remove broadcast preview keyboard",
                exc_info=True,
            )
        await send_admin_menu(query.message, edit=True)
        return

    if query.data == "admin_broadcast_confirm":
        state = context.user_data.get("admin_state")
        if not state or state.get("action") != "broadcast_pending":
            await query.answer(
                "لا يوجد إشعار معلّق للإرسال.",
                show_alert=True,
            )
            return

        await query.answer()
        context.user_data["admin_state"] = {
            **state,
            "action": "broadcast_sending",
        }
        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except Exception:
            logger.debug(
                "Could not remove broadcast preview keyboard",
                exc_info=True,
            )
        sent_count, failed_count = await _send_admin_broadcast(
            context.bot,
            state.get("text", ""),
            state.get("photo"),
        )
        context.user_data.pop("admin_state", None)
        await query.message.reply_text(
            f"تم إرسال الإشعار إلى {sent_count} مستخدم، وتعذر الإرسال إلى "
            f"{failed_count} مستخدم.",
            reply_markup=admin_back_keyboard(),
        )
        return

    if query.data == "admin_cleanup_cancel":
        state = context.user_data.get("admin_state")
        if not state or state.get("action") != "cleanup_pending":
            await query.answer(
                "لا يوجد تنظيف معلّق.",
                show_alert=True,
            )
            return

        await query.answer()
        context.user_data.pop("admin_state", None)
        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except Exception:
            logger.debug(
                "Could not remove cleanup confirmation keyboard",
                exc_info=True,
            )
        await send_admin_users_menu(query.message, edit=True)
        return

    if query.data == "admin_cleanup_confirm":
        state = context.user_data.get("admin_state")
        if not state or state.get("action") != "cleanup_pending":
            await query.answer(
                "لا يوجد تنظيف معلّق.",
                show_alert=True,
            )
            return

        await query.answer()
        context.user_data["admin_state"] = {
            **state,
            "action": "cleanup_deleting",
        }
        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except Exception:
            logger.debug(
                "Could not remove cleanup confirmation keyboard",
                exc_info=True,
            )

        try:
            deleted_count = await asyncio.to_thread(
                delete_user_chat_ids,
                state.get("inaccessible_ids", []),
            )
            remaining_count = len(
                await asyncio.to_thread(get_user_chat_ids)
            )
        except Exception:
            logger.exception("Failed to clean inaccessible users")
            context.user_data.pop("admin_state", None)
            await query.message.reply_text(
                "❌ تعذر تنفيذ تنظيف المستخدمين حاليا. لم يتم اعتماد العملية.",
                reply_markup=admin_users_keyboard(),
            )
            return

        context.user_data.pop("admin_state", None)
        await query.message.reply_text(
            f"تم تنظيف المستخدمين بنجاح.\n"
            f"عدد المستخدمين المحذوفين: {deleted_count}\n"
            f"عدد المستخدمين المتبقين: {remaining_count}",
            reply_markup=admin_users_keyboard(),
        )
        return

    await query.answer()
    if action == "menu":
        context.user_data.pop("admin_state", None)
        await send_admin_menu(query.message, edit=True)
        return

    if action == "cart":
        try:
            await send_admin_cart(query.message, edit=True)
        except Exception:
            logger.exception("Failed to load cart for admin")
            await _render_admin_message(
                query.message,
                "تعذر استخراج محتوى جدول cart حاليا، حاول مرة أخرى.",
                edit=True,
            )
        return

    if action == "tools":
        await send_admin_tools_menu(query.message, edit=True)
        return

    if action == "users":
        context.user_data.pop("admin_state", None)
        await send_admin_users_menu(query.message, edit=True)
        return

    if action == "activity":
        if len(action_parts) == 2:
            await send_activity_menu(query.message, edit=True)
        else:
            period = {
                "today": "today",
                "month": "month",
                "previous": "previous",
            }.get(action_parts[2])
            if period:
                await send_activity_report(query.message, period, edit=True)
            else:
                await _render_admin_message(
                    query.message,
                    "الفترة المطلوبة غير صالحة.",
                    edit=True,
                )
        return

    if action == "broadcast":
        context.user_data["admin_state"] = {"action": "broadcast"}
        await _render_admin_message(
            query.message,
            "أرسل الآن نص الإشعار أو صورة مع نص اختياري. "
            "يمكن أن يحتوي النص على عدة أسطر.",
            reply_markup=admin_back_keyboard(),
            edit=True,
        )
        return

    if action == "cleanup":
        context.user_data["admin_state"] = {"action": "cleanup_scanning"}
        await _render_admin_message(
            query.message,
            "جاري استخراج معلومات المستخدمين⏳",
            edit=True,
        )
        try:
            result = await _scan_inaccessible_users(context.bot)
        except Exception:
            logger.exception("Failed to scan users for cleanup")
            context.user_data.pop("admin_state", None)
            await _render_admin_message(
                query.message,
                "❌ تعذر استخراج معلومات المستخدمين حاليا، حاول مرة أخرى.",
                reply_markup=admin_users_keyboard(),
                edit=True,
            )
            return

        context.user_data["admin_state"] = {
            "action": "cleanup_pending",
            "total_count": result["total_count"],
            "inaccessible_ids": result["inaccessible_ids"],
            "verification_failures": result["verification_failures"],
        }
        await _render_admin_message(
            query.message,
            _cleanup_report_text(result),
            reply_markup=admin_cleanup_confirmation_keyboard(),
            edit=True,
        )
        return

    if action == "logs":
        if len(action_parts) == 2:
            await send_logs_report(query.message, edit=True)
        elif len(action_parts) == 3 and action_parts[2] in {
            "today",
            "week",
            "hour",
            "quarter",
        }:
            await send_logs_file(query.message, action_parts[2])
        elif (
            len(action_parts) == 4
            and action_parts[2] == "file"
            and action_parts[3] in {"hour", "quarter"}
        ):
            await send_logs_file(query.message, action_parts[3])
        return

    if action == "service":
        if len(action_parts) == 3:
            if action_parts[2] != "details":
                await _render_admin_message(
                    query.message,
                    "إجراء غير صالح.",
                    edit=True,
                )
                return
            await send_ai_service_status(query.message, edit=True)
            return
        if len(action_parts) in {4, 5}:
            if len(action_parts) == 5 and action_parts[2] in {
                "channel",
                "user",
            }:
                request_profiles = (action_parts[2],)
                provider_id = action_parts[3]
                requested_state = action_parts[4]
            elif len(action_parts) == 4:
                # Previously displayed keyboards changed both tasks together.
                # Keep already-sent Telegram buttons safe after this update.
                request_profiles = ("channel", "user")
                provider_id = action_parts[2]
                requested_state = action_parts[3]
            else:
                request_profiles = ()
                provider_id = None
                requested_state = None
            provider_name = {
                "gemini": "Gemini",
                "groq": "Groq",
                "cloudflare": "Cloudflare",
                "openrouter": "OpenRouter",
                "deepseek": "DeepSeek",
            }.get(provider_id)
            if not request_profiles or not provider_name or requested_state not in {
                "enable",
                "disable",
            }:
                await _render_admin_message(
                    query.message,
                    "إجراء غير صالح.",
                    edit=True,
                )
                return
            if provider_name == "Cloudflare" and "channel" in request_profiles:
                request_profiles = tuple(
                    profile
                    for profile in request_profiles
                    if profile != "channel"
                )
            try:
                for request_profile in request_profiles:
                    await asyncio.to_thread(
                        set_ai_provider_enabled,
                        provider_name,
                        requested_state == "enable",
                        request_profile,
                    )
                if requested_state == "enable":
                    monitor = context.application.bot_data.get(
                        "channel_monitor"
                    )
                    if monitor:
                        monitor.wake_retry_loop()
            except Exception:
                logger.exception(
                    "Failed to update AI provider %s",
                    provider_name,
                )
                await _render_admin_message(
                    query.message,
                    "تعذر حفظ حالة مزود AI في قاعدة البيانات، حاول مرة أخرى.",
                    edit=True,
                )
                return
        await send_ai_service_control_status(query.message, edit=True)
        return

    if action == "queue":
        await send_channel_queue_status(query.message, edit=True)
        return

    if action == "publish":
        if len(action_parts) == 2:
            context.user_data.pop("admin_state", None)
            await send_admin_publish_menu(query.message, edit=True)
            return

        publish_action = action_parts[2]
        if publish_action == "manual":
            if not manual_publish_channel_id():
                await _render_admin_message(
                    query.message,
                    "رابط قناة النشر غير مضبوط أو غير صالح. "
                    "تحقق من إعداد القناة ثم حاول مجددًا.",
                    reply_markup=publishing_methods_keyboard(),
                    edit=True,
                )
                return
            try:
                await begin_manual_publish(query.message, context)
            except Exception:
                context.user_data.pop("admin_state", None)
                logger.exception("Could not open the manual publishing prompt")
                await _render_admin_message(
                    query.message,
                    "تعذر فتح النشر اليدوي حاليا. حاول مرة أخرى.",
                    reply_markup=publishing_methods_keyboard(),
                    edit=True,
                )
            return

        if publish_action in {"auto", "report"}:
            await send_feature_status(
                query.message,
                "auto_publish",
                edit=True,
            )
            return

        if publish_action == "back":
            context.user_data.pop("admin_state", None)
            await send_admin_publish_menu(query.message, edit=True)
            return

    feature_key = {
        "ai": "ai_search",
        "smart": "smart_cart",
        "publish": "auto_publish",
    }.get(action)
    if feature_key:
        if len(action_parts) == 2:
            await send_feature_status(query.message, feature_key, edit=True)
            return
        requested_state = action_parts[2]
        if requested_state not in {"enable", "disable"}:
            await _render_admin_message(
                query.message,
                "إجراء غير صالح.",
                edit=True,
            )
            return
        try:
            await asyncio.to_thread(
                set_feature_enabled,
                feature_key,
                requested_state == "enable",
            )
            if feature_key == "ai_search":
                await _sync_channel_monitor(
                    context,
                    requested_state == "enable",
                )
            await send_feature_status(query.message, feature_key, edit=True)
        except Exception:
            logger.exception("Failed to update feature %s", feature_key)
            await _render_admin_message(
                query.message,
                "تعذر حفظ حالة الأداة في قاعدة البيانات، حاول مرة أخرى.",
                edit=True,
            )
        return

    if action == "exit":
        context.user_data.pop("admin_mode", None)
        context.user_data.pop("admin_state", None)
        _clear_ai_search_state(context)
        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except Exception:
            logger.debug("Could not remove admin action keyboard", exc_info=True)
        await query.message.reply_text(
            "تم الخروج من وضع الإدارة.",
            reply_markup=ai_search_reply_keyboard(),
        )
        return

    if action == "add":
        context.user_data["admin_state"] = {
            "action": "add",
            "chat_id": query.message.chat.id,
            "message_id": query.message.message_id,
        }
        await _send_admin_input_prompt(query.message, "add", edit=True)
        return

    if action == "edit" and len(action_parts) == 3:
        try:
            row_id = int(action_parts[2])
        except ValueError:
            await _render_admin_message(
                query.message,
                "رقم السطر غير صالح.",
                edit=True,
            )
            return
        context.user_data["admin_state"] = {
            "action": "edit",
            "row_id": row_id,
            "chat_id": query.message.chat.id,
            "message_id": query.message.message_id,
        }
        await _send_admin_input_prompt(query.message, "edit", edit=True)

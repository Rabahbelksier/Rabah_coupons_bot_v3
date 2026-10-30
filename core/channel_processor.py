import asyncio
import logging
import re
import time

from core.ai import (
    ChannelBatchResponseError,
    analyze_channel_posts,
    classify_product_category_from_title,
)
from core.categories import (
    SPECIAL_CATEGORY_BLUETOOTH_HEADPHONES,
    SPECIAL_CATEGORY_SMART_WATCHES,
    canonical_saved_offer_subcategory_name,
    official_names_for_internal_category,
)
from core.product import get_product_category_from_api
from core.affiliate import generate_affiliate_link
from core.db import (
    create_channel_post,
    delete_channel_post_if_status,
    is_feature_enabled,
    mark_channel_post_pending,
    refresh_ai_keyword_cache_from_title,
    update_channel_post,
)
from config import CHANNEL_TELEGRAM_REPLACEMENT_LINK
from services.ai_priority import ai_request_priority
from utils.parser import extract_aliexpress_urls, extract_product_id

logger = logging.getLogger(__name__)


class ChannelProcessResult(list):
    """List-compatible batch result carrying retry information for the monitor."""

    def __init__(self, values=(), retryable_failure=False):
        super().__init__(values)
        self.retryable_failure = bool(retryable_failure)


def _has_valid_api_category(api_category):
    if not isinstance(api_category, dict):
        return False
    if official_names_for_internal_category(api_category.get("category")):
        return True
    second_level = str(
        api_category.get("second_level_category_name") or ""
    ).strip().casefold()
    return bool(
        second_level
        and second_level not in {
            "غير محدد",
            "undefined",
            "unknown",
            "null",
            "none",
        }
    )


def _resolved_category_fields(analysis, api_category, title_category=None):
    """Keep official taxonomy separate from the bot's internal category."""
    api_category = api_category if isinstance(api_category, dict) else {}
    api_is_valid = _has_valid_api_category(api_category)
    if isinstance(title_category, dict):
        title_subcategory = canonical_saved_offer_subcategory_name(
            title_category.get("second_level_category_name")
        )
        if (
            official_names_for_internal_category(
                title_category.get("category")
            )
            or title_subcategory
        ):
            title_category = {
                **title_category,
                "second_level_category_name": title_subcategory,
            }
        else:
            title_category = {}
    else:
        title_category = {}
    analysis_subcategory = canonical_saved_offer_subcategory_name(
        analysis.get("second_level_category_name")
    )
    analysis_category = analysis.get("category")
    special_category = (
        analysis_category
        if analysis_category
        in {
            SPECIAL_CATEGORY_SMART_WATCHES,
            SPECIAL_CATEGORY_BLUETOOTH_HEADPHONES,
        }
        else None
    )
    api_category_name = api_category.get("category")
    api_category_value = str(api_category_name or "").casefold().strip()
    if api_is_valid and not (
        api_category_value == "other"
        or official_names_for_internal_category(api_category_value)
    ):
        # AliExpress can provide an exact second-level category that does not
        # belong to one of the bot's broad internal categories. Keep that API
        # result authoritative instead of replacing it with an AI guess.
        api_category_value = "other"
    special_api_compatible = {
        SPECIAL_CATEGORY_SMART_WATCHES: {"watches"},
        SPECIAL_CATEGORY_BLUETOOTH_HEADPHONES: {"headphones"},
    }
    if (
        special_category
        and api_is_valid
        and api_category_value != "other"
        and api_category_value
        not in special_api_compatible[special_category]
    ):
        special_category = None
    category = (
        special_category
        or (
            api_category_value
            if api_is_valid
            else None
        )
        or title_category.get("category")
        or analysis_category
    )
    official_names = official_names_for_internal_category(category) or {}
    official_subcategory = canonical_saved_offer_subcategory_name(
        official_names.get("second_level_category_name")
    )

    if special_category:
        provider = getattr(analysis, "ai_provider", None)
        normalized_provider = str(provider or "").strip().casefold()
        source = normalized_provider or "ai"
    elif api_is_valid:
        source = "aliexpress_api"
    elif title_category:
        provider = title_category.get("provider")
        source = str(provider or "ai").strip().casefold()
    else:
        provider = getattr(analysis, "ai_provider", None)
        normalized_provider = str(provider or "").strip().casefold()
        source = normalized_provider or ("ai" if category else "unclassified")

    return {
        "category": category,
        "first_level_category_name": (
            (api_category.get("first_level_category_name") if api_is_valid else None)
            or title_category.get("first_level_category_name")
            or analysis.get("first_level_category_name")
            or official_names.get("first_level_category_name")
        ),
        "second_level_category_name": (
            (api_category.get("second_level_category_name") if api_is_valid else None)
            or title_category.get("second_level_category_name")
            or analysis_subcategory
            or official_subcategory
        ),
        "category_source": source,
    }


def _analysis_source(analysis):
    provider = getattr(analysis, "ai_provider", None)
    normalized_provider = str(provider or "").strip().casefold()
    return normalized_provider or None


_TELEGRAM_LINK_PATTERN = re.compile(
    r"https?://(?:t\.me|telegram\.me)/[^\s<>()]+",
    re.IGNORECASE,
)

_CHANNEL_TEMPLATE_FOOTER = (
    "📎رابط تطبيق العروض Offres365👇\n"
    "https://offres365page.up.railway.app/\n"
    "📎لاستعمال طريقة السلة ارسل الرابط الى البوت👇\n"
    "https://t.me/rabahcopons/7366"
)
_BUNDLE_CHANNEL_HEADER = (
    "غـــيـــر الجديــــــــد في عــروض البــانـدل 3 قطع😍"
)
_BUNDLE_CHANNEL_FREE_SHIPPING = "✈️شحن مجاني"
_BUNDLE_CHANNEL_COUNTRY = "🚩ضع اعدادات الموقع على دولة الجزائر🇩🇿"


def rewrite_post_text(text, affiliate_links):
    """Replace Telegram and AliExpress links without changing other text."""
    rewritten = _TELEGRAM_LINK_PATTERN.sub(
        CHANNEL_TELEGRAM_REPLACEMENT_LINK,
        text or "",
    )
    for original, replacement in affiliate_links.items():
        rewritten = rewritten.replace(original, replacement)
    return rewritten


def build_source_link(channel_username, message_id, channel_id=None):
    if not channel_username:
        if channel_id and str(channel_id).startswith("-100"):
            return f"https://t.me/c/{str(channel_id)[4:]}/{message_id}"
        return None
    return f"https://t.me/{channel_username.lstrip('@')}/{message_id}"


def _get_photo_file_id(message):
    """Read a Bot API file_id when the source message provides one.

    Telethon messages expose the source photo but do not expose a Bot API
    file_id. In that case the id is intentionally left empty and is cached
    after the first successful Bot API upload during delivery.
    """
    direct_file_id = getattr(message, "photo_file_id", None)
    if direct_file_id:
        return direct_file_id

    photo_sizes = getattr(message, "photo", None)
    if isinstance(photo_sizes, (list, tuple)) and photo_sizes:
        return getattr(photo_sizes[-1], "file_id", None)
    return None


async def _generate_affiliate_replacements(urls):
    semaphore = asyncio.Semaphore(3)

    async def generate(url):
        async with semaphore:
            return url, await asyncio.to_thread(generate_affiliate_link, url)

    generated = await asyncio.gather(*(generate(url) for url in urls))
    failed = [url for url, replacement in generated if not replacement]
    if failed:
        raise RuntimeError(f"Affiliate link generation failed for {len(failed)} URL(s)")
    return dict(generated)


def _lines_for_numbers(source_lines, line_numbers):
    return [
        source_lines[line_number - 1]
        for line_number in line_numbers
        if 1 <= line_number <= len(source_lines)
    ]


def _fallback_link_sections(source_lines, urls):
    sections = []
    used_lines = set()
    for url in urls:
        url_line_index = next(
            (
                index
                for index, line in enumerate(source_lines)
                if url in line
            ),
            None,
        )
        if url_line_index is None:
            continue

        line_indexes = [url_line_index]
        description_index = url_line_index - 1
        if (
            description_index >= 0
            and source_lines[description_index].strip()
            and not extract_aliexpress_urls(source_lines[description_index])
            and description_index not in used_lines
        ):
            line_indexes.insert(0, description_index)
        used_lines.update(line_indexes)
        sections.append([source_lines[index] for index in line_indexes])
    return sections


def _build_link_sections(text, analysis, urls):
    source_lines = (text or "").splitlines()
    sections = []
    included_urls = set()

    for section in analysis.get("link_sections", []):
        line_numbers = section.get("line_numbers", [])
        lines = _lines_for_numbers(source_lines, line_numbers)
        section_urls = extract_aliexpress_urls("\n".join(lines))
        if not section_urls:
            continue
        sections.append(lines)
        included_urls.update(section_urls)

    missing_urls = [url for url in urls if url not in included_urls]
    if missing_urls:
        fallback_sections = _fallback_link_sections(source_lines, missing_urls)
        sections.extend(fallback_sections)

    if not sections:
        return _fallback_link_sections(source_lines, urls)
    return sections


def _build_channel_content(text, analysis, urls, replacements):
    source_lines = (text or "").splitlines()
    coupon_lines = _lines_for_numbers(
        source_lines,
        analysis.get("coupon_line_numbers", []),
    )
    shipping_lines = (
        _lines_for_numbers(
            source_lines,
            analysis.get("shipping_line_numbers", []),
        )
        if analysis.get("shipping_country")
        else []
    )
    link_sections = _build_link_sections(text, analysis, urls)

    dynamic_coupon_text = rewrite_post_text(
        "\n".join(coupon_lines),
        replacements,
    ).strip()
    dynamic_shipping_text = rewrite_post_text(
        "\n".join(shipping_lines),
        replacements,
    ).strip()
    dynamic_link_sections = [
        rewrite_post_text("\n".join(lines), replacements).strip()
        for lines in link_sections
    ]
    dynamic_link_sections = [
        section for section in dynamic_link_sections if section
    ]

    title = analysis.get("title") or ""
    price = analysis.get("price")
    price_text = str(price) if price is not None else ""
    currency = analysis.get("currency") or ""
    price_display = " ".join(
        part for part in (price_text, str(currency).strip()) if part
    )
    blocks = [
        "🛒ضع في السلة العرض يبدأ يوم الأحد",
    ]
    if dynamic_shipping_text:
        blocks.append(dynamic_shipping_text)
    blocks.extend([
        f"🔥 تخفيض على  {title}",
        f"💶 السعر: [ {price_display} ] ✈️باحتساب رسوم الشحن",
    ])
    if dynamic_coupon_text:
        blocks.append(dynamic_coupon_text)
    if dynamic_link_sections:
        blocks.append("\n\n".join(dynamic_link_sections))
    blocks.append(_CHANNEL_TEMPLATE_FOOTER)
    return "\n\n".join(blocks)


def _build_bundle_channel_content(text, analysis, urls, replacements):
    """Render the dedicated three-piece bundle template."""
    source_lines = (text or "").splitlines()
    price_lines = _lines_for_numbers(
        source_lines,
        analysis.get("price_line_numbers", []),
    )
    coupon_lines = _lines_for_numbers(
        source_lines,
        analysis.get("coupon_line_numbers", []),
    )
    link_sections = _build_link_sections(text, analysis, urls)

    price_text = rewrite_post_text("\n".join(price_lines), replacements).strip()
    if not price_text:
        price = analysis.get("price")
        currency = analysis.get("currency") or ""
        price_text = " ".join(
            part for part in (str(price) if price is not None else "", currency)
            if part
        )
    coupon_text = rewrite_post_text(
        "\n".join(coupon_lines),
        replacements,
    ).strip()
    link_text = "\n\n".join(
        rewrite_post_text("\n".join(lines), replacements).strip()
        for lines in link_sections
        if rewrite_post_text("\n".join(lines), replacements).strip()
    )

    blocks = [
        _BUNDLE_CHANNEL_HEADER,
        f"🔥 تخفيض على {analysis.get('title') or ''}",
        price_text,
        _BUNDLE_CHANNEL_FREE_SHIPPING,
        coupon_text,
        _BUNDLE_CHANNEL_COUNTRY,
        link_text,
    ]
    return "\n\n".join(blocks)


async def prepare_channel_message(
    message,
    channel_id,
    channel_username=None,
    existing_post_id=None,
):
    """Validate and register one source message without calling an AI provider."""
    if not await asyncio.to_thread(is_feature_enabled, "ai_search"):
        logger.info("Skipping channel post because AI search is disabled")
        return None
    text = getattr(message, "message", None) or getattr(message, "raw_text", "") or ""
    message_id = getattr(message, "id", None)
    if not channel_id or not message_id:
        logger.warning("Ignoring channel post without source identifiers")
        return None

    urls = extract_aliexpress_urls(text)
    if not urls:
        logger.info(
            "Skipping channel post %s/%s because it has no AliExpress link",
            channel_id,
            message_id,
        )
        return None

    source_link = build_source_link(channel_username, message_id, channel_id)
    photo_file_id = _get_photo_file_id(message)
    if existing_post_id is not None:
        post_id = existing_post_id
        persisted = await asyncio.to_thread(
            update_channel_post,
            post_id,
            expected_status="pending",
            content=text,
            processing_status="processing",
            processing_started_at="CURRENT_TIMESTAMP",
            source_link=source_link,
            published_at=getattr(message, "date", None),
            photo_file_id=photo_file_id,
        )
        if not persisted:
            return None
    else:
        post_id = await asyncio.to_thread(
            create_channel_post,
            source_channel_id=channel_id,
            source_message_id=message_id,
            source_link=source_link,
            published_at=getattr(message, "date", None),
            photo_file_id=photo_file_id,
        )
        if post_id is None:
            logger.info(
                "Skipping already processed channel post %s/%s",
                channel_id,
                message_id,
            )
            return None
    return {
        "post_id": post_id,
        "message": message,
        "channel_id": channel_id,
        "channel_username": channel_username,
        "text": text,
        "urls": urls,
    }


async def register_channel_message(message, channel_id, channel_username=None):
    """Register a newly received source message in the pending queue."""
    if not await asyncio.to_thread(is_feature_enabled, "ai_search"):
        return None
    message_id = getattr(message, "id", None)
    if not channel_id or not message_id:
        logger.warning("Ignoring channel post without source identifiers")
        return None
    text = getattr(message, "message", None) or getattr(message, "raw_text", "") or ""
    if not extract_aliexpress_urls(text):
        logger.info(
            "Skipping new channel post %s/%s because it has no AliExpress link",
            channel_id,
            message_id,
        )
        return None
    return await asyncio.to_thread(
        create_channel_post,
        source_channel_id=channel_id,
        source_message_id=message_id,
        source_link=build_source_link(channel_username, message_id, channel_id),
        published_at=getattr(message, "date", None),
        photo_file_id=_get_photo_file_id(message),
    )


def _mark_channel_post_pending(candidate):
    return mark_channel_post_pending(candidate["post_id"])


async def _mark_channel_post_pending_async(candidate):
    await asyncio.to_thread(_mark_channel_post_pending, candidate)


async def _handle_channel_analysis_failure(candidate):
    # Analysis/provider/persistence failures are always retryable.  Deletion is
    # reserved for deterministic source preparation failures in the monitor.
    await _mark_channel_post_pending_async(candidate)


async def _apply_channel_analysis(candidate, analysis):
    """Apply one validated AI result using the existing channel workflow."""
    processing_started_at = time.perf_counter()
    try:
        if not analysis["is_offer"]:
            deleted = await asyncio.to_thread(
                delete_channel_post_if_status,
                candidate["post_id"],
                "processing",
            )
            if not deleted:
                raise RuntimeError(
                    "Non-offer channel post could not be deleted from processing"
                )
            logger.info(
                "Deleted non-offer channel post %s/%s",
                candidate["channel_id"],
                getattr(candidate["message"], "id", None),
            )
            return None

        replacements = await _generate_affiliate_replacements(candidate["urls"])
        if analysis.get("offer_type") == "bundle":
            modified_text = _build_bundle_channel_content(
                candidate["text"],
                analysis,
                candidate["urls"],
                replacements,
            )
        else:
            modified_text = _build_channel_content(
                candidate["text"],
                analysis,
                candidate["urls"],
                replacements,
            )
        product_id = (
            await asyncio.to_thread(extract_product_id, candidate["urls"][0])
            if candidate["urls"]
            else None
        )
        api_category = (
            await asyncio.to_thread(get_product_category_from_api, product_id)
            if product_id
            else None
        )
        title_category = None
        if not _has_valid_api_category(api_category):
            try:
                async with ai_request_priority.channel_request():
                    title_category = await asyncio.to_thread(
                        classify_product_category_from_title,
                        analysis.get("title"),
                        "channel",
                    )
            except Exception:
                logger.exception(
                    "Title category fallback failed for channel post %s",
                    candidate["post_id"],
                )
        category_fields = _resolved_category_fields(
            analysis,
            api_category,
            title_category,
        )
        analysis_source = _analysis_source(analysis)
        if analysis_source:
            category_fields["source_analize"] = analysis_source
        offer_type = (
            "bundle"
            if analysis.get("offer_type") == "bundle"
            else "normal"
        )
        persisted = await asyncio.to_thread(
            update_channel_post,
            candidate["post_id"],
            expected_status="processing",
            title=analysis["title"],
            price=analysis["price"],
            content=modified_text,
            processing_status="processed",
             processed_started_at="CURRENT_TIMESTAMP",
            offer_type=offer_type,
            aliexpress_product_id=product_id,
            shipping_country=analysis.get("shipping_country"),
            **category_fields,
        )
        if persisted is False:
            raise RuntimeError("Channel post persistence did not update the row")
        try:
            await asyncio.to_thread(
                refresh_ai_keyword_cache_from_title,
                analysis["title"],
            )
        except Exception:
            # Keyword caching is auxiliary to the already persisted offer. Keep
            # the offer processed and let a later source update rebuild the
            # provider snapshot instead of reprocessing the Telegram post.
            logger.exception(
                "Could not update AI keyword cache for channel post %s",
                candidate["post_id"],
            )
        logger.info(
            "Channel post processing completed total_ms=%.1f",
            (time.perf_counter() - processing_started_at) * 1000,
        )
        return candidate["post_id"]
    except Exception:
        # Keep the original Telegram text available for diagnosis/retry even
        # when AI or affiliate-link processing fails.
        await _mark_channel_post_pending_async(candidate)
        logger.exception(
            "Failed to process channel post %s/%s",
            candidate["channel_id"],
            getattr(candidate["message"], "id", None),
        )
        return None


async def process_channel_messages(candidates):
    """Analyze and persist exactly five prepared channel posts as one batch."""
    candidates = list(candidates or [])
    if len(candidates) != 5:
        raise ValueError("Channel AI batches must contain exactly five posts")

    analysis_started_at = time.perf_counter()
    logger.info("Channel AI analysis task started count=%d", len(candidates))
    try:
        async with ai_request_priority.channel_request():
            provider_started_at = time.perf_counter()
            logger.info(
                "Channel AI provider request starting count=%d priority_wait_ms=%.1f",
                len(candidates),
                (provider_started_at - analysis_started_at) * 1000,
            )
            analyses = await asyncio.to_thread(
                analyze_channel_posts,
                [
                    {
                        "post_id": candidate["post_id"],
                        "text": candidate["text"],
                    }
                    for candidate in candidates
                ],
                request_kind="channel",
            )
            provider_duration_ms = (
                time.perf_counter() - provider_started_at
            ) * 1000
        logger.info(
            "Channel post AI batch completed count=%d provider_ms=%.1f "
            "total_ms=%.1f",
            len(candidates),
            provider_duration_ms,
            (time.perf_counter() - analysis_started_at) * 1000,
        )
    except ChannelBatchResponseError:
        await asyncio.gather(*(
            _handle_channel_analysis_failure(candidate)
            for candidate in candidates
        ))
        logger.warning(
            "Channel AI batch response was unusable; requeued batch count=%d",
            len(candidates),
            exc_info=True,
        )
        return ChannelProcessResult(
            [],
            retryable_failure=True,
        )
    except Exception:
        # Provider, quota, and transport failures still apply to the whole
        # request: no item is marked processed without a valid analysis.
        await asyncio.gather(*(
            _handle_channel_analysis_failure(candidate)
            for candidate in candidates
        ))
        logger.warning(
            "AI analysis failed for channel batch count=%d; requeued",
            len(candidates),
            exc_info=True,
        )
        return ChannelProcessResult(
            [],
            retryable_failure=True,
        )

    analyses_by_id = {
        str(getattr(analysis, "channel_post_id", "")): analysis
        for analysis in analyses
        if getattr(analysis, "channel_post_id", None) is not None
    }
    if analyses_by_id:
        failed_candidates = [
            candidate
            for candidate in candidates
            if str(candidate["post_id"]) not in analyses_by_id
        ]
        if failed_candidates:
            await asyncio.gather(*(
                _handle_channel_analysis_failure(candidate)
                for candidate in failed_candidates
            ))
        successful_pairs = [
            (candidate, analyses_by_id[str(candidate["post_id"])])
            for candidate in candidates
            if str(candidate["post_id"]) in analyses_by_id
        ]
    elif len(analyses) == len(candidates):
        successful_pairs = list(zip(candidates, analyses))
    else:
        successful_pairs = []
        failed_candidates = list(candidates)
        await asyncio.gather(*(
            _handle_channel_analysis_failure(candidate)
            for candidate in failed_candidates
        ))
        logger.warning(
            "AI analysis returned an unexpected batch size; "
            "requeued affected rows"
        )

    results = await asyncio.gather(*(
        _apply_channel_analysis(candidate, analysis)
        for candidate, analysis in successful_pairs
    ))
    processed_ids = [
        post_id
        for post_id in results
        if post_id is not None
    ]
    return ChannelProcessResult(
        processed_ids,
        retryable_failure=len(processed_ids) != len(candidates),
    )
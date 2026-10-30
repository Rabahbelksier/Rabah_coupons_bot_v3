import logging
import threading

from cachetools import TTLCache

from config import TRACKING_ID
from core.api import prepare_api_params, send_api_request_with_retry
from core.categories import category_from_aliexpress_names

logger = logging.getLogger(__name__)
_full_product_cache = TTLCache(maxsize=500, ttl=3600)
_full_product_cache_lock = threading.Lock()


def _extract_product(product_data):
    product = (
        product_data.get("aliexpress_affiliate_productdetail_get_response", {})
        .get("resp_result", {})
        .get("result", {})
        .get("products", {})
        .get("product")
    )
    if not product:
        return None
    return product[0] if isinstance(product, list) else product


def parse_product_data(product_data):
    try:
        p = _extract_product(product_data)
        if not p:
            return {}
        sale_price = p.get('target_sale_price', p.get('app_sale_price', 'غير متوفر'))
        original_price = p.get('target_original_price', p.get('original_price', 'غير متوفر'))
        discount = p.get('target_discount', 'غير محسوب')
        if discount == 'غير محسوب' and original_price != 'غير متوفر' and sale_price != 'غير متوفر':
            try:
                original = float(str(original_price).replace('USD', '').replace('$', '').strip())
                sale = float(str(sale_price).replace('USD', '').replace('$', '').strip())
                if original > 0:
                    discount = f"{((original - sale) / original) * 100:.1f}%"
            except Exception:
                pass
        shop_url = p.get('shop_url', 'غير متوفر')
        if '/store/' in shop_url:
            try:
                store_id = shop_url.split('/store/')[1].split('/')[0].split('?')[0]
                shop_url = f"https://m.aliexpress.com/store/{store_id}?shopId={store_id}"
            except Exception:
                pass
        return {
            'product_title': p.get('product_title', 'غير متوفر'),
            'target_sale_price': f"{sale_price} USD",
            'target_original_price': f"{original_price} USD",
            'target_discount': discount,
            'lastest_volume': p.get('lastest_volume', 'غير متوفر'),
            'shop_name': p.get('shop_name', 'غير متوفر'),
            'evaluate_rate': p.get('evaluate_rate', 'غير متوفر'),
            'shop_url': shop_url,
            'first_level_category_name': p.get('first_level_category_name', 'غير محدد'),
            'second_level_category_name': p.get('second_level_category_name', 'غير محدد'),
            'first_level_category_id': p.get('first_level_category_id'),
            'second_level_category_id': p.get('second_level_category_id'),
            'commission_rate': p.get('commission_rate', 'غير محدد')
        }
    except Exception:
        return {}


def get_full_product_details_from_api(product_id, max_retries=3):
    """Return the same full details used by the Telegram details button."""
    cache_key = str(product_id)
    with _full_product_cache_lock:
        cached = _full_product_cache.get(cache_key)
    if cached is not None:
        return dict(cached)

    try:
        params = prepare_api_params("aliexpress.affiliate.productdetail.get", {
            "product_ids": product_id,
            "target_currency": "USD",
            "target_language": "EN",
            "country": "DZ",
            "tracking_id": TRACKING_ID,
        })
        data = send_api_request_with_retry(params, max_retries=max_retries)
        if "error_response" in data:
            logger.warning(
                "AliExpress product details failed for %s: %s",
                product_id,
                data["error_response"].get("msg", "unknown"),
            )
            return None
        details = parse_product_data(data)
    except Exception:
        logger.exception("Could not load full AliExpress details for %s", product_id)
        return None
    if not details:
        return None
    with _full_product_cache_lock:
        _full_product_cache[cache_key] = dict(details)
    return details


def get_product_category_from_api(product_id):
    details = get_full_product_details_from_api(product_id)
    if not details:
        return None
    first_level = details.get("first_level_category_name")
    second_level = details.get("second_level_category_name")
    if first_level in (None, "", "غير محدد") and second_level in (
        None,
        "",
        "غير محدد",
    ):
        return None
    return {
        "first_level_category_name": first_level,
        "second_level_category_name": second_level,
        "category": (
            category_from_aliexpress_names(first_level, second_level)
            or "other"
        ),
    }


def get_product_info_from_api(product_id):
    try:
        params = prepare_api_params('aliexpress.affiliate.productdetail.get', {
            'product_ids': product_id,
            'target_currency': 'USD',
            'target_language': 'EN',
            'country': 'DZ',
            'tracking_id': TRACKING_ID,
            'fields': 'product_title,product_main_image_url'
        })
        data = send_api_request_with_retry(params, max_retries=2)
        if 'error_response' in data:
            logger.error(f"API error in get_product_info_from_api: {data['error_response'].get('msg', 'unknown')}")
            return None
        product = (
            data.get('aliexpress_affiliate_productdetail_get_response', {})
                .get('resp_result', {})
                .get('result', {})
                .get('products', {})
                .get('product')
        )
        if not product:
            return None
        p = product[0] if isinstance(product, list) else product
        title = p.get('product_title', '')
        image_url = p.get('product_main_image_url', '')
        if image_url and image_url.startswith('//'):
            image_url = f"https:{image_url}"
        if not title and not image_url:
            return None
        return {
            'title': title.strip()[:255] if title else 'غير متوفر',
            'image_url': image_url if image_url else None
        }
    except Exception as e:
        logger.error(f"Error in get_product_info_from_api: {e}")
        return None

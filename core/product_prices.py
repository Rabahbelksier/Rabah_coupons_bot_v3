"""Shared AliExpress current-price lookup for the Smart Cart product flow."""

from config import TRACKING_ID
from core.api import prepare_api_params, send_api_request_with_retry
from core.coupons import parse_numeric_value


def fetch_current_prices(product_ids):
    """Fetch current USD prices for multiple products in one AliExpress call."""
    product_ids = list(dict.fromkeys(
        str(product_id).strip()
        for product_id in product_ids
        if product_id is not None and str(product_id).strip()
    ))
    if not product_ids:
        return {}

    params = prepare_api_params("aliexpress.affiliate.productdetail.get", {
        "product_ids": ",".join(product_ids),
        "target_currency": "USD",
        "target_language": "EN",
        "country": "DZ",
        "tracking_id": TRACKING_ID,
        "fields": (
            "product_id,product_title,product_main_image_url,"
            "target_sale_price,app_sale_price"
        ),
    })
    data = send_api_request_with_retry(params, max_retries=2)
    if not isinstance(data, dict) or "error_response" in data:
        return {}

    raw = (
        data.get("aliexpress_affiliate_productdetail_get_response", {})
        .get("resp_result", {}).get("result", {}).get("products", {}).get("product")
    )
    if not raw:
        return {}

    products = raw if isinstance(raw, list) else [raw]
    requested_ids = set(product_ids)
    prices = {}
    for product in products:
        if not isinstance(product, dict):
            continue
        returned_id = product.get("product_id", product.get("productId"))
        if returned_id is None and len(products) == 1 and len(product_ids) == 1:
            returned_id = product_ids[0]
        returned_id = str(returned_id) if returned_id is not None else None
        if returned_id not in requested_ids:
            continue

        raw_price = product.get("target_sale_price")
        if raw_price in (None, ""):
            raw_price = product.get("app_sale_price")
        price = parse_numeric_value(raw_price)
        if price is not None and price > 0:
            prices[returned_id] = price
    return prices


def fetch_current_price(product_id):
    prices = fetch_current_prices([product_id])
    return prices.get(str(product_id))

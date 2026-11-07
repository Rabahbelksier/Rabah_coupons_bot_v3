"""Background refresh of current USD prices for the Smart Cart products."""

import logging
from utils.log_context import traced_operation
import threading

from core.db import (
    apply_cart_price_refresh_results,
    get_cart_price_refresh_rows,
)
from core.coupons import parse_numeric_value
from core.product_prices import fetch_current_prices
from utils.parser import extract_product_id

logger = logging.getLogger(__name__)

CART_PRICE_REFRESH_INTERVAL_SECONDS = 5 * 60 * 60


@traced_operation("cart.price_refresh", new_trace=True)
def refresh_cart_prices():
    """Refresh every cart product price with one AliExpress API request."""
    rows = get_cart_price_refresh_rows()
    if not rows:
        logger.info("Cart price refresh skipped: no product links are configured")
        return {"updated_prices": 0, "marked_failed": 0}

    row_product_ids = {}
    product_ids = []
    for row in rows:
        try:
            product_id = extract_product_id(str(row["linkcart"]).strip())
        except Exception:
            logger.exception("Could not extract product id for cart row %s", row["id"])
            product_id = None
        if product_id:
            product_id = str(product_id)
            row_product_ids[row["id"]] = product_id
            product_ids.append(product_id)

    unique_product_ids = list(dict.fromkeys(product_ids))
    prices = {}
    if unique_product_ids:
        try:
            prices = fetch_current_prices(unique_product_ids)
        except Exception:
            logger.exception("AliExpress cart price batch request failed")

    successful_rows = []
    failed_rows = []
    for row in rows:
        product_id = row_product_ids.get(row["id"])
        price = prices.get(product_id) if product_id else None
        if price is None:
            failed_rows.append((row["id"], row["linkcart"]))
        else:
            saved_price = parse_numeric_value(row.get("pricecart"))
            price_changed = saved_price != price
            successful_rows.append(
                (row["id"], row["linkcart"], price, price_changed)
            )

    result = apply_cart_price_refresh_results(successful_rows, failed_rows)
    logger.info(
        "Cart price refresh finished: updated=%d failed=%d",
        result["updated_prices"],
        result["marked_failed"],
    )
    return result


class CartPriceUpdater:
    """Run the immediate refresh and five-hour refreshes in one daemon thread."""

    def __init__(self, interval_seconds=CART_PRICE_REFRESH_INTERVAL_SECONDS):
        self.interval_seconds = interval_seconds
        self._stop_event = threading.Event()
        self._start_lock = threading.Lock()
        self._thread = None

    def start(self):
        with self._start_lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="cart-price-updater",
                daemon=True,
            )
            self._thread.start()

    def stop(self):
        self._stop_event.set()

    def _run(self):
        while not self._stop_event.is_set():
            try:
                refresh_cart_prices()
            except Exception:
                logger.exception("Scheduled cart price refresh failed")
            if self._stop_event.wait(self.interval_seconds):
                break


cart_price_updater = CartPriceUpdater()

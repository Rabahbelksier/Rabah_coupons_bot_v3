import os
import unittest
from decimal import Decimal
from unittest.mock import patch

for name in ("APP_KEY", "APP_SECRET", "TRACKING_ID", "TELEGRAM_TOKEN"):
    os.environ.setdefault(name, "test")

from services.cart_price_updater import refresh_cart_prices


class CartPriceUpdaterTests(unittest.TestCase):
    @patch("services.cart_price_updater.apply_cart_price_refresh_results")
    @patch("services.cart_price_updater.fetch_current_prices")
    @patch("services.cart_price_updater.extract_product_id")
    @patch("services.cart_price_updater.get_cart_price_refresh_rows")
    def test_refresh_batches_unique_ids_and_marks_only_unpriced_rows(
        self,
        get_rows,
        extract_product_id,
        fetch_current_prices,
        apply_results,
    ):
        get_rows.return_value = [
            {"id": 1, "linkcart": "https://aliexpress.com/item/100.html"},
            {"id": 2, "linkcart": "https://aliexpress.com/item/100.html?copy=1"},
            {"id": 3, "linkcart": "https://aliexpress.com/item/200.html"},
        ]
        extract_product_id.side_effect = ["100", "100", "200"]
        fetch_current_prices.return_value = {"100": Decimal("14.25")}
        apply_results.return_value = {"updated_prices": 2, "marked_failed": 1}

        result = refresh_cart_prices()

        fetch_current_prices.assert_called_once_with(["100", "200"])
        apply_results.assert_called_once_with(
            [
                (1, "https://aliexpress.com/item/100.html", Decimal("14.25")),
                (2, "https://aliexpress.com/item/100.html?copy=1", Decimal("14.25")),
            ],
            [(3, "https://aliexpress.com/item/200.html")],
        )
        self.assertEqual(result, {"updated_prices": 2, "marked_failed": 1})

    @patch("services.cart_price_updater.apply_cart_price_refresh_results")
    @patch("services.cart_price_updater.fetch_current_prices")
    @patch("services.cart_price_updater.extract_product_id")
    @patch("services.cart_price_updater.get_cart_price_refresh_rows")
    def test_api_error_preserves_all_prices_and_marks_rows_failed(
        self,
        get_rows,
        extract_product_id,
        fetch_current_prices,
        apply_results,
    ):
        rows = [
            {"id": 7, "linkcart": "https://aliexpress.com/item/700.html"},
            {"id": 8, "linkcart": "https://aliexpress.com/item/800.html"},
        ]
        get_rows.return_value = rows
        extract_product_id.side_effect = ["700", "800"]
        fetch_current_prices.side_effect = RuntimeError("API unavailable")
        apply_results.return_value = {"updated_prices": 0, "marked_failed": 2}

        result = refresh_cart_prices()

        apply_results.assert_called_once_with(
            [],
            [(7, rows[0]["linkcart"]), (8, rows[1]["linkcart"])],
        )
        self.assertEqual(result["updated_prices"], 0)


if __name__ == "__main__":
    unittest.main()

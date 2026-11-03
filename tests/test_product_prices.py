import os
import unittest
from decimal import Decimal
from unittest.mock import patch

for name in ("APP_KEY", "APP_SECRET", "TRACKING_ID", "TELEGRAM_TOKEN"):
    os.environ.setdefault(name, "test")

from core.product_prices import fetch_current_prices


class ProductPriceLookupTests(unittest.TestCase):
    @patch("core.product_prices.send_api_request_with_retry")
    @patch("core.product_prices.prepare_api_params")
    def test_fetch_current_prices_batches_products_in_usd_for_algeria(
        self,
        prepare_api_params,
        send_api_request,
    ):
        prepare_api_params.return_value = {"method": "test"}
        send_api_request.return_value = {
            "aliexpress_affiliate_productdetail_get_response": {
                "resp_result": {
                    "result": {
                        "products": {
                            "product": [
                                {
                                    "product_id": "100",
                                    "target_sale_price": "12.34",
                                },
                                {
                                    "product_id": "200",
                                    "app_sale_price": "8.90",
                                },
                            ]
                        }
                    }
                }
            }
        }

        prices = fetch_current_prices(["100", "200", "100"])

        self.assertEqual(
            prices,
            {"100": Decimal("12.34"), "200": Decimal("8.90")},
        )
        prepare_api_params.assert_called_once()
        api_params = prepare_api_params.call_args.args[1]
        self.assertEqual(api_params["product_ids"], "100,200")
        self.assertIn("product_id", api_params["fields"])
        self.assertEqual(api_params["target_currency"], "USD")
        self.assertEqual(api_params["country"], "DZ")
        send_api_request.assert_called_once_with(
            {"method": "test"},
            max_retries=2,
        )

    @patch("core.product_prices.send_api_request_with_retry")
    @patch("core.product_prices.prepare_api_params")
    def test_fetch_current_prices_ignores_unmatched_products(
        self,
        prepare_api_params,
        send_api_request,
    ):
        prepare_api_params.return_value = {}
        send_api_request.return_value = {
            "aliexpress_affiliate_productdetail_get_response": {
                "resp_result": {
                    "result": {
                        "products": {
                            "product": {
                                "product_id": "other",
                                "target_sale_price": "1.00",
                            }
                        }
                    }
                }
            }
        }

        self.assertEqual(fetch_current_prices(["100", "200"]), {})


if __name__ == "__main__":
    unittest.main()

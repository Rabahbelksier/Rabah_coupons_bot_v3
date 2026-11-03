import os
import unittest
from decimal import Decimal
from unittest.mock import patch

# smart_cart imports the application configuration, which requires these
# values during module import. Tests use placeholders and never make API calls.
os.environ.setdefault("APP_KEY", "test")
os.environ.setdefault("APP_SECRET", "test")
os.environ.setdefault("TRACKING_ID", "test")
os.environ.setdefault("TELEGRAM_TOKEN", "123456:TEST")

from core.smart_cart import (
    _get_initial_offer_image_id,
    _get_saved_initial_offer_image,
    _offer_lines,
    calculate_offer,
    fetch_current_prices,
)


class SmartCartCalculationTests(unittest.TestCase):
    @patch("core.smart_cart.send_api_request_with_retry")
    @patch("core.smart_cart.prepare_api_params")
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
        self.assertEqual(api_params["target_currency"], "USD")
        self.assertEqual(api_params["country"], "DZ")
        send_api_request.assert_called_once_with(
            {"method": "test"},
            max_retries=2,
        )

    @patch("core.smart_cart.send_api_request_with_retry")
    @patch("core.smart_cart.prepare_api_params")
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

    def test_smart_cart_reuses_initial_offer_image_id(self):
        class Photo:
            file_id = "telegram-file-id"

        class Message:
            photo = [Photo()]

        self.assertEqual(
            _get_initial_offer_image_id(Message()),
            "telegram-file-id",
        )

    def test_smart_cart_has_no_image_id_when_initial_offer_is_text(self):
        class Message:
            photo = []

        self.assertIsNone(_get_initial_offer_image_id(Message()))

    def test_smart_cart_uses_saved_image_when_callback_has_no_photo(self):
        class Message:
            photo = []

        class Context:
            user_data = {
                "product_images": {
                    "123": "https://example.com/initial-offer-image.jpg"
                }
            }

        self.assertEqual(
            _get_saved_initial_offer_image(Context(), "123", Message()),
            "https://example.com/initial-offer-image.jpg",
        )

    def test_smart_cart_prefers_callback_file_id_over_saved_image(self):
        class Photo:
            file_id = "telegram-file-id"

        class Message:
            photo = [Photo()]

        class Context:
            user_data = {
                "product_images": {
                    "123": "https://example.com/initial-offer-image.jpg"
                }
            }

        self.assertEqual(
            _get_saved_initial_offer_image(Context(), "123", Message()),
            "telegram-file-id",
        )

    def test_total_after_discount_uses_the_full_coupon_value(self):
        result = calculate_offer(
            Decimal("195"),
            {"discount": Decimal("30")},
            [
                {
                    "product": {"final_price": Decimal("85")},
                    "quantity": 1,
                }
            ],
            Decimal("5"),
        )

        self.assertEqual(result["extra_final_total"], Decimal("85"))
        self.assertEqual(result["total_after_discount"], Decimal("255"))

    def test_extra_shipping_only_changes_cart_total(self):
        result = calculate_offer(
            Decimal("195"),
            {"discount": Decimal("30")},
            [
                {
                    "product": {
                        "final_price": Decimal("85"),
                        "shipping": Decimal("7.31"),
                    },
                    "quantity": 1,
                }
            ],
            Decimal("5"),
        )

        self.assertEqual(result["extra_shipping_total"], Decimal("7.31"))
        self.assertEqual(result["total_after_discount"], Decimal("262.31"))
        self.assertEqual(result["partial_coupon"], Decimal("20.89285714285714285714285714"))
        self.assertEqual(result["final_price"], Decimal("179.1071428571428571428571429"))

    def test_extra_shipping_respects_product_quantity(self):
        result = calculate_offer(
            Decimal("100"),
            {"discount": Decimal("10")},
            [
                {
                    "product": {
                        "final_price": Decimal("20"),
                        "shipping": Decimal("2.31"),
                    },
                    "quantity": 3,
                }
            ],
            Decimal("0"),
        )

        self.assertEqual(result["extra_shipping_total"], Decimal("6.93"))

    def test_extra_shipping_is_added_to_both_displayed_cart_totals(self):
        additions = [
            {
                "product": {
                    "final_price": Decimal("85"),
                    "shipping": Decimal("7.31"),
                    "link": "https://example.com/extra",
                },
                "quantity": 1,
            }
        ]
        state = {
            "current_price": Decimal("195"),
            "shipping": Decimal("5"),
            "coupon": {"label": "30/300$", "discount": Decimal("30")},
            "main_links": {},
        }
        offer = {
            "additions": additions,
            "added_original_total": Decimal("100"),
            "result": calculate_offer(
                Decimal("195"),
                state["coupon"],
                additions,
                state["shipping"],
            ),
        }

        lines = _offer_lines(state, offer, 1, 1)

        self.assertIn("📦 اجمالي أسعار المنتجات قبل التخفيض: 307.31$", lines)
        self.assertIn("📦 اجمالي أسعار المنتجات بعد التخفيض: 262.31$", lines)


if __name__ == "__main__":
    unittest.main()
import unittest
from unittest.mock import patch

from core.product import get_product_category_from_api


class ProductCategoryTests(unittest.TestCase):
    @patch("core.product.get_full_product_details_from_api")
    def test_extracts_official_aliexpress_category_names(self, get_details):
        get_details.return_value = {
            "first_level_category_name": "Phones & Telecommunications",
            "second_level_category_name": "Mobile Phones",
        }

        category = get_product_category_from_api("100500123")

        self.assertEqual(category["category"], "phones")
        self.assertEqual(
            category["first_level_category_name"],
            "Phones & Telecommunications",
        )

    @patch("core.product.get_full_product_details_from_api", return_value=None)
    def test_returns_none_when_aliexpress_api_fails(self, _get_details):
        self.assertIsNone(get_product_category_from_api("100500123"))

    @patch("core.product.get_full_product_details_from_api")
    def test_unknown_official_category_never_falls_back_to_ai(self, get_details):
        get_details.return_value = {
            "first_level_category_name": "Special Official Category",
            "second_level_category_name": "Special Products",
        }

        self.assertEqual(
            get_product_category_from_api("100500123")["category"],
            "other",
        )


if __name__ == "__main__":
    unittest.main()
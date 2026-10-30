import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from handlers.callbacks import product_details_callback


class ProductDetailsCallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_details_button_uses_shared_loader_and_formats_response(self):
        status_message = SimpleNamespace(edit_text=AsyncMock())
        query = SimpleNamespace(
            data="details_123",
            answer=AsyncMock(),
            message=SimpleNamespace(
                reply_text=AsyncMock(return_value=status_message),
            ),
        )
        info = {
            "target_sale_price": "100 USD",
            "shop_url": "غير متوفر",
            "first_level_category_name": "Phones & Telecommunications",
            "second_level_category_name": "Mobile Phones",
        }
        with (
            patch(
                "handlers.callbacks.remove_pressed_button",
                new=AsyncMock(),
            ),
            patch(
                "handlers.callbacks.get_full_product_details_from_api",
                return_value=info,
            ) as get_details,
            patch("handlers.callbacks.get_available_coupons", return_value=[]),
            patch("handlers.callbacks.get_best_coupon_for_price", return_value=None),
            patch("handlers.callbacks.format_product_message", return_value="formatted"),
        ):
            await product_details_callback(
                SimpleNamespace(callback_query=query),
                SimpleNamespace(),
            )

        get_details.assert_called_once_with("123")
        status_message.edit_text.assert_awaited_once()
        self.assertEqual(
            status_message.edit_text.await_args.args[0],
            "formatted",
        )


if __name__ == "__main__":
    unittest.main()
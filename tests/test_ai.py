import hashlib
import re
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from core.ai import (
    _AI_SERVICE,
    _GEMINI_OFFICIAL_CATEGORY_NAMES,
    CHANNEL_AI_MAX_PROMPT_CHARS,
    GeminiError,
    GeminiRateLimitError,
    GEMINI_MODEL,
    _call_gemini,
    _create_gemini_keyword_cache,
    _deepseek_request_context,
    _gemini_official_category_cache_context,
    _gemini_keyword_cache_state_hash,
    _gemini_keyword_cache_context,
    _normalize_official_categories,
    _restrict_gemini_keywords_to_snapshot,
    _match_user_categories_in_gemini_cache,
    _channel_analysis_prompt,
    classify_product_category_from_title,
    _extract_shipping_country_from_text,
    _extract_offer_type_from_text,
    _local_parse_user_request,
    _normalize_keyword_groups,
    analyze_channel_post,
    analyze_channel_posts,
    analyze_error_logs,
    parse_user_request,
    rebuild_and_sync_gemini_keyword_cache_on_startup,
    reset_ai_service_state,
    sync_gemini_keyword_cache_on_startup,
)
from core.ai_service import AIResponse, gemini_quota_limits


class AiParsingTests(unittest.TestCase):
    def setUp(self):
        loader_patch = patch.object(
            _AI_SERVICE,
            "_usage_state_loader",
            lambda: None,
        )
        saver_patch = patch.object(
            _AI_SERVICE,
            "_usage_state_saver",
            lambda _state: True,
        )
        loader_patch.start()
        saver_patch.start()
        self.addCleanup(loader_patch.stop)
        self.addCleanup(saver_patch.stop)
        key_patch = patch("core.ai.GEMINI_API_KEY", "test-key")
        key_patch.start()
        self.addCleanup(key_patch.stop)
        provider_reader_patch = patch.object(
            _AI_SERVICE,
            "_provider_enabled_reader",
            lambda _feature_key: True,
        )
        provider_writer_patch = patch.object(
            _AI_SERVICE,
            "_provider_enabled_writer",
            lambda _feature_key, enabled: bool(enabled),
        )
        provider_reader_patch.start()
        provider_writer_patch.start()
        self.addCleanup(provider_reader_patch.stop)
        self.addCleanup(provider_writer_patch.stop)
        gemini_stub_patch = patch(
            "core.ai._call_gemini",
            side_effect=GeminiError("Gemini disabled in unit tests"),
        )
        gemini_stub_patch.start()
        self.addCleanup(gemini_stub_patch.stop)
        for provider in _AI_SERVICE._providers:
            if provider.name == "Gemini" or not hasattr(provider, "api_key"):
                continue
            provider_patch = patch.object(provider, "api_key", "")
            provider_patch.start()
            self.addCleanup(provider_patch.stop)
        reset_ai_service_state()

    @patch("core.ai._call_gemini")
    def test_normalizes_channel_offer_analysis(self, call_gemini):
        call_gemini.return_value = {
            "is_offer": True,
            "title": "  POCO C85 6/128GB Batterie 5000mAh - عرض كامل  ",
            "discounted_price": "12,50",
            "currency": "$",
        }
        self.assertEqual(
            analyze_channel_post("POCO C85 6/128GB Batterie 5000mAh بسعر 12.50$"),
            {
                "is_offer": True,
                "title": "POCO C85 6/128GB Batterie 5000mAh - عرض كامل",
                "price": 12.5,
                "currency": "$",
                "coupon_line_numbers": [],
                "link_sections": [],
                "category": "other",
            },
        )
        prompt = call_gemini.call_args.args[0]
        self.assertIn("العنوان الكامل", prompt)
        self.assertIn("title_is_clear", prompt)
        self.assertIn("clean_title", prompt)
        self.assertIn("coupon_line_numbers", prompt)
        self.assertIn("currency", prompt)

    @patch("core.ai._call_gemini")
    def test_preserves_a_clear_channel_title(self, call_gemini):
        call_gemini.return_value = {
            "is_offer": True,
            "title": "  Xiaomi Redmi Note 14 Pro 12GB 256GB  ",
            "title_is_clear": True,
            "clean_title": "Unrelated replacement title",
            "discounted_price": 100,
            "currency": "$",
        }

        result = analyze_channel_post(
            "Xiaomi Redmi Note 14 Pro 12GB 256GB بسعر 100$"
        )

        self.assertEqual(
            result["title"],
            "Xiaomi Redmi Note 14 Pro 12GB 256GB",
        )

    @patch("core.ai._call_gemini")
    def test_uses_clean_title_only_for_an_unclear_channel_title(
        self,
        call_gemini,
    ):
        original_title = (
            "NEW 1:16 Scale Remote Control Monster Truck High Speeds "
            "20KMH Off-Road RC Car with LED Lights for Adults Kids Durable "
            "Gift Ready"
        )
        call_gemini.return_value = {
            "is_offer": True,
            "title": original_title,
            "title_is_clear": False,
            "clean_title": "1:16 Scale Remote Control Monster Truck RC Car",
            "discounted_price": 25,
            "currency": "$",
        }

        result = analyze_channel_post(
            f"{original_title} بسعر 25$"
        )

        self.assertEqual(
            result["title"],
            "1:16 Scale Remote Control Monster Truck RC Car",
        )

    @patch("core.ai._call_gemini")
    def test_invalid_clean_title_falls_back_to_original_title(
        self,
        call_gemini,
    ):
        call_gemini.return_value = {
            "is_offer": True,
            "title": "Bluetooth Wireless Earbuds TWS",
            "title_is_clear": False,
            "clean_title": "   ",
            "discounted_price": 12,
            "currency": "$",
        }

        result = analyze_channel_post(
            "Bluetooth Wireless Earbuds TWS بسعر 12$"
        )

        self.assertEqual(result["title"], "Bluetooth Wireless Earbuds TWS")

    @patch("core.ai._call_ai")
    def test_channel_batch_uses_clean_title_for_an_unclear_title(self, call_ai):
        call_ai.return_value = AIResponse(
            {
                "posts": [
                    {
                        "post_id": "101",
                        "is_offer": True,
                        "title": (
                            "NEW 1:16 Scale Remote Control Monster Truck "
                            "High Speeds 20KMH Off-Road RC Car"
                        ),
                        "title_is_clear": False,
                        "clean_title": (
                            "1:16 Scale Remote Control Monster Truck RC Car"
                        ),
                        "discounted_price": 25,
                        "currency": "$",
                    },
                ]
            },
            provider="Cloudflare",
        )

        result = analyze_channel_posts(
            [{"post_id": 101, "text": "عرض الشاحنة بسعر 25$"}]
        )

        self.assertEqual(
            result[0]["title"],
            "1:16 Scale Remote Control Monster Truck RC Car",
        )
        self.assertEqual(result[0].ai_provider, "Cloudflare")

    @patch("core.ai._call_gemini")
    def test_channel_bundle_classification_requires_explicit_bundle_wording(
        self,
        call_gemini,
    ):
        cases = (
            ("عرض باندل 3 قطع بسعر 30$", "bundle"),
            ("bundle deals: 5 pieces for 30$", "bundle"),
            ("عرض حزمات 4 قطع بسعر 30$", "bundle"),
            ("عرض 3 قطع بسعر 30$ وقطعة واحدة بسعر 12$", "normal"),
            ("عرض 4 قطع بسعر 30$", "normal"),
        )

        for text, expected_offer_type in cases:
            with self.subTest(text=text):
                call_gemini.return_value = {
                    "is_offer": True,
                    "title": "Product",
                    "discounted_price": 12,
                    "currency": "$",
                    "offer_type": "bundle",
                    "bundle_quantity": 5,
                }

                result = analyze_channel_post(text)

                self.assertEqual(result["offer_type"], expected_offer_type)

    def test_extract_offer_type_only_accepts_explicit_bundle_phrases(self):
        self.assertEqual(
            _extract_offer_type_from_text("هذا عرض باندل"),
            "bundle",
        )
        self.assertEqual(
            _extract_offer_type_from_text("bundle deals today"),
            "bundle",
        )
        self.assertEqual(
            _extract_offer_type_from_text("عرض حزم 4 قطع"),
            "bundle",
        )
        self.assertIsNone(_extract_offer_type_from_text("عرض 3 قطع"))
        self.assertIsNone(
            _extract_offer_type_from_text("4 قطع بسعر 30$"),
        )

    @patch("core.ai._call_ai")
    def test_classifies_product_title_into_canonical_category(self, call_ai):
        call_ai.return_value = AIResponse(
            {"category": "tablets", "confidence": "high"},
            provider="Groq",
        )

        result = classify_product_category_from_title("iPad Air 11 inch 256GB")

        self.assertEqual(
            result,
            {
                "category": "tablets",
                "first_level_category_name": "Computer & Office",
                "second_level_category_name": "Tablets",
                "provider": "Groq",
            },
        )
        self.assertEqual(call_ai.call_args.kwargs["request_kind"], "channel")
        self.assertIn("لا تعتبر المنتج متوافقًا مع هاتف", call_ai.call_args.args[0])

    @patch("core.ai._call_ai")
    def test_unlisted_ai_subcategory_uses_only_a_listed_fallback(self, call_ai):
        call_ai.return_value = {
            "category": "tablets",
            "second_level_category_name": "Tablet PCs",
            "confidence": "high",
        }

        result = classify_product_category_from_title("iPad Air")

        self.assertEqual(result["second_level_category_name"], "Tablets")
        prompt = call_ai.call_args.args[0]
        self.assertIn("Tablets", prompt)
        self.assertNotIn("Tablet PCs", prompt)

    @patch("core.ai._call_ai")
    def test_ai_does_not_store_an_unlisted_generic_subcategory(self, call_ai):
        call_ai.return_value = {
            "category": "home",
            "second_level_category_name": "Home Appliances",
            "confidence": "high",
        }

        result = classify_product_category_from_title("Kitchen appliance")

        self.assertEqual(result["category"], "home")
        self.assertIsNone(result["second_level_category_name"])

    @patch("core.ai._call_ai")
    def test_classifies_allowed_subcategory_name_verbatim(self, call_ai):
        call_ai.return_value = {
            "category": None,
            "second_level_category_name": "LED Lighting",
            "confidence": "high",
        }

        result = classify_product_category_from_title("Smart LED Lighting Kit")

        self.assertEqual(result["category"], None)
        self.assertEqual(
            result["second_level_category_name"],
            "LED Lighting",
        )
        prompt = call_ai.call_args.args[0]
        self.assertIn("second_level_category_name", prompt)
        self.assertIn("Emergency Safety Supplies", prompt)
        self.assertIn("Cultural Derivatives(Office Supplies)", prompt)
        self.assertNotIn("Tablet PCs", prompt)
        self.assertIn("لا تترجم اسم الفئة", prompt)

    @patch("core.ai._call_ai")
    def test_rejects_unlisted_subcategory_when_no_valid_category_exists(
        self,
        call_ai,
    ):
        call_ai.return_value = {
            "category": "unknown",
            "second_level_category_name": "Unlisted Lighting",
            "confidence": "high",
        }

        self.assertIsNone(
            classify_product_category_from_title("Unknown lighting product")
        )

    @patch("core.ai._call_ai")
    def test_classifies_explicit_smartwatch_and_bluetooth_titles(self, call_ai):
        cases = (
            (
                "Amazfit Smart Watch GPS",
                {"category": "watches", "confidence": "high"},
                "smart_watches",
            ),
            (
                "Bluetooth Wireless Earbuds TWS",
                {"category": "headphones", "confidence": "high"},
                "bluetooth_headphones",
            ),
        )

        for title, response, expected_category in cases:
            with self.subTest(title=title):
                call_ai.return_value = response
                result = classify_product_category_from_title(title)

                self.assertEqual(result["category"], expected_category)

        prompt = call_ai.call_args.args[0]
        self.assertIn("smart_watches", prompt)
        self.assertIn("bluetooth_headphones", prompt)

    @patch("core.ai._call_ai")
    def test_title_accessories_do_not_receive_special_device_categories(
        self,
        call_ai,
    ):
        cases = (
            (
                "Smart Watch replacement strap",
                {"category": "watches", "confidence": "high"},
                "watches",
            ),
            (
                "Bluetooth Earbuds protective case",
                {"category": "headphones", "confidence": "high"},
                "headphones",
            ),
        )

        for title, response, expected_category in cases:
            with self.subTest(title=title):
                call_ai.return_value = response
                result = classify_product_category_from_title(title)

                self.assertEqual(result["category"], expected_category)

    @patch("core.ai._call_gemini")
    def test_channel_analysis_normalizes_special_audio_and_watch_categories(
        self,
        call_gemini,
    ):
        call_gemini.return_value = {
            "is_offer": True,
            "title": "Bluetooth Wireless Earbuds TWS",
            "discounted_price": 12,
            "currency": "$",
            "category": "headphones",
        }

        result = analyze_channel_post(
            "Bluetooth Wireless Earbuds TWS بسعر 12$"
        )

        self.assertEqual(result["category"], "bluetooth_headphones")

        call_gemini.return_value = {
            "is_offer": True,
            "title": "Amazfit Smart Watch GPS",
            "discounted_price": 20,
            "currency": "$",
            "category": "watches",
        }
        result = analyze_channel_post("Amazfit Smart Watch GPS بسعر 20$")
        self.assertEqual(result["category"], "smart_watches")

    @patch("core.ai._call_gemini")
    def test_channel_analysis_recovers_special_categories_from_other(
        self,
        call_gemini,
    ):
        cases = (
            (
                "Bluetooth Wireless Earbuds TWS",
                "bluetooth_headphones",
            ),
            (
                "Amazfit Smart Watch GPS",
                "smart_watches",
            ),
        )
        for title, expected_category in cases:
            with self.subTest(title=title):
                call_gemini.return_value = {
                    "is_offer": True,
                    "title": title,
                    "discounted_price": 20,
                    "currency": "$",
                    "category": "other",
                }
                result = analyze_channel_post(f"{title} بسعر 20$")
                self.assertEqual(result["category"], expected_category)

    @patch("core.ai._call_ai")
    def test_title_category_guard_turns_obvious_phone_accessory_into_accessories(
        self,
        call_ai,
    ):
        call_ai.return_value = {
            "category": "phones",
            "confidence": "high",
        }

        result = classify_product_category_from_title(
            "iPhone 15 Pro Max case with screen protector"
        )

        self.assertEqual(result["category"], "accessories")
        self.assertEqual(
            result["second_level_category_name"],
            "Mobile Phone Accessories",
        )

    @patch("core.ai._call_ai")
    def test_title_category_rejects_low_confidence_or_unknown_categories(
        self,
        call_ai,
    ):
        for response in (
            {"category": "phones", "confidence": "low"},
            {"category": "unknown", "confidence": "high"},
        ):
            with self.subTest(response=response):
                call_ai.return_value = response
                self.assertIsNone(
                    classify_product_category_from_title("منتج غير واضح")
                )

    @patch("core.ai._wait_for_gemini_request_slot")
    @patch("core.ai.requests.post")
    @patch("core.ai.GEMINI_MODEL", "gemini-3.5-flash-lite")
    @patch("core.ai.GEMINI_API_KEY", "test-key")
    def test_gemini_channel_contract_accepts_json_response(
        self,
        post,
        _wait_for_slot,
    ):
        response = SimpleNamespace(
            status_code=200,
            headers={},
            ok=True,
            json=lambda: {
                "candidates": [{
                    "content": {
                        "parts": [{
                            "text": '{"is_offer": true, "title": "هاتف"}',
                        }],
                    },
                }],
            },
        )
        post.return_value = response

        result = _call_gemini("حلل المنشور")

        self.assertEqual(result["is_offer"], True)
        self.assertEqual(result["title"], "هاتف")
        self.assertEqual(result.model, "gemini-3.5-flash-lite")

    @patch("core.ai._wait_for_gemini_request_slot")
    @patch("core.ai.requests.post")
    @patch("core.ai.GEMINI_MODEL", "gemini-3.5-flash-lite")
    @patch("core.ai.GEMINI_API_KEY", "test-key")
    def test_gemini_user_request_references_cached_content(
        self,
        post,
        _wait_for_slot,
    ):
        response = SimpleNamespace(
            status_code=200,
            headers={},
            ok=True,
            json=lambda: {
                "candidates": [{
                    "content": {
                        "parts": [{"text": '{"request_type": "trending"}'}],
                    },
                }],
            },
        )
        post.return_value = response

        result = _call_gemini(
            "حلل نية البحث",
            context={
                "cached_content": "cachedContents/keywords",
                "allowed_keywords": ["smart", "scale"],
            },
        )

        self.assertEqual(result["request_type"], "trending")
        request_body = post.call_args.kwargs["json"]
        self.assertEqual(
            request_body["cachedContent"],
            "cachedContents/keywords",
        )
        self.assertEqual(result.keyword_cache_keywords, ["smart", "scale"])

    @patch("core.ai._wait_for_gemini_request_slot")
    @patch("core.ai.requests.post")
    def test_gemini_phrase_cache_formats_legacy_keys_and_preserves_title_text(
        self,
        post,
        _wait_for_slot,
    ):
        post.return_value = SimpleNamespace(
            status_code=200,
            ok=True,
            json=lambda: {
                "name": "cachedContents/phrases",
                "expireTime": "2030-01-01T00:00:00Z",
            },
        )

        result = _create_gemini_keyword_cache(
            {
                "keywords": [
                    "smart_scale",
                    "car-code-reader",
                    "SSD_M.2 2280",
                    "SSD_M.2-2280",
                ],
            },
            GEMINI_MODEL,
        )

        self.assertEqual(result["name"], "cachedContents/phrases")
        cache_text = post.call_args.kwargs["json"]["contents"][0]["parts"][0][
            "text"
        ]
        self.assertIn("- smart scale", cache_text)
        self.assertIn("- car-code-reader", cache_text)
        self.assertIn("- SSD_M.2 2280", cache_text)
        self.assertIn("- SSD_M.2-2280", cache_text)
        self.assertNotIn("smart_scale", cache_text)
        self.assertNotIn("- car code reader", cache_text)

    def test_gemini_phrase_cache_hash_tracks_rendered_text(self):
        underscore_hash = _gemini_keyword_cache_state_hash(
            ["car_code_reader"]
        )
        spaced_hash = _gemini_keyword_cache_state_hash(
            ["car code reader"]
        )
        hyphen_hash = _gemini_keyword_cache_state_hash(
            ["car-code-reader"]
        )
        self.assertEqual(underscore_hash, spaced_hash)
        self.assertNotEqual(hyphen_hash, spaced_hash)
        self.assertTrue(
            underscore_hash.startswith("title-phrases-v3:")
        )

    def test_gemini_keyword_cache_reuses_unchanged_snapshot(self):
        now = datetime.now(timezone.utc)
        snapshot = {
            "keywords": ["samsung", "galaxy"],
            "snapshot_hash": "same",
            "state": {
                "snapshot_hash": _gemini_keyword_cache_state_hash(
                    ["samsung", "galaxy"]
                ),
                "gemini_cache_name": "cachedContents/current",
                "gemini_model": GEMINI_MODEL,
                "synced_at": now - timedelta(hours=2),
                "expires_at": now + timedelta(hours=1),
            },
        }

        with patch(
            "core.ai.get_gemini_keyword_cache_snapshot",
            return_value=snapshot,
        ), patch("core.ai._create_gemini_keyword_cache") as create_cache:
            context = _gemini_keyword_cache_context("user")

        self.assertEqual(
            context,
            {
                "cached_content": "cachedContents/current",
                "allowed_keywords": ["samsung", "galaxy"],
            },
        )
        create_cache.assert_not_called()

    def test_gemini_keyword_cache_refreshes_legacy_format_immediately(self):
        now = datetime.now(timezone.utc)
        snapshot = {
            "keywords": ["smart_watch", "car-code-reader"],
            "snapshot_hash": "unchanged-source-vocabulary",
            "state": {
                # The prior format also rewrote title-originated hyphens.
                "snapshot_hash": "space-separated-v2:legacy-rendering",
                "gemini_cache_name": "cachedContents/legacy",
                "gemini_model": GEMINI_MODEL,
                "synced_at": now - timedelta(minutes=2),
                "expires_at": now + timedelta(hours=1),
            },
        }
        created = {
            "name": "cachedContents/space-separated",
            "expires_at": now + timedelta(hours=48),
        }

        with patch(
            "core.ai.get_gemini_keyword_cache_snapshot",
            return_value=snapshot,
        ), patch(
            "core.ai._create_gemini_keyword_cache",
            return_value=created,
        ) as create_cache, patch(
            "core.ai.save_gemini_keyword_cache_state",
            return_value=True,
        ) as save_state:
            context = _gemini_keyword_cache_context("user")

        self.assertEqual(
            context,
            {
                "cached_content": "cachedContents/space-separated",
                "allowed_keywords": ["smart_watch", "car-code-reader"],
            },
        )
        create_cache.assert_called_once_with(snapshot, GEMINI_MODEL)
        save_state.assert_called_once_with(
            _gemini_keyword_cache_state_hash(
                ["smart_watch", "car-code-reader"]
            ),
            "cachedContents/space-separated",
            GEMINI_MODEL,
            created["expires_at"],
        )

    def test_gemini_keyword_cache_does_not_reuse_legacy_cache_on_refresh_failure(
        self,
    ):
        now = datetime.now(timezone.utc)
        snapshot = {
            "keywords": ["smart_watch"],
            "snapshot_hash": "unchanged-source-vocabulary",
            "state": {
                "snapshot_hash": "unchanged-source-vocabulary",
                "gemini_cache_name": "cachedContents/legacy",
                "gemini_model": GEMINI_MODEL,
                "synced_at": now - timedelta(minutes=2),
                "expires_at": now + timedelta(hours=1),
            },
        }

        with patch(
            "core.ai.get_gemini_keyword_cache_snapshot",
            return_value=snapshot,
        ), patch(
            "core.ai._create_gemini_keyword_cache",
            return_value=None,
        ) as create_cache, patch(
            "core.ai.save_gemini_keyword_cache_state",
        ) as save_state:
            context = _gemini_keyword_cache_context("user")

        self.assertEqual(context, {"allowed_keywords": ["smart_watch"]})
        create_cache.assert_called_once_with(snapshot, GEMINI_MODEL)
        save_state.assert_not_called()

    def test_gemini_keyword_cache_waits_for_interval_after_snapshot_change(self):
        now = datetime.now(timezone.utc)
        snapshot = {
            "keywords": ["samsung", "galaxy", "ultra"],
            "snapshot_hash": "new",
            "state": {
                "snapshot_hash": _gemini_keyword_cache_state_hash(
                    ["samsung", "galaxy"]
                ),
                "gemini_cache_name": "cachedContents/old",
                "gemini_model": GEMINI_MODEL,
                "synced_at": now - timedelta(minutes=5),
                "expires_at": now + timedelta(hours=1),
            },
        }

        with patch(
            "core.ai.get_gemini_keyword_cache_snapshot",
            return_value=snapshot,
        ), patch("core.ai._create_gemini_keyword_cache") as create_cache:
            context = _gemini_keyword_cache_context("user")

        self.assertEqual(
            context,
            {
                "allowed_keywords": ["samsung", "galaxy", "ultra"],
            },
        )
        create_cache.assert_not_called()

    def test_gemini_keyword_cache_refreshes_immediately_for_model_change(self):
        now = datetime.now(timezone.utc)
        configured_model = "gemini-configured-model"
        snapshot = {
            "keywords": ["samsung", "galaxy", "ultra"],
            "snapshot_hash": "same",
            "state": {
                "snapshot_hash": _gemini_keyword_cache_state_hash(
                    ["samsung", "galaxy", "ultra"]
                ),
                "gemini_cache_name": "cachedContents/old-model",
                "gemini_model": "previous-model",
                "synced_at": now - timedelta(minutes=2),
                "expires_at": now + timedelta(hours=1),
            },
        }
        created = {
            "name": "cachedContents/configured-model",
            "expires_at": now + timedelta(hours=48),
        }

        with patch("core.ai.GEMINI_MODEL", configured_model), patch(
            "core.ai.get_gemini_keyword_cache_snapshot",
            return_value=snapshot,
        ), patch(
            "core.ai._create_gemini_keyword_cache",
            return_value=created,
        ) as create_cache, patch(
            "core.ai.save_gemini_keyword_cache_state",
            return_value=True,
        ) as save_state:
            context = _gemini_keyword_cache_context("user")

        self.assertEqual(
            context,
            {
                "cached_content": "cachedContents/configured-model",
                "allowed_keywords": ["samsung", "galaxy", "ultra"],
            },
        )
        create_cache.assert_called_once_with(snapshot, configured_model)
        save_state.assert_called_once_with(
            _gemini_keyword_cache_state_hash(
                ["samsung", "galaxy", "ultra"]
            ),
            "cachedContents/configured-model",
            configured_model,
            created["expires_at"],
        )

    def test_gemini_keyword_cache_does_not_use_previous_model_on_refresh_failure(self):
        now = datetime.now(timezone.utc)
        snapshot = {
            "keywords": ["samsung"],
            "snapshot_hash": "same",
            "state": {
                "snapshot_hash": _gemini_keyword_cache_state_hash(
                    ["samsung"]
                ),
                "gemini_cache_name": "cachedContents/old-model",
                "gemini_model": "previous-model",
                "synced_at": now - timedelta(minutes=2),
                "expires_at": now + timedelta(hours=1),
            },
        }

        with patch("core.ai.GEMINI_MODEL", "gemini-configured-model"), patch(
            "core.ai.get_gemini_keyword_cache_snapshot",
            return_value=snapshot,
        ), patch(
            "core.ai._create_gemini_keyword_cache",
            return_value=None,
        ), patch(
            "core.ai.save_gemini_keyword_cache_state",
        ) as save_state:
            context = _gemini_keyword_cache_context("user")

        self.assertEqual(
            context,
            {"allowed_keywords": ["samsung"]},
        )
        save_state.assert_not_called()

    def test_startup_keyword_cache_sync_uses_configured_user_context(self):
        with patch("core.ai.GEMINI_API_KEY", "test-key"), patch(
            "core.ai._gemini_keyword_cache_context",
            return_value={"cached_content": "cachedContents/current"},
        ) as load_context, patch(
            "core.ai._gemini_official_category_cache_context",
            return_value={"cached_content": "cachedContents/categories"},
        ) as load_categories:
            self.assertTrue(sync_gemini_keyword_cache_on_startup())

        load_context.assert_called_once_with("user")
        load_categories.assert_called_once_with("user_category")

    def test_gemini_official_category_cache_reuses_matching_snapshot(self):
        now = datetime.now(timezone.utc)
        snapshot_hash = hashlib.sha256(
            "\n".join(_GEMINI_OFFICIAL_CATEGORY_NAMES).encode("utf-8")
        ).hexdigest()
        state = {
            "snapshot_hash": snapshot_hash,
            "gemini_cache_name": "cachedContents/categories",
            "gemini_model": GEMINI_MODEL,
            "synced_at": now - timedelta(hours=2),
            "expires_at": now + timedelta(hours=1),
        }

        with patch(
            "core.ai.get_gemini_official_category_cache_state",
            return_value=state,
        ), patch(
            "core.ai._create_gemini_official_category_cache"
        ) as create_cache:
            context = _gemini_official_category_cache_context("user_category")

        self.assertEqual(
            context["cached_content"],
            "cachedContents/categories",
        )
        self.assertEqual(
            context["allowed_official_categories"],
            list(_GEMINI_OFFICIAL_CATEGORY_NAMES),
        )
        create_cache.assert_not_called()

    def test_official_category_cache_is_not_loaded_for_other_request_kinds(self):
        with patch(
            "core.ai.get_gemini_official_category_cache_state",
        ) as load_state:
            self.assertIsNone(
                _gemini_official_category_cache_context("channel")
            )

        load_state.assert_not_called()

    def test_official_category_normalizer_returns_only_canonical_names(self):
        self.assertEqual(
            _normalize_official_categories(
                {
                    "official_category": "mobile phones",
                    "official_categories": [
                        "Automobiles & Motorcycles",
                    "Car Electronics",
                        "not an official category",
                    ],
                }
            ),
            ["Mobile Phones", "Car Electronics"],
        )

    @patch("core.ai._call_ai")
    def test_gemini_category_stage_adds_exact_filter_and_keeps_other_filters(
        self,
        call_ai,
    ):
        primary_result = AIResponse(
            {
                "request_type": "product_best",
                "keywords": ["Samsung S24"],
                "category": "phones",
                "max_price": 500,
                "category_filter_requested": True,
            },
            provider="Gemini",
        )
        primary_result.keyword_cache_active = True
        primary_result.keyword_cache_keywords = ["Samsung S24"]
        category_result = AIResponse(
            {"official_categories": ["Mobile Phones"]},
            provider="Gemini",
        )
        call_ai.side_effect = [primary_result, category_result]

        result = parse_user_request(
            "أريد أفضل عرض Samsung S24 للهواتف بأقل من 500 دولار"
        )

        self.assertEqual(result["request_type"], "product_price_range")
        self.assertEqual(result["keywords"], ["Samsung S24"])
        self.assertEqual(result["category"], "phones")
        self.assertEqual(result["official_categories"], ["Mobile Phones"])
        self.assertEqual(result["max_price"], 500)
        self.assertEqual(call_ai.call_count, 2)
        self.assertEqual(
            call_ai.call_args.kwargs,
            {
                "request_kind": "user_category",
                "provider_name": "Gemini",
            },
        )

    @patch(
        "core.ai._local_product_request_type",
        return_value="product_cheapest",
    )
    @patch("core.ai._call_ai")
    def test_gemini_category_search_without_keywords_reaches_category_matcher(
        self,
        call_ai,
        _local_product_type,
    ):
        primary_result = AIResponse(
            {
                "request_type": "category_cheapest",
                "keywords": [],
                "category": "phones",
                "category_filter_requested": True,
            },
            provider="Gemini",
        )
        primary_result.keyword_cache_active = True
        primary_result.keyword_cache_keywords = []
        call_ai.side_effect = [
            primary_result,
            AIResponse(
                {
                    "official_categories": [
                        "Mobile Phones",
                        "Tablets",
                    ],
                },
                provider="Gemini",
            ),
        ]

        result = parse_user_request(
            "أرخص عروض الهواتف أو الحواسيب اللوحية تحت 100 دولار"
        )

        self.assertEqual(result["request_type"], "category_cheapest")
        self.assertEqual(
            result["official_categories"],
            ["Mobile Phones", "Tablets"],
        )
        self.assertEqual(call_ai.call_count, 2)
        self.assertEqual(
            call_ai.call_args.kwargs,
            {
                "request_kind": "user_category",
                "provider_name": "Gemini",
            },
        )

    @patch("core.ai._call_ai")
    def test_gemini_product_search_does_not_apply_unrequested_category(
        self,
        call_ai,
    ):
        cases = (
            (
                "عروض سكانير السيارات",
                "category_cheapest",
                "Adaptateur scanner OBD2 L17 Bluetooth sans fil",
                "accessories",
            ),
            (
                "جهاز نفخ عجلات السيارات",
                "product_best",
                "Cordless Car Tire Inflator Portable Air Compressor",
                "home",
            ),
            (
                "عروض جهاز فحص السيارات",
                "trending",
                "V600 OBD2 Diagnostic Scanner Car Code Reader Vehicle Health Inspection Tool Auto Code Reader Scanner",
                "accessories",
            ),
        )

        for text, request_type, phrase, inferred_category in cases:
            with self.subTest(text=text):
                result = AIResponse(
                    {
                        "request_type": request_type,
                        "keywords": [phrase],
                        "keyword_groups": [[phrase]],
                        "category": inferred_category,
                        "category_filter_requested": False,
                    },
                    provider="Gemini",
                )
                result.keyword_cache_active = True
                result.keyword_cache_keywords = [phrase]
                call_ai.return_value = result

                parsed = parse_user_request(text)

                self.assertEqual(parsed["request_type"], "product_best")
                self.assertEqual(parsed["keywords"], [phrase])
                self.assertEqual(parsed["keyword_groups"], [[phrase]])
                self.assertIsNone(parsed["category"])

    @patch("core.ai._call_ai")
    def test_gemini_explicit_category_with_no_match_fails_closed(self, call_ai):
        primary_result = AIResponse(
            {
                "request_type": "category_cheapest",
                "keywords": ["phones"],
                "category": "phones",
                "category_filter_requested": True,
            },
            provider="Gemini",
        )
        primary_result.keyword_cache_active = True
        primary_result.keyword_cache_keywords = ["phones"]
        call_ai.side_effect = [
            primary_result,
            AIResponse(
                {"official_category": None, "official_categories": []},
                provider="Gemini",
            ),
        ]

        result = parse_user_request("أرخص عروض الهواتف")

        self.assertEqual(result["request_type"], "unsupported")
        self.assertTrue(result["no_results"])
        self.assertEqual(result["keywords"], [])

    @patch("core.ai._call_ai")
    def test_non_gemini_category_search_does_not_use_category_cache(self, call_ai):
        call_ai.return_value = AIResponse(
            {
                "request_type": "category_cheapest",
                "keywords": ["phones"],
                "category": "phones",
                "category_filter_requested": True,
                "official_categories": ["Mobile Phones"],
            },
            provider="Groq",
        )

        result = parse_user_request("أرخص عروض الهواتف")

        self.assertEqual(result["request_type"], "category_cheapest")
        self.assertNotIn("official_categories", result)
        self.assertEqual(call_ai.call_count, 1)

    def test_startup_rebuild_finishes_before_gemini_cache_sync(self):
        calls = []
        with patch(
            "core.ai.rebuild_ai_keyword_cache_from_processed_titles",
            side_effect=lambda: calls.append("rebuild") or 4,
        ), patch(
            "core.ai.sync_gemini_keyword_cache_on_startup",
            side_effect=lambda: calls.append("sync") or True,
        ):
            result = rebuild_and_sync_gemini_keyword_cache_on_startup()

        self.assertTrue(result)
        self.assertEqual(calls, ["rebuild", "sync"])

    def test_gemini_keyword_cache_updates_after_interval_and_saves_state(self):
        now = datetime.now(timezone.utc)
        snapshot = {
            "keywords": ["samsung", "galaxy", "ultra"],
            "snapshot_hash": "new",
            "state": {
                "snapshot_hash": _gemini_keyword_cache_state_hash(
                    ["samsung", "galaxy"]
                ),
                "gemini_cache_name": "cachedContents/old",
                "gemini_model": GEMINI_MODEL,
                "synced_at": now - timedelta(minutes=31),
                "expires_at": now + timedelta(hours=1),
            },
        }
        created = {
            "name": "cachedContents/new",
            "expires_at": now + timedelta(hours=48),
        }

        with patch(
            "core.ai.get_gemini_keyword_cache_snapshot",
            return_value=snapshot,
        ), patch(
            "core.ai._create_gemini_keyword_cache",
            return_value=created,
        ) as create_cache, patch(
            "core.ai.save_gemini_keyword_cache_state",
            return_value=True,
        ) as save_state:
            context = _gemini_keyword_cache_context("user")

        self.assertEqual(
            context,
            {
                "cached_content": "cachedContents/new",
                "allowed_keywords": ["samsung", "galaxy", "ultra"],
            },
        )
        create_cache.assert_called_once_with(snapshot, GEMINI_MODEL)
        save_state.assert_called_once_with(
            _gemini_keyword_cache_state_hash(
                ["samsung", "galaxy", "ultra"]
            ),
            "cachedContents/new",
            GEMINI_MODEL,
            created["expires_at"],
        )

    def test_gemini_keywords_are_limited_to_cache_vocabulary(self):
        result = _restrict_gemini_keywords_to_snapshot(
            {
                "request_type": "product_best",
                "keywords": ["smart scale", "phone", "ميزان"],
            },
            {"keywords": ["smart_scale", "ميزان"]},
        )

        self.assertEqual(result["keywords"], ["smart_scale", "ميزان"])

    def test_gemini_cannot_compose_a_phrase_from_separate_cached_entries(self):
        result = _restrict_gemini_keywords_to_snapshot(
            {
                "request_type": "product_best",
                "keywords": [
                    "automotive diagnostic scanner",
                    "car code reader",
                ],
            },
            {
                "keywords": [
                    "automotive",
                    "diagnostic",
                    "scanner",
                    "car_code_reader",
                ]
            },
        )

        self.assertEqual(result["keywords"], ["car_code_reader"])

    def test_gemini_keywords_normalize_cache_variants_without_inventing_terms(
        self,
    ):
        result = _restrict_gemini_keywords_to_snapshot(
            {
                "request_type": "product_best",
                "keywords": ["إلكتروني", "غير موجود"],
            },
            {"keywords": ["الكتروني"]},
        )

        self.assertEqual(result["keywords"], ["الكتروني"])

    def test_gemini_keyword_groups_only_keep_cached_multilingual_terms(self):
        result = _restrict_gemini_keywords_to_snapshot(
            {
                "request_type": "product_best",
                "keywords": ["جهاز فحص السيارات"],
                "keyword_groups": [
                    {
                        "canonical": "جهاز فحص السيارات",
                        "synonyms": {
                            "fr": ["outil diagnostic voiture"],
                            "en": [
                                "automotive diagnostic scanner",
                                "automotive diagnostic scanner widget",
                                "car code reader",
                            ],
                        },
                    }
                ],
            },
            {
                "keywords": [
                    "جهاز_فحص_السيارات",
                    "outil_diagnostic_voiture",
                    "automotive_diagnostic_scanner",
                    "car_code_reader",
                ]
            },
        )

        self.assertEqual(result["keywords"], ["جهاز_فحص_السيارات"])
        self.assertEqual(
            result["keyword_groups"],
            [[
                "جهاز_فحص_السيارات",
                "outil_diagnostic_voiture",
                "automotive_diagnostic_scanner",
                "car_code_reader",
            ]],
        )

    def test_normalizes_multilingual_keyword_groups(self):
        self.assertEqual(
            _normalize_keyword_groups(
                [
                    {
                        "canonical": "حافظات",
                        "synonyms": {
                            "ar": ["أغطية"],
                            "fr": ["coques", "étuis"],
                            "en": ["cases"],
                        },
                    }
                ]
            ),
            [["حافظات", "أغطية", "coques", "étuis", "cases"]],
        )

    @patch("core.ai.get_gemini_keyword_cache_snapshot")
    @patch("core.ai._call_ai")
    def test_non_gemini_providers_keep_multilingual_keyword_groups(
        self,
        call_ai,
        get_snapshot,
    ):
        get_snapshot.return_value = {
            "keywords": ["حافظات", "أغطية", "coques", "cases"],
        }
        for provider in ("Groq", "OpenRouter", "Cloudflare"):
            with self.subTest(provider=provider):
                call_ai.return_value = AIResponse(
                    {
                        "request_type": "product_best",
                        "keywords": ["حافظات"],
                        "category": "phones",
                        "keyword_groups": [
                            {
                                "canonical": "حافظات",
                                "synonyms": {
                                    "ar": ["أغطية"],
                                    "fr": ["coques"],
                                    "en": ["cases"],
                                },
                            }
                        ],
                    },
                    provider=provider,
                )

                result = parse_user_request("أريد عروضًا على حافظات الهواتف")

                self.assertEqual(result["request_type"], "product_best")
                self.assertEqual(
                    result["keyword_groups"],
                    [["حافظات", "أغطية", "coques", "cases"]],
                )

    @patch("core.ai.get_gemini_keyword_cache_snapshot")
    @patch("core.ai._call_ai")
    def test_non_gemini_product_search_uses_cached_semantic_phrases(
        self,
        call_ai,
        get_snapshot,
    ):
        cached_phrases = [
            "lecteur_de_codes_OBD_II",
            "automotive_diagnostic_scanner",
        ]
        get_snapshot.return_value = {"keywords": cached_phrases}
        primary_result = AIResponse(
            {
                "request_type": "product_best",
                "keywords": ["جهاز فحص السيارات"],
                "category": "other",
                "min_price": 15,
                "max_price": 60,
            },
            provider="Groq",
        )
        semantic_result = AIResponse(
            {
                "keywords": ["lecteur_de_codes_OBD_II"],
                "keyword_groups": [[
                    "lecteur_de_codes_OBD_II",
                    "automotive_diagnostic_scanner",
                ]],
            },
            provider="Gemini",
        )
        semantic_result.keyword_cache_active = True
        semantic_result.keyword_cache_keywords = cached_phrases
        call_ai.side_effect = [primary_result, semantic_result]

        result = parse_user_request("عروض جهاز فحص السيارات")

        self.assertEqual(result["request_type"], "product_best")
        self.assertEqual(result["keywords"], ["lecteur_de_codes_OBD_II"])
        self.assertEqual(
            result["keyword_groups"],
            [[
                "lecteur_de_codes_OBD_II",
                "automotive_diagnostic_scanner",
            ]],
        )
        self.assertEqual(result["category"], "other")
        self.assertEqual(result["min_price"], 15)
        self.assertEqual(result["max_price"], 60)
        self.assertEqual(call_ai.call_count, 2)
        self.assertEqual(
            call_ai.call_args.kwargs["provider_name"],
            "Gemini",
        )

    @patch("core.ai.get_gemini_keyword_cache_snapshot")
    @patch("core.ai._call_ai")
    def test_gemini_cache_match_keeps_independent_concept_groups(
        self,
        call_ai,
        get_snapshot,
    ):
        concept_groups = [
            [
                "automotive_diagnostic_scanner",
                "obd_code_reader",
            ],
            [
                "bluetooth_adapter",
                "wireless_receiver",
            ],
        ]
        cached_phrases = [
            phrase
            for group in concept_groups
            for phrase in group
        ]
        get_snapshot.return_value = {"keywords": cached_phrases}
        primary_result = AIResponse(
            {
                "request_type": "product_best",
                "keywords": ["جهاز فحص السيارات", "بلوتوث"],
                "category": "other",
            },
            provider="Groq",
        )
        semantic_result = AIResponse(
            {"keywords": [], "keyword_groups": concept_groups},
            provider="Gemini",
        )
        semantic_result.keyword_cache_active = True
        semantic_result.keyword_cache_keywords = cached_phrases
        call_ai.side_effect = [primary_result, semantic_result]

        result = parse_user_request(
            "أريد جهاز فحص السيارات مع بلوتوث"
        )

        self.assertEqual(
            result["keywords"],
            [
                "automotive_diagnostic_scanner",
                "bluetooth_adapter",
            ],
        )
        self.assertEqual(result["keyword_groups"], concept_groups)

    @patch("core.ai.get_gemini_keyword_cache_snapshot")
    @patch("core.ai._call_ai")
    def test_gemini_quota_fallback_keeps_only_exact_cached_primary_terms(
        self,
        call_ai,
        get_snapshot,
    ):
        get_snapshot.return_value = {
            "keywords": ["smart_scale", "electronic_scale"],
        }
        call_ai.side_effect = [
            AIResponse(
                {
                    "request_type": "product_best",
                    "keywords": ["smart_scale", "invented phrase"],
                    "category": None,
                },
                provider="Groq",
            ),
            GeminiRateLimitError("Gemini quota exhausted"),
        ]

        result = parse_user_request("أريد عروضًا على ميزان ذكي")

        self.assertEqual(result["request_type"], "product_best")
        self.assertEqual(result["keywords"], ["smart_scale"])

    @patch("core.ai.get_gemini_keyword_cache_snapshot")
    @patch("core.ai._call_ai")
    def test_non_gemini_product_with_no_cache_match_cannot_go_broad(
        self,
        call_ai,
        get_snapshot,
    ):
        get_snapshot.return_value = {"keywords": ["coffee_grinder"]}
        primary_result = AIResponse(
            {
                "request_type": "product_best",
                "keywords": ["جهاز فحص السيارات"],
                "category": None,
            },
            provider="OpenRouter",
        )
        semantic_result = AIResponse(
            {"keywords": [], "keyword_groups": []},
            provider="Gemini",
        )
        semantic_result.keyword_cache_active = True
        semantic_result.keyword_cache_keywords = ["coffee_grinder"]
        call_ai.side_effect = [primary_result, semantic_result]

        result = parse_user_request("عروض جهاز فحص السيارات")

        self.assertEqual(result["request_type"], "unsupported")
        self.assertEqual(result["keywords"], [])
        self.assertTrue(result["no_results"])

    @patch("core.ai.get_gemini_keyword_cache_snapshot")
    @patch("core.ai._call_ai")
    def test_gemini_maps_arabic_product_request_to_cached_vocabulary(
        self,
        call_ai,
        get_snapshot,
    ):
        call_ai.return_value = AIResponse(
            {
                "request_type": "product_best",
                "keywords": ["smart scale", "phone"],
                "category": None,
            },
            provider="Gemini",
        )
        call_ai.return_value.keyword_cache_active = True
        get_snapshot.return_value = {
            "keywords": ["smart_scale", "electronic_scale"],
        }

        result = parse_user_request(
            "اريد عروض على الميزان الالكتروني الذكي"
        )

        self.assertEqual(result["request_type"], "product_best")
        self.assertEqual(result["keywords"], ["smart_scale"])
        self.assertEqual(call_ai.call_count, 1)

    @patch("core.ai.get_gemini_keyword_cache_snapshot")
    @patch("core.ai._call_ai")
    def test_gemini_maps_arabic_product_to_cached_multilingual_group(
        self,
        call_ai,
        get_snapshot,
    ):
        call_ai.return_value = AIResponse(
            {
                "request_type": "product_best",
                "keywords": ["جهاز فحص السيارات"],
                "keyword_groups": [
                    {
                        "canonical": "جهاز فحص السيارات",
                        "synonyms": {
                            "fr": ["outil diagnostic voiture"],
                            "en": ["automotive diagnostic scanner"],
                        },
                    }
                ],
                "category": None,
            },
            provider="Gemini",
        )
        call_ai.return_value.keyword_cache_active = True
        get_snapshot.return_value = {
            "keywords": ["automotive_diagnostic_scanner"],
        }

        result = parse_user_request("عروض جهاز فحص السيارات")

        self.assertEqual(result["request_type"], "product_best")
        self.assertEqual(result["keywords"], ["automotive_diagnostic_scanner"])
        self.assertEqual(
            result["keyword_groups"],
            [["automotive_diagnostic_scanner"]],
        )
        self.assertEqual(result.ai_provider, "Gemini")

    @patch("core.ai.get_gemini_keyword_cache_snapshot")
    @patch("core.ai._call_ai")
    def test_gemini_retries_semantic_phrase_match_after_cache_miss(
        self,
        call_ai,
        get_snapshot,
    ):
        cached_phrase = "smart body scale"
        get_snapshot.return_value = {"keywords": [cached_phrase]}
        primary_result = AIResponse(
            {
                "request_type": "product_best",
                "keywords": ["ميزان ذكي"],
                "category": None,
                "max_price": 40,
                "price_mode": "below",
                "shipping_country": "Algeria",
                "offer_type": "bundle",
            },
            provider="Gemini",
        )
        primary_result.keyword_cache_active = True
        primary_result.keyword_cache_keywords = [cached_phrase]
        semantic_result = AIResponse(
            {
                "keywords": [cached_phrase],
                "keyword_groups": [[cached_phrase]],
            },
            provider="Gemini",
        )
        semantic_result.keyword_cache_active = True
        semantic_result.keyword_cache_keywords = [cached_phrase]
        call_ai.side_effect = [primary_result, semantic_result]

        result = parse_user_request(
            "أريد ميزان ذكي بعروض باندل أقل من 40 دولار وتشحن إلى الجزائر"
        )

        self.assertEqual(result["request_type"], "product_price_range")
        self.assertEqual(result["keywords"], [cached_phrase])
        self.assertEqual(result["keyword_groups"], [[cached_phrase]])
        self.assertEqual(result["max_price"], 40)
        self.assertEqual(result["price_mode"], "below")
        self.assertEqual(result["shipping_country"], "Algeria")
        self.assertEqual(result["offer_type"], "bundle")
        self.assertEqual(call_ai.call_count, 2)
        self.assertEqual(
            call_ai.call_args.kwargs["provider_name"],
            "Gemini",
        )
        self.assertIn(
            "لا تشترط تطابق الكلمات حرفيًا",
            call_ai.call_args.args[0],
        )

    @patch("core.ai.get_gemini_keyword_cache_snapshot")
    @patch("core.ai._call_ai")
    def test_gemini_product_request_with_no_cache_match_is_unsupported(
        self,
        call_ai,
        get_snapshot,
    ):
        call_ai.return_value = AIResponse(
            {
                "request_type": "product_best",
                "keywords": ["ميزان", "phone"],
                "category": None,
            },
            provider="Gemini",
        )
        call_ai.return_value.keyword_cache_active = True
        get_snapshot.return_value = {"keywords": ["smart", "scale"]}

        result = parse_user_request("اريد عروض على الميزان الالكتروني الذكي")

        self.assertEqual(result["request_type"], "unsupported")
        self.assertEqual(result["keywords"], [])

    @patch("core.ai.get_gemini_keyword_cache_snapshot")
    @patch("core.ai._call_ai")
    def test_gemini_category_fallback_cannot_broaden_a_product_request(
        self,
        call_ai,
        get_snapshot,
    ):
        call_ai.return_value = AIResponse(
            {
                "request_type": "category_cheapest",
                "keywords": ["home"],
                "category": "home",
            },
            provider="Gemini",
        )
        call_ai.return_value.keyword_cache_active = True
        get_snapshot.return_value = {"keywords": ["home"]}

        result = parse_user_request("اريد عروض على ماكنة القهوة")

        self.assertEqual(result["request_type"], "unsupported")
        self.assertEqual(result["keywords"], [])

    @patch("core.ai.get_gemini_keyword_cache_snapshot")
    @patch("core.ai._call_ai")
    def test_gemini_product_shape_wins_over_category_shape_when_keyword_matches(
        self,
        call_ai,
        get_snapshot,
    ):
        call_ai.return_value = AIResponse(
            {
                "request_type": "category_cheapest",
                "keywords": ["coffee"],
                "category": "home",
            },
            provider="Gemini",
        )
        call_ai.return_value.keyword_cache_active = True
        get_snapshot.return_value = {"keywords": ["coffee", "home"]}

        result = parse_user_request("اريد عروض على ماكنة القهوة")

        self.assertEqual(result["request_type"], "product_best")
        self.assertEqual(result["keywords"], ["coffee"])

    @patch("core.ai._call_ai")
    def test_provider_category_fallback_cannot_broaden_specific_product_request(
        self,
        call_ai,
    ):
        call_ai.return_value = AIResponse(
            {
                "request_type": "category_cheapest",
                "keywords": ["home"],
                "category": "home",
            },
            provider="Groq",
        )

        result = parse_user_request("اريد عروض على الميزان الالكتروني الذكي")

        self.assertEqual(result["request_type"], "unsupported")
        self.assertEqual(result["keywords"], [])

    def test_local_broad_cheapest_request_is_searchable(self):
        result = _local_parse_user_request("اريد عروض باقل سعر ممكن")

        self.assertEqual(result["request_type"], "trending")
        self.assertEqual(result["keywords"], [])

    @patch("core.ai.get_gemini_keyword_cache_snapshot")
    @patch("core.ai._call_ai")
    def test_gemini_unsupported_broad_cheapest_request_uses_search_path(
        self,
        call_ai,
        get_snapshot,
    ):
        call_ai.return_value = AIResponse(
            {
                "request_type": "unsupported",
                "keywords": [],
                "category": None,
            },
            provider="Gemini",
        )
        call_ai.return_value.keyword_cache_active = True
        get_snapshot.return_value = {"keywords": []}

        result = parse_user_request("اريد عروض باقل سعر ممكن")

        self.assertEqual(result["request_type"], "trending")
        self.assertEqual(result["keywords"], [])

    def test_gemini_keyword_cache_is_not_loaded_for_channel_requests(self):
        with patch(
            "core.ai.get_gemini_keyword_cache_snapshot",
        ) as load_snapshot:
            context = _gemini_keyword_cache_context("channel")

        self.assertIsNone(context)
        load_snapshot.assert_not_called()

    def test_gemini_keyword_context_is_available_without_cached_content(self):
        with patch(
            "core.ai.get_gemini_keyword_cache_snapshot",
            return_value={
                "keywords": ["smart_scale"],
                "snapshot_hash": "hash",
                "state": None,
            },
        ), patch("core.ai._create_gemini_keyword_cache", return_value=None):
            context = _gemini_keyword_cache_context("user")

        self.assertEqual(
            context,
            {"allowed_keywords": ["smart_scale"]},
        )

    @patch(
        "core.ai.get_gemini_keyword_cache_snapshot",
        return_value={"keywords": ["smart_scale", "car-code-reader"]},
    )
    def test_deepseek_user_context_uses_the_closed_phrase_vocabulary(
        self,
        load_snapshot,
    ):
        context = _deepseek_request_context("user")

        self.assertEqual(
            context["allowed_keywords"],
            ["smart_scale", "car-code-reader"],
        )
        self.assertIn("smart scale", context["system_prompt"])
        self.assertIn("car-code-reader", context["system_prompt"])
        self.assertIn("عبارات كاملة من القائمة فقط", context["system_prompt"])
        self.assertIn("أيًا كانت اللغة أو الكتابة", context["system_prompt"])
        self.assertIn("قارن هوية المنتج لا مجرد الترابط", context["system_prompt"])
        self.assertIn("لا تحوّل طلب فئة فقط إلى طلب منتج محدد", context["system_prompt"])
        self.assertIn("category_filter_requested", context["system_prompt"])
        self.assertNotIn("cachedContent", context["system_prompt"])
        load_snapshot.assert_called_once_with()

    def test_deepseek_category_context_uses_official_names_without_gemini_cache(
        self,
    ):
        with patch("core.ai.get_gemini_keyword_cache_snapshot") as load_snapshot:
            context = _deepseek_request_context("user_category")

        self.assertEqual(
            context["allowed_official_categories"],
            list(_GEMINI_OFFICIAL_CATEGORY_NAMES),
        )
        self.assertIn(
            _GEMINI_OFFICIAL_CATEGORY_NAMES[0],
            context["system_prompt"],
        )
        self.assertNotIn("cachedContent", context["system_prompt"])
        load_snapshot.assert_not_called()

    def test_deepseek_channel_context_is_only_the_stable_json_policy(self):
        with patch("core.ai.get_gemini_keyword_cache_snapshot") as load_snapshot:
            context = _deepseek_request_context("channel")

        self.assertTrue(context["system_prompt"])
        self.assertNotIn("allowed_keywords", context)
        self.assertNotIn("allowed_official_categories", context)
        load_snapshot.assert_not_called()

    @patch("core.ai._call_ai")
    def test_deepseek_category_stage_uses_its_official_vocabulary(self, call_ai):
        call_ai.return_value = AIResponse(
            {
                "official_category": "Mobile Phones",
                "official_categories": ["Mobile Phones"],
            },
            provider="DeepSeek",
        )
        normalized = {
            "request_type": "category_cheapest",
            "category": "phones",
            "keywords": [],
            "min_price": None,
            "max_price": 200,
        }

        result = _match_user_categories_in_gemini_cache(
            "أرخص الهواتف",
            normalized,
            "DeepSeek",
            category_filter_requested=True,
        )

        self.assertEqual(result["official_categories"], ["Mobile Phones"])
        self.assertEqual(result["request_type"], "category_cheapest")
        self.assertEqual(
            call_ai.call_args.kwargs["provider_name"],
            "DeepSeek",
        )
        self.assertEqual(
            call_ai.call_args.kwargs["request_kind"],
            "user_category",
        )

    @patch("core.ai.get_gemini_keyword_cache_snapshot")
    @patch("core.ai._call_ai")
    def test_deepseek_phrase_retry_uses_closed_vocabulary_and_preserves_filters(
        self,
        call_ai,
        get_snapshot,
    ):
        cached_phrase = "smart body scale"
        get_snapshot.return_value = {"keywords": [cached_phrase]}
        primary_result = AIResponse(
            {
                "request_type": "product_best",
                "keywords": ["unlisted scale phrase"],
                "category": None,
                "max_price": 40,
                "price_mode": "below",
                "shipping_country": "Algeria",
                "offer_type": "bundle",
                "category_filter_requested": False,
            },
            provider="DeepSeek",
        )
        primary_result.keyword_cache_active = True
        primary_result.keyword_cache_keywords = [cached_phrase]
        semantic_result = AIResponse(
            {
                "keywords": [cached_phrase],
                "keyword_groups": [[cached_phrase]],
            },
            provider="DeepSeek",
        )
        semantic_result.keyword_cache_active = True
        semantic_result.keyword_cache_keywords = [cached_phrase]
        call_ai.side_effect = [primary_result, semantic_result]

        result = parse_user_request(
            "أريد ميزان ذكي بعروض باندل أقل من 40 دولار وتشحن إلى الجزائر"
        )

        self.assertEqual(result["request_type"], "product_price_range")
        self.assertEqual(result["keywords"], [cached_phrase])
        self.assertEqual(result["keyword_groups"], [[cached_phrase]])
        self.assertEqual(result["max_price"], 40)
        self.assertEqual(result["shipping_country"], "Algeria")
        self.assertEqual(result["offer_type"], "bundle")
        self.assertEqual(call_ai.call_count, 2)
        self.assertEqual(
            call_ai.call_args.kwargs["provider_name"],
            "DeepSeek",
        )

    @patch("core.ai.get_gemini_keyword_cache_snapshot")
    @patch("core.ai._call_ai")
    def test_closed_vocabulary_product_match_recovers_from_category_misclass(
        self,
        call_ai,
        get_snapshot,
    ):
        cached_phrase = "Portable Mini Washing Machine"
        unrelated_phrase = "Foldable Laundry Basket Organizer"
        cached_phrases = [cached_phrase, unrelated_phrase]
        get_snapshot.return_value = {"keywords": cached_phrases}
        user_text = (
            "أريد عروض باندل لغسالة الملابس المحمولة "
            "أقل من 40 دولار وتشحن إلى الجزائر"
        )

        for provider_name in ("Gemini", "DeepSeek"):
            with self.subTest(provider=provider_name):
                primary_result = AIResponse(
                    {
                        "request_type": "category_cheapest",
                        "keywords": [],
                        "category": "home",
                        "max_price": 40,
                        "price_mode": "below",
                        "shipping_country": "Algeria",
                        "offer_type": "bundle",
                        "category_filter_requested": True,
                    },
                    provider=provider_name,
                )
                primary_result.keyword_cache_active = True
                primary_result.keyword_cache_keywords = cached_phrases
                semantic_result = AIResponse(
                    {
                        "keywords": [cached_phrase],
                        "keyword_groups": [[cached_phrase]],
                    },
                    provider=provider_name,
                )
                semantic_result.keyword_cache_active = True
                semantic_result.keyword_cache_keywords = cached_phrases
                call_ai.side_effect = [primary_result, semantic_result]

                result = parse_user_request(user_text)

                self.assertEqual(result["request_type"], "product_price_range")
                self.assertEqual(result["keywords"], [cached_phrase])
                self.assertEqual(
                    result["keyword_groups"],
                    [[cached_phrase]],
                )
                self.assertIsNone(result["category"])
                self.assertEqual(result["max_price"], 40)
                self.assertEqual(result["shipping_country"], "Algeria")
                self.assertEqual(result["offer_type"], "bundle")
                self.assertEqual(call_ai.call_count, 2)
                self.assertEqual(
                    call_ai.call_args.kwargs["provider_name"],
                    provider_name,
                )
                phrase_prompt = call_ai.call_args.args[0]
                self.assertIn(user_text, phrase_prompt)
                self.assertIn("لا تشترط تطابق الكلمات حرفيًا", phrase_prompt)
                self.assertIn(
                    "قارن هوية المنتج لا مجرد الترابط",
                    phrase_prompt,
                )
                call_ai.reset_mock()

    @patch("core.ai.get_gemini_keyword_cache_snapshot")
    @patch("core.ai._call_ai")
    def test_closed_vocabulary_product_retry_keeps_explicit_category_filter(
        self,
        call_ai,
        get_snapshot,
    ):
        cached_phrase = "Samsung Galaxy S24"
        get_snapshot.return_value = {"keywords": [cached_phrase]}
        user_text = "أريد أفضل عرض Samsung S24 للهواتف بأقل من 500 دولار"

        for provider_name in ("Gemini", "DeepSeek"):
            with self.subTest(provider=provider_name):
                primary_result = AIResponse(
                    {
                        "request_type": "category_cheapest",
                        "keywords": [],
                        "category": "phones",
                        "max_price": 500,
                        "price_mode": "below",
                        "category_filter_requested": True,
                    },
                    provider=provider_name,
                )
                primary_result.keyword_cache_active = True
                primary_result.keyword_cache_keywords = [cached_phrase]
                semantic_result = AIResponse(
                    {
                        "keywords": [cached_phrase],
                        "keyword_groups": [[cached_phrase]],
                    },
                    provider=provider_name,
                )
                semantic_result.keyword_cache_active = True
                semantic_result.keyword_cache_keywords = [cached_phrase]
                category_result = AIResponse(
                    {"official_categories": ["Mobile Phones"]},
                    provider=provider_name,
                )
                call_ai.side_effect = [
                    primary_result,
                    semantic_result,
                    category_result,
                ]

                result = parse_user_request(user_text)

                self.assertEqual(result["request_type"], "product_price_range")
                self.assertEqual(result["keywords"], [cached_phrase])
                self.assertEqual(result["category"], "phones")
                self.assertEqual(
                    result["official_categories"],
                    ["Mobile Phones"],
                )
                self.assertEqual(result["max_price"], 500)
                self.assertEqual(call_ai.call_count, 3)
                self.assertEqual(
                    call_ai.call_args.kwargs["request_kind"],
                    "user_category",
                )
                call_ai.reset_mock()

    @patch("core.ai.get_gemini_keyword_cache_snapshot")
    @patch("core.ai._call_ai")
    def test_closed_vocabulary_product_cache_miss_stays_no_results(
        self,
        call_ai,
        get_snapshot,
    ):
        get_snapshot.return_value = {
            "keywords": ["Unrelated Laundry Storage Basket"],
        }

        for provider_name in ("Gemini", "DeepSeek"):
            with self.subTest(provider=provider_name):
                primary_result = AIResponse(
                    {
                        "request_type": "category_cheapest",
                        "keywords": [],
                        "category": "home",
                        "category_filter_requested": True,
                    },
                    provider=provider_name,
                )
                primary_result.keyword_cache_active = True
                primary_result.keyword_cache_keywords = [
                    "Unrelated Laundry Storage Basket",
                ]
                semantic_result = AIResponse(
                    {"keywords": [], "keyword_groups": []},
                    provider=provider_name,
                )
                semantic_result.keyword_cache_active = True
                semantic_result.keyword_cache_keywords = [
                    "Unrelated Laundry Storage Basket",
                ]
                call_ai.side_effect = [primary_result, semantic_result]

                result = parse_user_request("عروض غسالة الملابس المحمولة")

                self.assertEqual(result["request_type"], "unsupported")
                self.assertEqual(result["keywords"], [])
                self.assertIsNone(result["category"])
                self.assertTrue(result["no_results"])
                self.assertEqual(call_ai.call_count, 2)
                call_ai.reset_mock()

    @patch("core.ai._wait_for_gemini_request_slot")
    @patch("core.ai.requests.post")
    @patch("core.ai.GEMINI_MODEL", "gemini-3.5-flash-lite")
    @patch("core.ai.GEMINI_API_KEY", "test-key")
    def test_gemini_channel_contract_rejects_missing_json_content(
        self,
        post,
        _wait_for_slot,
    ):
        response = SimpleNamespace(
            status_code=200,
            headers={},
            ok=True,
            json=lambda: {"candidates": [{"content": {"parts": []}}]},
        )
        post.return_value = response

        with self.assertRaises(GeminiError):
            _call_gemini("حلل المنشور")

    @patch("core.ai._call_ai")
    def test_formats_error_analysis_into_readable_sections(self, call_ai):
        call_ai.return_value = {
            "summary": "تكرر خطأ الاتصال.",
            "patterns": ["Timeout في طلبات المنتج"],
            "impact": "قد يتأخر الرد على المستخدم.",
            "actions": ["فحص مهلة الاتصال"],
        }

        report = analyze_error_logs(
            [{
                "created_at": "2026-09-13 20:00:00",
                "level": "ERROR",
                "logger_name": "test",
                "message": "timeout",
            }],
            "آخر ربع ساعة",
        )

        self.assertIn("الملخص:\nتكرر خطأ الاتصال.", report)
        self.assertIn("الأنماط المتكررة:\n• Timeout في طلبات المنتج", report)
        self.assertIn("الأثر المحتمل:\nقد يتأخر الرد على المستخدم.", report)
        self.assertIn("الإجراءات المقترحة:\n• فحص مهلة الاتصال", report)

    @patch("core.ai._call_ai")
    def test_bounds_oversized_error_analysis_fields(self, call_ai):
        call_ai.return_value = {
            "summary": "s" * 5000,
            "patterns": ["p" * 1000 for _ in range(5)],
            "impact": "i" * 5000,
            "actions": ["a" * 1000 for _ in range(5)],
        }

        report = analyze_error_logs(
            [{
                "created_at": "2026-09-13 20:00:00",
                "level": "ERROR",
                "logger_name": "test",
                "message": "failure",
            }],
            "اليوم",
        )

        self.assertLessEqual(len(report), 4096)
        self.assertIn("…", report)
        self.assertLessEqual(report.count("•"), 10)

    @patch("core.ai._call_ai", side_effect=RuntimeError("AI unavailable"))
    def test_error_analysis_falls_back_to_a_safe_report(self, call_ai):
        logs = [{
            "created_at": "2026-09-13 20:00:00",
            "level": "ERROR",
            "logger_name": "test",
            "message": "failure",
        }]

        report = analyze_error_logs(logs, "آخر ساعة")

        self.assertIn("تعذر التحليل الآلي", report)
        self.assertIn("تم تسجيل 1 خطأ", report)

    @patch("core.ai._call_ai")
    def test_normalizes_a_channel_batch_by_stable_post_id(self, call_ai):
        call_ai.return_value = AIResponse(
            {
                "posts": [
                    {
                        "post_id": "102",
                        "is_offer": False,
                        "title": None,
                    },
                    {
                        "post_id": "101",
                        "is_offer": True,
                        "title": "هاتف",
                        "discounted_price": 100,
                        "category": "phones",
                        "currency": "$",
                    },
                ]
            },
            provider="Cloudflare",
        )

        result = analyze_channel_posts(
            [
                {"post_id": 101, "text": "هاتف بسعر 100$"},
                {"post_id": 102, "text": "إعلان عام"},
            ]
        )

        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["title"], "هاتف")
        self.assertTrue(result[0]["is_offer"])
        self.assertFalse(result[1]["is_offer"])
        self.assertEqual(result[0].ai_provider, "Cloudflare")
        prompt = call_ai.call_args.args[0]
        self.assertIn("معرّف المنشور: 101", prompt)
        self.assertIn("معرّف المنشور: 102", prompt)
        self.assertEqual(call_ai.call_args.kwargs["request_kind"], "channel")

    @patch("core.ai._call_ai")
    def test_splits_long_channel_batch_without_dropping_or_mixing_posts(
        self,
        call_ai,
    ):
        def respond(prompt, request_kind):
            self.assertEqual(request_kind, "channel")
            self.assertLessEqual(len(prompt), CHANNEL_AI_MAX_PROMPT_CHARS)
            post_ids = [
                int(post_id)
                for post_id in re.findall(
                    r"معرّف المنشور: (\d+)",
                    prompt,
                )
            ]
            return AIResponse(
                {
                    "posts": [
                        {
                            "post_id": str(post_id),
                            "is_offer": True,
                            "title": f"Product {post_id}",
                            "discounted_price": post_id,
                            "category": "phones",
                        }
                        for post_id in reversed(post_ids)
                    ]
                },
                provider="Gemini",
            )

        call_ai.side_effect = respond
        posts = [
            {"post_id": post_id, "text": "عرض هاتف\n" + ("تفصيل " * 2_000)}
            for post_id in range(1, 6)
        ]

        result = analyze_channel_posts(posts)

        self.assertGreater(call_ai.call_count, 1)
        self.assertEqual(
            [analysis["title"] for analysis in result],
            [f"Product {post_id}" for post_id in range(1, 6)],
        )
        self.assertEqual(
            [analysis.ai_provider for analysis in result],
            ["Gemini"] * 5,
        )

    @patch("core.ai._call_ai")
    def test_keeps_small_channel_batches_in_one_request(self, call_ai):
        def respond(prompt, request_kind):
            self.assertEqual(request_kind, "channel")
            post_ids = re.findall(r"معرّف المنشور: (\d+)", prompt)
            return AIResponse(
                {
                    "posts": [
                        {
                            "post_id": post_id,
                            "is_offer": False,
                            "title": None,
                        }
                        for post_id in post_ids
                    ]
                },
                provider="Gemini",
            )

        call_ai.side_effect = respond
        for batch_size in (1, 3, 5):
            with self.subTest(batch_size=batch_size):
                call_ai.reset_mock()
                posts = [
                    {"post_id": post_id, "text": "إعلان"}
                    for post_id in range(1, batch_size + 1)
                ]
                result = analyze_channel_posts(posts)

                self.assertEqual(len(result), batch_size)
                call_ai.assert_called_once()
                self.assertLessEqual(
                    len(call_ai.call_args.args[0]),
                    CHANNEL_AI_MAX_PROMPT_CHARS,
                )
                self.assertLessEqual(
                    len(_channel_analysis_prompt(posts)),
                    CHANNEL_AI_MAX_PROMPT_CHARS,
                )

    @patch("core.ai._call_ai")
    def test_reports_missing_channel_batch_items_without_retrying_singletons(
        self,
        call_ai,
    ):
        call_ai.return_value = {
            "posts": [
                {
                    "post_id": "101",
                    "is_offer": True,
                    "title": "هاتف",
                }
            ]
        }

        result = analyze_channel_posts(
            [
                {"post_id": 101, "text": "هاتف"},
                {"post_id": 102, "text": "سماعة"},
            ]
        )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].channel_post_id, "101")
        self.assertEqual(result.failed_post_ids, ["102"])

    @patch("core.ai._call_gemini")
    def test_channel_offer_without_valid_category_uses_other(self, call_gemini):
        call_gemini.return_value = {
            "is_offer": True,
            "title": "منتج غير مصنف",
            "discounted_price": 20,
            "category": None,
        }

        result = analyze_channel_post("منتج غير مصنف بسعر 20$")

        self.assertEqual(result["category"], "other")

    @patch("core.ai._call_ai")
    def test_channel_analysis_preserves_the_successful_provider_name(self, call_ai):
        for provider in ("Gemini", "Groq", "OpenRouter"):
            with self.subTest(provider=provider):
                call_ai.return_value = AIResponse(
                    {
                        "is_offer": True,
                        "title": "هاتف",
                        "discounted_price": 100,
                        "category": "phones",
                    },
                    provider=provider,
                )

                result = analyze_channel_post("هاتف بسعر 100$")

                self.assertEqual(result.ai_provider, provider)

    @patch("core.ai._call_gemini")
    def test_extracts_shipping_country_from_channel_analysis(self, call_gemini):
        call_gemini.return_value = {
            "is_offer": True,
            "title": "هاتف",
            "discounted_price": 100,
            "currency": "$",
            "shipping_line_numbers": [2],
            "shipping_country": "Algeria",
            "shipping_country_mentions": ["الجزائر"],
        }

        result = analyze_channel_post(
            "هاتف بسعر 100$\nالشحن متوفر إلى الجزائر"
        )

        self.assertEqual(result["shipping_country"], "Algeria")
        self.assertEqual(result["shipping_line_numbers"], [2])

    @patch("core.ai._call_gemini")
    def test_does_not_classify_generic_shipping_as_a_country(self, call_gemini):
        call_gemini.return_value = {
            "is_offer": True,
            "title": "هاتف",
            "discounted_price": 100,
            "shipping_line_numbers": [2],
            "shipping_country": None,
            "shipping_country_mentions": [],
        }

        result = analyze_channel_post("هاتف بسعر 100$\nالشحن متوفر")

        self.assertIsNone(result.get("shipping_country"))
        self.assertEqual(result["shipping_line_numbers"], [])

    @patch("core.ai._call_gemini")
    def test_accepts_country_mentioned_outside_shipping_lines(self, call_gemini):
        call_gemini.return_value = {
            "is_offer": True,
            "title": "هاتف نسخة فرنسا",
            "discounted_price": 100,
            "shipping_line_numbers": [2],
            "shipping_country": "France",
            "shipping_country_mentions": ["فرنسا"],
        }

        result = analyze_channel_post(
            "هاتف نسخة فرنسا\nالشحن متوفر دون تحديد دولة"
        )

        self.assertEqual(result["shipping_country"], "France")
        self.assertEqual(result["shipping_line_numbers"], [1, 2])

    @patch("core.ai._call_gemini")
    def test_accepts_country_mention_without_shipping_context(self, call_gemini):
        call_gemini.return_value = {
            "is_offer": True,
            "title": "هاتف نسخة فرنسا",
            "discounted_price": 100,
            "shipping_line_numbers": [],
            "shipping_country": "France",
            "shipping_country_mentions": ["فرنسا"],
        }

        result = analyze_channel_post("هاتف نسخة فرنسا بسعر 100$")

        self.assertEqual(result["shipping_country"], "France")
        self.assertEqual(result["shipping_line_numbers"], [1])

    @patch("core.ai._call_gemini")
    def test_rejects_country_not_mentioned_anywhere_in_post(self, call_gemini):
        call_gemini.return_value = {
            "is_offer": True,
            "title": "هاتف",
            "discounted_price": 100,
            "shipping_line_numbers": [2],
            "shipping_country": "France",
            "shipping_country_mentions": ["France"],
        }

        result = analyze_channel_post("هاتف بسعر 100$\nشحن مجاني")

        self.assertIsNone(result.get("shipping_country"))
        self.assertEqual(result["shipping_line_numbers"], [])

    @patch("core.ai._call_gemini")
    def test_accepts_country_on_a_separate_referenced_shipping_line(
        self,
        call_gemini,
    ):
        call_gemini.return_value = {
            "is_offer": True,
            "title": "هاتف",
            "discounted_price": 100,
            "shipping_line_numbers": [2, 3],
            "shipping_country": "Algeria",
            "shipping_country_mentions": ["الجزائر"],
        }

        result = analyze_channel_post(
            "هاتف بسعر 100$\nالشحن متوفر إلى الدول التالية:\nالجزائر"
        )

        self.assertEqual(result["shipping_country"], "Algeria")
        self.assertEqual(result["shipping_line_numbers"], [2, 3])

    @patch("core.ai._call_gemini")
    def test_accepts_an_explicit_english_shipping_destination(self, call_gemini):
        call_gemini.return_value = {
            "is_offer": True,
            "title": "Phone",
            "discounted_price": 100,
            "shipping_line_numbers": [2],
            "shipping_country": "France",
            "shipping_country_mentions": ["France"],
        }

        result = analyze_channel_post("Phone offer\nShips to France")

        self.assertEqual(result["shipping_country"], "France")
        self.assertEqual(result["shipping_line_numbers"], [2])

    @patch("core.ai._call_gemini")
    def test_normalizes_user_search_request(self, call_gemini):
        call_gemini.return_value = {
            "request_type": "category_price_range",
            "keywords": ["هواتف", "phones"],
            "min_price": "50",
            "max_price": "100",
        }
        self.assertEqual(
            parse_user_request("هواتف بين 50 و100 دولار"),
            {
                "request_type": "category_price_range",
                "keywords": ["هواتف", "phones"],
                "category": "phones",
                "min_price": 50.0,
                "max_price": 100.0,
                "required_specs": [],
            },
        )

    @patch(
        "core.ai._gemini_keyword_cache_context",
        return_value={"allowed_keywords": ["redmi", "17"]},
    )
    @patch(
        "core.ai.get_gemini_keyword_cache_snapshot",
        return_value={"keywords": ["redmi", "17"]},
    )
    @patch("core.ai._call_gemini")
    def test_keeps_numeric_model_when_ai_omits_it(
        self,
        call_gemini,
        _snapshot,
        _cache_context,
    ):
        call_gemini.return_value = {
            "request_type": "product_best",
            "keywords": ["redmi"],
            "category": "phones",
        }

        result = parse_user_request("اريد عرض على هاتف redmi 17")

        self.assertEqual(result["keywords"], ["redmi", "17"])

    @patch(
        "core.ai._call_gemini",
        side_effect=GeminiRateLimitError("quota exhausted"),
    )
    def test_local_parser_accepts_currency_suffixes_in_ranges(self, _call_gemini):
        result = parse_user_request(
            "عروض هواتف سعرها يتراوح بين 100$ و 150$"
        )

        self.assertEqual(result["request_type"], "category_price_range")
        self.assertEqual((result["min_price"], result["max_price"]), (100.0, 150.0))

    def test_local_parser_distinguishes_thousands_from_decimal_comma(self):
        thousands = _local_parse_user_request(
            "عروض حواسيب أقل من 1,200 دولار"
        )
        decimal = _local_parse_user_request(
            "عروض اكسسوارات أقل من 12,50 دولار"
        )

        self.assertEqual(thousands["max_price"], 1200.0)
        self.assertEqual(decimal["max_price"], 12.5)

    @patch("core.ai._call_gemini")
    def test_local_parser_recovers_supported_intent_from_bad_model_schema(
        self,
        call_gemini,
    ):
        call_gemini.return_value = {
            "request_type": "unknown",
            "keywords": [],
        }

        result = parse_user_request("أرخص هاتف Samsung S24")

        self.assertEqual(result["request_type"], "product_cheapest")
        self.assertIn("samsung", result["keywords"])

    @patch("core.ai._call_gemini")
    def test_preserves_explicit_unsupported_model_decisions(self, call_gemini):
        call_gemini.return_value = {
            "request_type": "unsupported",
            "keywords": ["Samsung S24", "iPhone 15"],
            "category": "phones",
            "max_price": 1000,
            "shipping_country": "Algeria",
        }
        requests = (
            "قارن بين Samsung S24 و iPhone 15 تحت 1000 دولار",
            "رشح لي هاتف Samsung مناسب للتصوير بسعر 500 دولار",
            "هل هاتف Samsung S24 متوفر ويشحن إلى الجزائر؟",
        )

        for text in requests:
            with self.subTest(text=text):
                result = parse_user_request(text)
                self.assertEqual(result["request_type"], "unsupported")
                self.assertEqual(result["keywords"], [])
                self.assertIsNone(result["category"])
                self.assertIsNone(result["max_price"])

    @patch(
        "core.ai._gemini_keyword_cache_context",
        return_value={"allowed_keywords": ["Samsung", "هواتف"]},
    )
    @patch(
        "core.ai.get_gemini_keyword_cache_snapshot",
        return_value={"keywords": ["Samsung", "هواتف"]},
    )
    @patch("core.ai._call_gemini")
    def test_scopes_brand_category_requests_to_the_brand_product(
        self,
        call_gemini,
        _snapshot,
        _cache_context,
    ):
        call_gemini.return_value = {
            "request_type": "category_cheapest",
            "keywords": ["Samsung", "هواتف"],
            "category": "phones",
        }

        result = parse_user_request("أرخص هواتف Samsung")

        self.assertEqual(result["request_type"], "product_cheapest")
        self.assertEqual(result["category"], "phones")
        self.assertEqual(result["keywords"], ["Samsung", "هواتف"])

    @patch("core.ai._call_gemini")
    def test_keeps_shipping_country_as_an_additional_search_filter(self, call_gemini):
        call_gemini.return_value = {
            "request_type": "category_price_range",
            "keywords": ["هواتف", "phones"],
            "category": "phones",
            "max_price": 200,
            "shipping_country": "Algeria",
        }

        result = parse_user_request("هواتف أقل من 200 دولار تشحن إلى الجزائر")

        self.assertEqual(result["shipping_country"], "Algeria")
        self.assertEqual(result["max_price"], 200.0)

    @patch(
        "core.ai._gemini_keyword_cache_context",
        return_value={
            "allowed_keywords": ["Samsung S24 Ultra", "256GB"],
        },
    )
    @patch(
        "core.ai.get_gemini_keyword_cache_snapshot",
        return_value={
            "keywords": ["Samsung S24 Ultra", "256GB"],
        },
    )
    @patch("core.ai._call_gemini")
    def test_keeps_supported_long_request_details_for_model(
        self,
        call_gemini,
        _snapshot,
        _cache_context,
    ):
        call_gemini.return_value = {
            "request_type": "product_cheapest",
            "keywords": ["Samsung S24 Ultra", "256GB"],
        }
        long_request = (
            "السلام عليكم، أبحث عن هاتف مناسب للعمل والتصوير. "
            + ("شرح إضافي لا يغير الطلب. " * 400)
            + "المهم أريد Samsung S24 Ultra 256GB بأرخص عرض."
        )

        result = parse_user_request(long_request)

        self.assertEqual(result["request_type"], "product_cheapest")
        self.assertEqual(result["keywords"], ["Samsung S24 Ultra", "256GB"])
        self.assertEqual(
            result["required_specs"],
            [],
        )
        prompt = call_gemini.call_args.args[0]
        self.assertIn("لا تشترط تطابق الكلمات حرفيًا", prompt)
        self.assertIn("Samsung S24 Ultra 256GB", prompt)
        self.assertIn("... [تم اختصار الجزء الأوسط", prompt)

    @patch("core.ai._call_gemini")
    def test_rejects_incomplete_category_intent(self, call_gemini):
        call_gemini.return_value = {
            "request_type": "category_cheapest",
            "keywords": [],
            "category": None,
        }

        self.assertEqual(
            parse_user_request("أريد أفضل شيء بسعر مناسب"),
            {
                "request_type": "unsupported",
                "keywords": [],
                "category": None,
                "min_price": None,
                "max_price": None,
                "required_specs": [],
            },
        )

    @patch("core.ai._call_gemini")
    def test_extracts_storage_and_ram_constraints_from_user_text(self, call_gemini):
        call_gemini.return_value = {
            "request_type": "category_price_range",
            "keywords": ["هواتف", "phones"],
            "category": "phones",
            "max_price": 300,
        }

        result = parse_user_request(
            "اريد عروض هواتف دات مساحة تخزين 256gb ورام 12gb "
            "ويجب ان تكون بسعر اقل من 300$"
        )

        self.assertEqual(result["request_type"], "category_price_range")
        self.assertEqual(result["max_price"], 300.0)
        self.assertEqual(
            result["required_specs"],
            [
                {"type": "storage", "value": "256GB"},
                {"type": "ram", "value": "12GB"},
            ],
        )
        self.assertIn("required_specs", call_gemini.call_args.args[0])

    @patch("core.ai._call_gemini")
    def test_preserves_category_filter_for_trending_requests(self, call_gemini):
        call_gemini.return_value = {
            "request_type": "trending",
            "keywords": ["هواتف", "phones"],
            "category": "phones",
        }

        result = parse_user_request(
            "اريد العروض الرائجة اليوم على الهواتف فقط"
        )

        self.assertEqual(result["request_type"], "trending")
        self.assertEqual(result["category"], "phones")
        self.assertEqual(result["keywords"], ["هواتف", "phones"])

    @patch("core.ai._call_gemini")
    def test_swaps_reversed_price_range(self, call_gemini):
        call_gemini.return_value = {
            "request_type": "product_price_range",
            "keywords": ["هاتف"],
            "min_price": 150,
            "max_price": 50,
        }
        result = parse_user_request("من 150 إلى 50")
        self.assertEqual(result["min_price"], 50.0)
        self.assertEqual(result["max_price"], 150.0)

    @patch(
        "core.ai._call_gemini",
        side_effect=GeminiRateLimitError("quota exhausted"),
    )
    def test_cache_restricts_local_parser_when_gemini_quota_is_exhausted(
        self,
        _call_gemini,
    ):
        self.assertEqual(
            parse_user_request("أريد عروض هواتف بين 50 و100 دولار"),
            {
                "request_type": "category_price_range",
                "keywords": [],
                "category": "phones",
                "min_price": 50.0,
                "max_price": 100.0,
                "required_specs": [],
            },
        )

    @patch(
        "core.ai._call_gemini",
        side_effect=GeminiRateLimitError("quota exhausted"),
    )
    def test_local_parser_rejects_specific_product_without_cache_matches(
        self,
        _call_gemini,
    ):
        result = parse_user_request("أرخص هاتف Samsung S24 Ultra 256GB")

        self.assertEqual(result["request_type"], "unsupported")
        self.assertIsNone(result["category"])
        self.assertEqual(result["keywords"], [])

    @patch(
        "core.ai._call_gemini",
        side_effect=GeminiRateLimitError("quota exhausted"),
    )
    def test_local_parser_keeps_trending_category_without_raw_keywords(
        self,
        _call_gemini,
    ):
        result = parse_user_request("العروض الرائجة اليوم على الهواتف")

        self.assertEqual(result["request_type"], "trending")
        self.assertEqual(result["category"], "phones")
        self.assertEqual(result["keywords"], [])

    @patch(
        "core.ai._call_gemini",
        side_effect=GeminiRateLimitError("quota exhausted"),
    )
    def test_local_parser_uses_directional_price_modes(self, _call_gemini):
        below = _local_parse_user_request("عروض سماعات أقل من 100 دولار")
        above = _local_parse_user_request("عروض سماعات فوق 100 دولار")
        near = _local_parse_user_request("عروض سماعات في حدود 100 دولار")

        self.assertEqual((below["price_mode"], below["max_price"]), ("below", 100.0))
        self.assertEqual((above["price_mode"], above["min_price"]), ("above", 100.0))
        self.assertEqual(
            (near["price_mode"], near["target_price"]),
            ("near", 100.0),
        )

    def test_shipping_fallback_allows_text_between_shipping_and_country(self):
        self.assertEqual(
            _extract_shipping_country_from_text("الشحن متوفر إلى الجزائر"),
            "الجزائر",
        )
        self.assertEqual(
            _extract_shipping_country_from_text("عروض الشحن نحو فرنسا"),
            "فرنسا",
        )
        self.assertEqual(
            _extract_shipping_country_from_text("عروض الشحن إلى فرنسا"),
            "فرنسا",
        )
        self.assertEqual(
            _extract_shipping_country_from_text("عروض الشحن الى فرنسا؟"),
            "فرنسا",
        )

    def test_local_parser_handles_the_reported_arabic_search_phrases(self):
        cases = (
            (
                "عروض هواتف سعرها يتراوح بين $100 و $150",
                ("category_price_range", "phones", 100.0, 150.0, None),
            ),
            (
                "عروض هواتف سعرها يفوق $100",
                ("category_price_range", "phones", 100.0, None, "above"),
            ),
            (
                "عروض هواتف سعرها اقل من $200",
                ("category_price_range", "phones", None, 200.0, "below"),
            ),
            (
                "عروض اكسسوارات اقل من $100",
                ("category_price_range", "accessories", None, 100.0, "below"),
            ),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                result = _local_parse_user_request(text)
                self.assertEqual(
                    (
                        result["request_type"],
                        result["category"],
                        result["min_price"],
                        result["max_price"],
                        result.get("price_mode"),
                    ),
                    expected,
                )

    def test_local_parser_supports_broad_trending_price_and_shipping_searches(self):
        for text in (
            "احسن عروض اليوم",
            "ابرز عروض اليوم",
            "العروض الرائجة اليوم",
        ):
            with self.subTest(text=text):
                self.assertEqual(
                    _local_parse_user_request(text)["request_type"],
                    "trending",
                )

        price_result = _local_parse_user_request("عروض منتجات اقل من $100")
        self.assertEqual(price_result["request_type"], "trending")
        self.assertEqual(price_result["max_price"], 100.0)

        shipping_result = _local_parse_user_request("عروض الشحن نحو فرنسا")
        self.assertEqual(shipping_result["request_type"], "trending")
        self.assertEqual(shipping_result["shipping_country"], "فرنسا")

        combined = _local_parse_user_request(
            "هواتف Samsung تحت 300$ تشحن إلى الجزائر"
        )
        self.assertEqual(combined["category"], "phones")
        self.assertEqual(combined["max_price"], 300.0)
        self.assertEqual(combined["shipping_country"], "الجزائر")
        self.assertIn("samsung", combined["keywords"])

    @patch("core.ai._call_gemini")
    def test_shipping_prepositions_share_the_same_broad_intent(self, call_gemini):
        call_gemini.return_value = {
            "request_type": "unsupported",
            "keywords": [],
        }

        results = [
            parse_user_request(text)
            for text in (
                "عروض الشحن نحو فرنسا",
                "عروض الشحن إلى فرنسا",
                "عروض الشحن الى فرنسا",
            )
        ]

        self.assertEqual(results[0], results[1])
        self.assertEqual(results[1], results[2])
        self.assertEqual(results[0]["request_type"], "trending")
        self.assertEqual(results[0]["keywords"], [])
        self.assertEqual(results[0]["shipping_country"], "فرنسا")

    @patch("core.ai._call_gemini")
    def test_shipping_terms_do_not_replace_product_context(self, call_gemini):
        call_gemini.return_value = {
            "request_type": "product_best",
            "keywords": ["هواتف", "تشحن", "إلى", "France"],
            "category": "phones",
            "shipping_country": "France",
        }

        results = [
            parse_user_request(text)
            for text in (
                "هواتف تشحن نحو فرنسا",
                "هواتف تشحن إلى فرنسا",
                "هواتف تشحن الى فرنسا",
            )
        ]

        self.assertEqual(results[0], results[1])
        self.assertEqual(results[1], results[2])
        self.assertEqual(results[0]["request_type"], "category_cheapest")
        self.assertEqual(results[0]["category"], "phones")
        self.assertEqual(results[0]["shipping_country"], "France")
        self.assertNotIn("تشحن", results[0]["keywords"])
        self.assertNotIn("إلى", results[0]["keywords"])

    @patch("core.ai._call_gemini")
    def test_broad_shipping_intent_recovers_when_model_omits_country(
        self,
        call_gemini,
    ):
        call_gemini.return_value = {
            "request_type": "product_best",
            "keywords": ["عروض", "الشحن", "France"],
        }

        results = [
            parse_user_request(text)
            for text in (
                "عروض الشحن نحو فرنسا",
                "عروض الشحن إلى فرنسا",
                "عروض الشحن الى فرنسا",
            )
        ]

        self.assertEqual(results[0], results[1])
        self.assertEqual(results[1], results[2])
        self.assertEqual(results[0]["request_type"], "trending")
        self.assertEqual(results[0]["keywords"], [])
        self.assertEqual(results[0]["shipping_country"], "فرنسا")

    @patch("core.ai._call_gemini")
    def test_deterministic_price_rules_correct_incomplete_model_output(
        self,
        call_gemini,
    ):
        call_gemini.return_value = {
            "request_type": "product_best",
            "keywords": ["سعرها", "يتراوح"],
            "category": "phones",
        }

        result = parse_user_request(
            "عروض هواتف سعرها يتراوح بين $100 و $150"
        )

        self.assertEqual(result["request_type"], "category_price_range")
        self.assertEqual((result["min_price"], result["max_price"]), (100.0, 150.0))

    @patch("core.ai._call_gemini")
    def test_global_filters_override_incorrect_product_intent(self, call_gemini):
        call_gemini.return_value = {
            "request_type": "product_best",
            "keywords": ["منتجات"],
        }
        price_result = parse_user_request("عروض منتجات اقل من $100")
        self.assertEqual(price_result["request_type"], "trending")
        self.assertEqual(price_result["max_price"], 100.0)

        call_gemini.return_value = {
            "request_type": "product_best",
            "keywords": ["France"],
            "shipping_country": "France",
        }
        shipping_result = parse_user_request("عروض الشحن نحو فرنسا")
        self.assertEqual(shipping_result["request_type"], "trending")
        self.assertEqual(shipping_result["keywords"], [])
        self.assertEqual(shipping_result["shipping_country"], "France")

        call_gemini.return_value = {
            "request_type": "product_best",
            "keywords": ["عروض"],
            "max_price": 100,
        }
        near_result = parse_user_request("اريد عروض في حدود 100$")
        self.assertEqual(near_result["request_type"], "trending")
        self.assertEqual(near_result["price_mode"], "near")
        self.assertEqual(near_result["target_price"], 100.0)
        self.assertEqual(near_result["keywords"], [])

    @patch("core.ai._wait_for_gemini_request_slot")
    @patch("core.ai.requests.post")
    @patch("core.ai.GEMINI_MODEL", "gemini-3.5-flash-lite")
    @patch("core.ai.GEMINI_API_KEY", "test-key")
    def test_keeps_selected_model_after_rate_limit(self, post, _wait_for_slot):
        rate_limited = SimpleNamespace(
            status_code=429,
            headers={},
            ok=False,
        )
        post.return_value = rate_limited

        with self.assertRaises(GeminiRateLimitError):
            _call_gemini("أعد JSON فقط")

        post.assert_called_once()
        self.assertIn(
            "/models/gemini-3.5-flash-lite:generateContent",
            post.call_args.args[0],
        )

    def test_gemini_daily_quota_uses_pacific_reset(self):
        response = SimpleNamespace(
            headers={},
            json=lambda: {
                "error": {
                    "message": "Daily requests exhausted",
                    "details": [{
                        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                        "violations": [{
                            "quotaMetric": "generate_content_free_tier_requests",
                            "quotaId": "RequestsPerModelPerDay-FreeTier",
                            "quotaValue": "20",
                        }],
                    }],
                }
            },
        )

        limits = gemini_quota_limits(response)

        self.assertEqual(limits[0]["metric"], "rpd")
        self.assertEqual(limits[0]["limit"], 20)
        self.assertEqual(limits[0]["remaining"], 0)
        self.assertIsNotNone(limits[0]["reset_at"])


if __name__ == "__main__":
    unittest.main()
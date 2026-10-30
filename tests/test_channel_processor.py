import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from core.ai import (
    ChannelBatchAnalysis,
    ChannelAnalysis,
    ChannelBatchResponseError,
    GeminiError,
    GeminiRateLimitError,
)

from core.channel_processor import (
    build_source_link,
    _build_channel_content,
    _apply_channel_analysis,
    _get_photo_file_id,
    prepare_channel_message,
    process_channel_messages,
    register_channel_message,
    rewrite_post_text,
    _resolved_category_fields,
)


class ChannelProcessorTests(unittest.IsolatedAsyncioTestCase):
    def test_uses_ai_category_only_when_aliexpress_category_is_unavailable(self):
        self.assertEqual(
            _resolved_category_fields({"category": "phones"}, None),
            {
                "category": "phones",
                "first_level_category_name": "Phones & Telecommunications",
                "second_level_category_name": "Mobile Phones",
                "category_source": "ai",
            },
        )
        self.assertEqual(
            _resolved_category_fields(
                {"category": "phones"},
                {
                    "category": "accessories",
                    "first_level_category_name": "Phones & Telecommunications",
                    "second_level_category_name": "Mobile Phone Accessories",
                },
            )["category"],
            "accessories",
        )

    def test_api_subcategory_is_authoritative_when_internal_family_is_other(self):
        fields = _resolved_category_fields(
            {
                "category": "phones",
                "second_level_category_name": "Mobile Phones",
            },
            {
                "category": "other",
                "first_level_category_name": "Consumer Electronics",
                "second_level_category_name": "Home Audio & Video",
            },
        )

        self.assertEqual(fields["category"], "other")
        self.assertEqual(
            fields["first_level_category_name"],
            "Consumer Electronics",
        )
        self.assertEqual(
            fields["second_level_category_name"],
            "Home Audio & Video",
        )
        self.assertEqual(fields["category_source"], "aliexpress_api")

    def test_ai_category_fallback_uses_canonical_names_and_marks_its_source(self):
        fields = _resolved_category_fields({"category": "headphones"}, None)

        self.assertEqual(fields["category"], "headphones")
        self.assertEqual(
            fields["first_level_category_name"],
            "Consumer Electronics",
        )
        self.assertEqual(
            fields["second_level_category_name"],
            "Earphones & Headphones",
        )
        self.assertEqual(fields["category_source"], "ai")

    def test_ai_category_fallback_uses_the_successful_provider_name(self):
        for provider in ("Gemini", "Groq", "Cloudflare", "OpenRouter"):
            with self.subTest(provider=provider):
                analysis = SimpleNamespace(
                    ai_provider=provider,
                    get=lambda key, default=None: {
                        "category": "phones",
                    }.get(key, default),
                )

                fields = _resolved_category_fields(analysis, None)

                self.assertEqual(fields["category_source"], provider.casefold())
                self.assertEqual(
                    fields["first_level_category_name"],
                    "Phones & Telecommunications",
                )
                self.assertEqual(
                    fields["second_level_category_name"],
                    "Mobile Phones",
                )

    def test_ai_subcategory_fallback_accepts_exact_taxonomy_name(self):
        analysis = SimpleNamespace(
            ai_provider="Cloudflare",
            get=lambda key, default=None: {
                "category": "other",
            }.get(key, default),
        )

        fields = _resolved_category_fields(
            analysis,
            None,
            {
                "category": None,
                "second_level_category_name": "LED Lighting",
                "provider": "Groq",
            },
        )

        self.assertEqual(fields["second_level_category_name"], "LED Lighting")
        self.assertEqual(fields["category_source"], "groq")

    def test_aliexpress_subcategory_still_overrides_ai_fallback_name(self):
        fields = _resolved_category_fields(
            {
                "category": "other",
                "second_level_category_name": "LED Lighting",
            },
            {
                "category": "phones",
                "first_level_category_name": "Phones & Telecommunications",
                "second_level_category_name": "Mobile Phones",
            },
            {
                "category": None,
                "second_level_category_name": "LED Lighting",
                "provider": "Groq",
            },
        )

        self.assertEqual(fields["category"], "phones")
        self.assertEqual(
            fields["second_level_category_name"],
            "Mobile Phones",
        )
        self.assertEqual(fields["category_source"], "aliexpress_api")

    def test_api_category_fills_missing_official_level_names(self):
        fields = _resolved_category_fields(
            {"category": "phones"},
            {"category": "phones"},
        )

        self.assertEqual(
            fields["first_level_category_name"],
            "Phones & Telecommunications",
        )
        self.assertEqual(fields["second_level_category_name"], "Mobile Phones")
        self.assertEqual(fields["category_source"], "aliexpress_api")

    def test_special_ai_category_refines_compatible_api_category(self):
        analysis = SimpleNamespace(
            ai_provider="Groq",
            get=lambda key, default=None: {
                "category": "smart_watches",
            }.get(key, default),
        )

        fields = _resolved_category_fields(
            analysis,
            {
                "category": "watches",
                "first_level_category_name": "Watches",
                "second_level_category_name": "Smart Watches",
            },
        )

        self.assertEqual(fields["category"], "smart_watches")
        self.assertEqual(fields["category_source"], "groq")
        self.assertEqual(fields["second_level_category_name"], "Smart Watches")

    def test_special_ai_category_survives_unmapped_api_category(self):
        cases = (
            (
                "bluetooth_headphones",
                "Consumer Electronics",
                "Unmapped Audio Products",
            ),
            (
                "smart_watches",
                "Wearable Technology",
                "Other Wearables",
            ),
        )

        for category, first_level, second_level in cases:
            with self.subTest(category=category):
                analysis = SimpleNamespace(
                    ai_provider="Groq",
                    get=lambda key, default=None, value=category: {
                        "category": value,
                    }.get(key, default),
                )

                fields = _resolved_category_fields(
                    analysis,
                    {
                        "category": "other",
                        "first_level_category_name": first_level,
                        "second_level_category_name": second_level,
                    },
                )

                self.assertEqual(fields["category"], category)
                self.assertEqual(fields["category_source"], "groq")
                self.assertEqual(fields["first_level_category_name"], first_level)
                self.assertEqual(fields["second_level_category_name"], second_level)

    def test_special_ai_category_does_not_override_incompatible_api_category(self):
        analysis = SimpleNamespace(
            ai_provider="Groq",
            get=lambda key, default=None: {
                "category": "smart_watches",
            }.get(key, default),
        )

        fields = _resolved_category_fields(
            analysis,
            {
                "category": "phones",
                "first_level_category_name": "Phones & Telecommunications",
                "second_level_category_name": "Mobile Phones",
            },
        )

        self.assertEqual(fields["category"], "phones")
        self.assertEqual(fields["category_source"], "aliexpress_api")

    def test_rewrites_telegram_and_all_aliexpress_links(self):
        text = (
            "عرض جديد https://t.me/source/12\n"
            "الرابط الأول https://www.aliexpress.com/item/111.html\n"
            "والثاني https://a.aliexpress.com/_abc"
        )
        rewritten = rewrite_post_text(
            text,
            {
                "https://www.aliexpress.com/item/111.html": "https://affiliate/111",
                "https://a.aliexpress.com/_abc": "https://affiliate/abc",
            },
        )
        self.assertIn("https://t.me/rabahcopons/7366", rewritten)
        self.assertIn("https://affiliate/111", rewritten)
        self.assertIn("https://affiliate/abc", rewritten)
        self.assertNotIn("https://t.me/source/12", rewritten)

    def test_builds_public_source_link(self):
        self.assertEqual(
            build_source_link("@offers", 42),
            "https://t.me/offers/42",
        )
        self.assertEqual(
            build_source_link(None, 42, -1001234567890),
            "https://t.me/c/1234567890/42",
        )
        self.assertIsNone(build_source_link(None, 42))

    def test_reads_bot_api_photo_file_id_without_assuming_telethon_has_one(self):
        self.assertEqual(
            _get_photo_file_id(
                SimpleNamespace(
                    photo=[SimpleNamespace(file_id="bot-file-id")],
                )
            ),
            "bot-file-id",
        )
        self.assertIsNone(
            _get_photo_file_id(
                SimpleNamespace(
                    photo=SimpleNamespace(id=123),
                )
            )
        )

    def test_builds_template_from_original_coupon_and_link_lines(self):
        text = (
            "منتج ممتاز بسعر 12.50$\n"
            "كوبون 5$ للطلبات فوق 30$\n"
            "رابط عملات:\n"
            "https://s.click.aliexpress.com/e/_c3tbmYUP\n"
            "رابط مباشر:\n"
            "https://s.click.aliexpress.com/e/_c2yy0GsJ\n"
        )
        content = _build_channel_content(
            text,
            {
                "title": "منتج ممتاز",
                "price": 12.5,
                "currency": "$",
                "coupon_line_numbers": [2],
                "link_sections": [
                    {"line_numbers": [3, 4]},
                    {"line_numbers": [5, 6]},
                ],
            },
            [
                "https://s.click.aliexpress.com/e/_c3tbmYUP",
                "https://s.click.aliexpress.com/e/_c2yy0Gs",
            ],
            {
                "https://s.click.aliexpress.com/e/_c3tbmYUP": "https://affiliate/coins",
                "https://s.click.aliexpress.com/e/_c2yy0Gs": "https://affiliate/direct",
            },
        )

        self.assertIn("🔥 تخفيض على  منتج ممتاز", content)
        self.assertIn("💶 السعر: [ 12.5 $ ] ✈️باحتساب رسوم الشحن", content)
        self.assertIn("كوبون 5$ للطلبات فوق 30$", content)
        self.assertIn("رابط عملات:\nhttps://affiliate/coins", content)
        self.assertIn("رابط مباشر:\nhttps://affiliate/direct", content)
        self.assertIn("https://offres365page.up.railway.app/", content)
        self.assertIn("https://t.me/rabahcopons/7366", content)
        self.assertNotIn("s.click.aliexpress.com", content)

    def test_does_not_add_shipping_lines_without_a_named_country(self):
        text = (
            "منتج ممتاز بسعر 12.50$\n"
            "شحن مجاني\n"
            "رابط المنتج:\n"
            "https://s.click.aliexpress.com/e/_c3tbmYUP\n"
        )
        content = _build_channel_content(
            text,
            {
                "title": "منتج ممتاز",
                "price": 12.5,
                "currency": "$",
                "shipping_line_numbers": [2],
                "coupon_line_numbers": [],
                "link_sections": [{"line_numbers": [3, 4]}],
            },
            ["https://s.click.aliexpress.com/e/_c3tbmYUP"],
            {
                "https://s.click.aliexpress.com/e/_c3tbmYUP":
                    "https://affiliate/product",
            },
        )

        self.assertNotIn("شحن مجاني", content)

    def test_keeps_shipping_lines_when_a_country_is_known(self):
        text = (
            "منتج ممتاز بسعر 12.50$\n"
            "الشحن متوفر إلى الجزائر\n"
            "رابط المنتج:\n"
            "https://s.click.aliexpress.com/e/_c3tbmYUP\n"
        )
        content = _build_channel_content(
            text,
            {
                "title": "منتج ممتاز",
                "price": 12.5,
                "currency": "$",
                "shipping_country": "Algeria",
                "shipping_line_numbers": [2],
                "coupon_line_numbers": [],
                "link_sections": [{"line_numbers": [3, 4]}],
            },
            ["https://s.click.aliexpress.com/e/_c3tbmYUP"],
            {
                "https://s.click.aliexpress.com/e/_c3tbmYUP":
                    "https://affiliate/product",
            },
        )

        self.assertIn("الشحن متوفر إلى الجزائر", content)


class ChannelProcessorAffiliateTests(unittest.IsolatedAsyncioTestCase):
    def _batch_candidate(self, post_id):
        return {
            "post_id": post_id,
            "message": SimpleNamespace(id=post_id),
            "channel_id": -100123,
            "channel_username": "offers",
            "text": (
                f"عرض {post_id} "
                "https://www.aliexpress.com/item/123.html"
            ),
            "urls": ["https://www.aliexpress.com/item/123.html"],
        }

    @staticmethod
    def _offer_analysis():
        return ChannelAnalysis(
            {
                "is_offer": True,
                "title": "Product",
                "price": 10,
                "currency": "$",
                "category": "phones",
                "coupon_line_numbers": [],
                "link_sections": [],
            },
            ai_provider="Gemini",
        )

    async def test_non_offer_deletes_processing_post(self):
        analysis = self._offer_analysis()
        analysis["is_offer"] = False
        with patch(
            "core.channel_processor.delete_channel_post_if_status",
            return_value=True,
        ) as delete_post:
            result = await _apply_channel_analysis(
                self._batch_candidate(1),
                analysis,
            )

        self.assertIsNone(result)
        delete_post.assert_called_once_with(1, "processing")

    async def test_non_offer_returns_processing_post_to_pending_when_delete_fails(self):
        analysis = self._offer_analysis()
        analysis["is_offer"] = False
        with (
            patch(
                "core.channel_processor.delete_channel_post_if_status",
                return_value=False,
            ),
            patch(
                "core.channel_processor.mark_channel_post_pending",
                return_value=True,
            ) as mark_pending,
        ):
            result = await _apply_channel_analysis(
                self._batch_candidate(1),
                analysis,
            )

        self.assertIsNone(result)
        mark_pending.assert_called_once_with(1)

    @patch("core.channel_processor.create_channel_post")
    @patch("core.channel_processor.extract_aliexpress_urls", return_value=[])
    async def test_register_skips_new_posts_without_aliexpress_links(
        self,
        extract_urls,
        create_post,
    ):
        result = await register_channel_message(
            SimpleNamespace(
                message="إعلان عام بدون رابط شراء",
                raw_text="إعلان عام بدون رابط شراء",
                id=42,
            ),
            channel_id=-100123,
        )

        self.assertIsNone(result)
        extract_urls.assert_called_once()
        create_post.assert_not_called()

    async def test_affiliate_failure_returns_processing_post_to_pending(self):
        with (
            patch(
                "core.channel_processor._generate_affiliate_replacements",
                new=AsyncMock(side_effect=RuntimeError("affiliate")),
            ),
            patch(
                "core.channel_processor.mark_channel_post_pending",
                return_value=True,
            ) as mark_pending,
        ):
            result = await _apply_channel_analysis(
                self._batch_candidate(2),
                self._offer_analysis(),
            )

        self.assertIsNone(result)
        mark_pending.assert_called_once_with(2)

    async def test_category_failure_returns_processing_post_to_pending(self):
        with (
            patch(
                "core.channel_processor._generate_affiliate_replacements",
                new=AsyncMock(return_value={}),
            ),
            patch("core.channel_processor.extract_product_id", return_value="123"),
            patch(
                "core.channel_processor.get_product_category_from_api",
                side_effect=RuntimeError("category"),
            ),
            patch(
                "core.channel_processor.mark_channel_post_pending",
                return_value=True,
            ) as mark_pending,
        ):
            result = await _apply_channel_analysis(
                self._batch_candidate(3),
                self._offer_analysis(),
            )

        self.assertIsNone(result)
        mark_pending.assert_called_once_with(3)

    async def test_persistence_failure_returns_processing_post_to_pending(self):
        with (
            patch(
                "core.channel_processor._generate_affiliate_replacements",
                new=AsyncMock(return_value={}),
            ),
            patch("core.channel_processor.extract_product_id", return_value="123"),
            patch(
                "core.channel_processor.get_product_category_from_api",
                return_value=None,
            ),
            patch("core.channel_processor.update_channel_post", return_value=False),
            patch(
                "core.channel_processor.mark_channel_post_pending",
                return_value=True,
            ) as mark_pending,
        ):
            result = await _apply_channel_analysis(
                self._batch_candidate(4),
                self._offer_analysis(),
            )

        self.assertIsNone(result)
        mark_pending.assert_called_once_with(4)

    async def test_four_piece_normal_offer_is_processed_instead_of_deleted(self):
        analysis = self._offer_analysis()
        analysis.update({
            "offer_type": "normal",
            "bundle_quantity": 4,
        })
        with (
            patch(
                "core.channel_processor._generate_affiliate_replacements",
                new=AsyncMock(return_value={}),
            ),
            patch("core.channel_processor.extract_product_id", return_value="123"),
            patch(
                "core.channel_processor.get_product_category_from_api",
                return_value=None,
            ),
            patch("core.channel_processor.update_channel_post") as update_post,
        ):
            result = await _apply_channel_analysis(
                self._batch_candidate(5),
                analysis,
            )

        self.assertEqual(result, 5)
        self.assertEqual(update_post.call_args.kwargs["offer_type"], "normal")

    async def test_legacy_discard_classification_is_saved_as_normal(self):
        analysis = self._offer_analysis()
        analysis.update({
            "offer_type": "discard",
            "bundle_quantity": 4,
        })
        with (
            patch(
                "core.channel_processor._generate_affiliate_replacements",
                new=AsyncMock(return_value={}),
            ),
            patch("core.channel_processor.extract_product_id", return_value="123"),
            patch(
                "core.channel_processor.get_product_category_from_api",
                return_value=None,
            ),
            patch("core.channel_processor.update_channel_post") as update_post,
        ):
            result = await _apply_channel_analysis(
                self._batch_candidate(6),
                analysis,
            )

        self.assertEqual(result, 6)
        self.assertEqual(update_post.call_args.kwargs["offer_type"], "normal")

    async def test_uses_final_title_for_content_persistence_and_category(self):
        analysis = self._offer_analysis()
        analysis["title"] = "1:16 Scale Remote Control Monster Truck RC Car"
        with (
            patch(
                "core.channel_processor._generate_affiliate_replacements",
                new=AsyncMock(return_value={}),
            ),
            patch("core.channel_processor.extract_product_id", return_value="123"),
            patch(
                "core.channel_processor.get_product_category_from_api",
                return_value=None,
            ),
            patch(
                "core.channel_processor.classify_product_category_from_title",
                return_value={
                    "category": "gaming",
                    "first_level_category_name": "Consumer Electronics",
                    "second_level_category_name": "Video Games",
                },
            ) as classify_title,
            patch("core.channel_processor.update_channel_post") as update_post,
        ):
            result = await _apply_channel_analysis(
                self._batch_candidate(7),
                analysis,
            )

        self.assertEqual(result, 7)
        classify_title.assert_called_once_with(
            "1:16 Scale Remote Control Monster Truck RC Car",
            "channel",
        )
        self.assertEqual(
            update_post.call_args.kwargs["title"],
            "1:16 Scale Remote Control Monster Truck RC Car",
        )
        self.assertIn(
            "🔥 تخفيض على  1:16 Scale Remote Control Monster Truck RC Car",
            update_post.call_args.kwargs["content"],
        )

    async def test_processes_five_channel_posts_from_one_ai_batch(self):
        candidates = [self._batch_candidate(post_id) for post_id in range(1, 6)]
        analyses = [
            ChannelAnalysis(
                {
                    "is_offer": True,
                    "title": f"Product {post_id}",
                    "price": post_id,
                    "currency": "$",
                    "category": "phones",
                    "coupon_line_numbers": [],
                    "link_sections": [],
                },
                ai_provider="Gemini",
            )
            for post_id in range(1, 6)
        ]
        with (
            patch(
                "core.channel_processor.analyze_channel_posts",
                return_value=analyses,
            ) as analyze_batch,
            patch(
                "core.channel_processor._generate_affiliate_replacements",
                new=AsyncMock(return_value={}),
            ),
            patch("core.channel_processor.extract_product_id", return_value="123"),
            patch(
                "core.channel_processor.get_product_category_from_api",
                return_value=None,
            ),
            patch("core.channel_processor.update_channel_post") as update_post,
        ):
            result = await process_channel_messages(candidates)

        self.assertEqual(result, [1, 2, 3, 4, 5])
        analyze_batch.assert_called_once()
        self.assertEqual(
            [item["post_id"] for item in analyze_batch.call_args.args[0]],
            [1, 2, 3, 4, 5],
        )
        self.assertEqual(update_post.call_count, 5)
        self.assertTrue(
            all(
                call.kwargs["processing_status"] == "processed"
                for call in update_post.call_args_list
            )
        )
        self.assertTrue(
            all(
                call.kwargs["processed_started_at"] == "CURRENT_TIMESTAMP"
                for call in update_post.call_args_list
            )
        )

    async def test_batch_ai_failure_keeps_every_post_pending(self):
        candidates = [self._batch_candidate(post_id) for post_id in range(1, 6)]
        with (
            patch(
                "core.channel_processor.analyze_channel_posts",
                side_effect=GeminiError("provider unavailable"),
            ),
            patch(
                "core.channel_processor.mark_channel_post_pending",
                return_value=True,
            ) as mark_pending,
        ):
            result = await process_channel_messages(candidates)

        self.assertEqual(result, [])
        self.assertTrue(result.retryable_failure)
        self.assertEqual(mark_pending.call_count, 5)

    async def test_every_processing_failure_returns_to_pending(self):
        candidates = [self._batch_candidate(post_id) for post_id in range(1, 6)]
        with (
            patch(
                "core.channel_processor.analyze_channel_posts",
                side_effect=GeminiError("provider unavailable"),
            ),
            patch(
                "core.channel_processor.mark_channel_post_pending",
                return_value=True,
            ) as mark_pending,
        ):
            result = await process_channel_messages(candidates)

        self.assertEqual(result, [])
        self.assertTrue(result.retryable_failure)
        self.assertEqual(
            [call.args[0] for call in mark_pending.call_args_list],
            [1, 2, 3, 4, 5],
        )

    async def test_requeues_unmatched_item_and_processes_valid_posts(self):
        candidates = [
            self._batch_candidate(post_id)
            for post_id in range(1, 6)
        ]
        analyses = [
            ChannelAnalysis(
                {
                    "is_offer": True,
                    "title": f"Product {post_id}",
                    "price": post_id,
                    "currency": "$",
                    "category": "phones",
                    "coupon_line_numbers": [],
                    "link_sections": [],
                },
                ai_provider="Groq",
            )
            for post_id in (1, 2, 4, 5)
        ]
        for post_id, analysis in zip((1, 2, 4, 5), analyses):
            analysis.channel_post_id = post_id
        with (
            patch(
                "core.channel_processor.analyze_channel_posts",
                return_value=ChannelBatchAnalysis(analyses),
            ) as analyze_batch,
            patch(
                "core.channel_processor._generate_affiliate_replacements",
                new=AsyncMock(return_value={}),
            ),
            patch("core.channel_processor.extract_product_id", return_value="123"),
            patch(
                "core.channel_processor.get_product_category_from_api",
                return_value=None,
            ),
            patch("core.channel_processor.update_channel_post") as update_post,
            patch(
                "core.channel_processor.mark_channel_post_pending",
                return_value=True,
            ) as mark_pending,
        ):
            result = await process_channel_messages(candidates)

        self.assertEqual(result, [1, 2, 4, 5])
        analyze_batch.assert_called_once()
        statuses_by_post = {
            call.args[0]: call.kwargs["processing_status"]
            for call in update_post.call_args_list
        }
        self.assertEqual(
            {
                post_id: statuses_by_post[post_id]
                for post_id in (1, 2, 4, 5)
            },
            {post_id: "processed" for post_id in (1, 2, 4, 5)},
        )
        mark_pending.assert_called_once_with(3)
        self.assertTrue(result.retryable_failure)

    async def test_unusable_batch_response_requeues_without_singleton_fallback(self):
        candidates = [self._batch_candidate(post_id) for post_id in range(1, 6)]
        with (
            patch(
                "core.channel_processor.analyze_channel_posts",
                side_effect=ChannelBatchResponseError("invalid response"),
            ),
            patch(
                "core.channel_processor.mark_channel_post_pending",
                return_value=True,
            ) as mark_pending,
        ):
            result = await process_channel_messages(candidates)

        self.assertEqual(result, [])
        self.assertTrue(result.retryable_failure)
        self.assertEqual(mark_pending.call_count, 5)

    @patch("core.channel_processor.update_channel_post")
    @patch("core.channel_processor.create_channel_post")
    @patch("core.channel_processor.extract_aliexpress_urls")
    @patch("core.channel_processor.is_feature_enabled", return_value=True)
    async def test_prepare_atomically_claims_existing_pending_post(
        self,
        feature_enabled,
        extract_urls,
        create_post,
        update_post,
    ):
        extract_urls.return_value = [
            "https://www.aliexpress.com/item/123.html"
        ]
        message = SimpleNamespace(
            message="عرض https://www.aliexpress.com/item/123.html",
            raw_text="",
            id=42,
            date=None,
        )

        candidate = await prepare_channel_message(
            message,
            channel_id=-100123,
            existing_post_id=77,
        )

        self.assertEqual(candidate["post_id"], 77)
        create_post.assert_not_called()
        update_post.assert_called_once()
        self.assertEqual(update_post.call_args.kwargs["expected_status"], "pending")
        self.assertEqual(
            update_post.call_args.kwargs["processing_status"],
            "processing",
        )

    async def test_persists_official_category_names_from_product_api(self):
        message = SimpleNamespace(
            message="Redmi offer https://www.aliexpress.com/item/123.html",
            raw_text="",
            id=42,
            date=None,
        )
        analysis = ChannelAnalysis(
            {
                "is_offer": True,
                "title": "Redmi Phone",
                "price": 125,
                "currency": "$",
                "category": "phones",
                "coupon_line_numbers": [],
                "link_sections": [],
            },
            ai_provider="Groq",
        )
        official_category = {
            "category": "accessories",
            "first_level_category_name": "Phones & Telecommunications",
            "second_level_category_name": "Mobile Phone Accessories",
        }
        with (
            patch("core.channel_processor.is_feature_enabled", return_value=True),
            patch(
                "core.channel_processor.extract_aliexpress_urls",
                return_value=["https://www.aliexpress.com/item/123.html"],
            ),
            patch("core.channel_processor.create_channel_post", return_value=7),
            patch(
                "core.channel_processor.analyze_channel_posts",
                return_value=[analysis],
            ),
            patch(
                "core.channel_processor._generate_affiliate_replacements",
                new=AsyncMock(return_value={}),
            ),
            patch("core.channel_processor.extract_product_id", return_value="123"),
            patch(
                "core.channel_processor.get_product_category_from_api",
                return_value=official_category,
            ) as get_category,
            patch(
                "core.channel_processor.classify_product_category_from_title"
            ) as classify_title,
            patch("core.channel_processor.update_channel_post") as update_post,
        ):
            result = await _apply_channel_analysis(
                self._batch_candidate(7),
                analysis,
            )

        self.assertEqual(result, 7)
        get_category.assert_called_once_with("123")
        saved = update_post.call_args.kwargs
        self.assertEqual(saved["category"], "accessories")
        self.assertEqual(
            saved["first_level_category_name"],
            "Phones & Telecommunications",
        )
        self.assertEqual(
            saved["second_level_category_name"],
            "Mobile Phone Accessories",
        )
        self.assertEqual(saved["category_source"], "aliexpress_api")
        self.assertEqual(saved["source_analize"], "groq")
        classify_title.assert_not_called()

    async def test_persists_ai_category_fallback_when_product_api_fails(self):
        message = SimpleNamespace(
            message="Redmi offer https://www.aliexpress.com/item/123.html",
            raw_text="",
            id=42,
            date=None,
        )
        analysis = ChannelAnalysis(
            {
                "is_offer": True,
                "title": "iPhone 15 case with screen protector",
                "price": 125,
                "currency": "$",
                "category": "phones",
                "coupon_line_numbers": [],
                "link_sections": [],
            },
            ai_provider="Gemini",
        )
        with (
            patch("core.channel_processor.is_feature_enabled", return_value=True),
            patch(
                "core.channel_processor.extract_aliexpress_urls",
                return_value=["https://www.aliexpress.com/item/123.html"],
            ),
            patch("core.channel_processor.create_channel_post", return_value=7),
            patch(
                "core.channel_processor.analyze_channel_posts",
                return_value=[analysis],
            ),
            patch(
                "core.channel_processor._generate_affiliate_replacements",
                new=AsyncMock(return_value={}),
            ),
            patch("core.channel_processor.extract_product_id", return_value="123"),
            patch(
                "core.channel_processor.get_product_category_from_api",
                return_value=None,
            ),
            patch(
                "core.channel_processor.classify_product_category_from_title",
                return_value={
                    "category": "accessories",
                    "first_level_category_name": "Phones & Telecommunications",
                    "second_level_category_name": "Mobile Phone Accessories",
                    "provider": "Groq",
                },
            ) as classify_title,
            patch("core.channel_processor.update_channel_post") as update_post,
        ):
            result = await _apply_channel_analysis(
                self._batch_candidate(7),
                analysis,
            )

        self.assertEqual(result, 7)
        saved = update_post.call_args.kwargs
        self.assertEqual(saved["category"], "accessories")
        self.assertEqual(
            saved["first_level_category_name"],
            "Phones & Telecommunications",
        )
        self.assertEqual(
            saved["second_level_category_name"],
            "Mobile Phone Accessories",
        )
        self.assertEqual(saved["category_source"], "groq")
        self.assertEqual(saved["source_analize"], "gemini")
        classify_title.assert_called_once_with(
            "iPhone 15 case with screen protector",
            "channel",
        )

    async def test_persists_special_category_label_for_smartwatch_offer(self):
        analysis = ChannelAnalysis(
            {
                "is_offer": True,
                "title": "Amazfit Smart Watch GPS",
                "price": 40,
                "currency": "$",
                "category": "smart_watches",
                "coupon_line_numbers": [],
                "link_sections": [],
            },
            ai_provider="Groq",
        )
        with (
            patch(
                "core.channel_processor._generate_affiliate_replacements",
                new=AsyncMock(return_value={}),
            ),
            patch("core.channel_processor.extract_product_id", return_value="123"),
            patch(
                "core.channel_processor.get_product_category_from_api",
                return_value={
                    "category": "watches",
                    "first_level_category_name": "Watches",
                    "second_level_category_name": "Smart Watches",
                },
            ),
            patch("core.channel_processor.update_channel_post") as update_post,
        ):
            result = await _apply_channel_analysis(
                self._batch_candidate(9),
                analysis,
            )

        self.assertEqual(result, 9)
        saved = update_post.call_args.kwargs
        self.assertEqual(saved["category"], "smart_watches")
        self.assertEqual(saved["category_source"], "groq")
        self.assertEqual(saved["second_level_category_name"], "Smart Watches")

    async def test_persists_special_category_label_for_bluetooth_offer(self):
        analysis = ChannelAnalysis(
            {
                "is_offer": True,
                "title": "Bluetooth Wireless Earbuds TWS",
                "price": 18,
                "currency": "$",
                "category": "bluetooth_headphones",
                "coupon_line_numbers": [],
                "link_sections": [],
            },
            ai_provider="Groq",
        )
        with (
            patch(
                "core.channel_processor._generate_affiliate_replacements",
                new=AsyncMock(return_value={}),
            ),
            patch("core.channel_processor.extract_product_id", return_value="123"),
            patch(
                "core.channel_processor.get_product_category_from_api",
                return_value={
                    "category": "headphones",
                    "first_level_category_name": "Consumer Electronics",
                    "second_level_category_name": "Earphones & Headphones",
                },
            ),
            patch("core.channel_processor.update_channel_post") as update_post,
        ):
            result = await _apply_channel_analysis(
                self._batch_candidate(10),
                analysis,
            )

        self.assertEqual(result, 10)
        saved = update_post.call_args.kwargs
        self.assertEqual(saved["category"], "bluetooth_headphones")
        self.assertEqual(saved["category_source"], "groq")
        self.assertEqual(
            saved["second_level_category_name"],
            "Earphones & Headphones",
        )

    async def test_title_category_fallback_failure_keeps_existing_ai_category(self):
        analysis = self._offer_analysis()
        with (
            patch(
                "core.channel_processor._generate_affiliate_replacements",
                new=AsyncMock(return_value={}),
            ),
            patch("core.channel_processor.extract_product_id", return_value="123"),
            patch(
                "core.channel_processor.get_product_category_from_api",
                return_value=None,
            ),
            patch(
                "core.channel_processor.classify_product_category_from_title",
                side_effect=GeminiError("fallback unavailable"),
            ),
            patch("core.channel_processor.update_channel_post") as update_post,
        ):
            result = await _apply_channel_analysis(
                self._batch_candidate(8),
                analysis,
            )

        self.assertEqual(result, 8)
        saved = update_post.call_args.kwargs
        self.assertEqual(saved["category"], "phones")
        self.assertEqual(saved["category_source"], "gemini")
        self.assertEqual(saved["second_level_category_name"], "Mobile Phones")

    @patch("core.channel_processor.generate_affiliate_link")
    async def test_generates_one_link_for_each_unique_source_url(self, generate):
        from core.channel_processor import _generate_affiliate_replacements

        generate.side_effect = lambda url: f"affiliate:{url.rsplit('/', 1)[-1]}"
        result = await _generate_affiliate_replacements([
            "https://a.aliexpress.com/one",
            "https://a.aliexpress.com/two",
        ])
        self.assertEqual(
            result,
            {
                "https://a.aliexpress.com/one": "affiliate:one",
                "https://a.aliexpress.com/two": "affiliate:two",
            },
        )

    @patch("core.channel_processor.create_channel_post")
    @patch("core.channel_processor.extract_aliexpress_urls", return_value=[])
    async def test_skips_posts_without_aliexpress_links(
        self,
        extract_urls,
        create_post,
    ):
        result = await prepare_channel_message(
            SimpleNamespace(
                message="إعلان عام بدون رابط شراء",
                raw_text="إعلان عام بدون رابط شراء",
                id=42,
            ),
            channel_id=-100123,
        )

        self.assertIsNone(result)
        extract_urls.assert_called_once()
        create_post.assert_not_called()

    async def test_quota_recovery_keeps_channel_post_pending(self):
        candidates = [self._batch_candidate(post_id) for post_id in range(1, 6)]
        with (
            patch(
                "core.channel_processor.analyze_channel_posts",
                side_effect=GeminiRateLimitError("provider reset scheduled"),
            ),
            patch(
                "core.channel_processor.mark_channel_post_pending",
                return_value=True,
            ) as mark_pending,
        ):
            result = await process_channel_messages(candidates)

        self.assertEqual(result, [])
        self.assertTrue(result.retryable_failure)
        self.assertEqual(mark_pending.call_count, 5)

    async def test_malformed_ai_response_keeps_channel_post_pending(self):
        candidates = [self._batch_candidate(post_id) for post_id in range(1, 6)]
        with (
            patch(
                "core.channel_processor.analyze_channel_posts",
                side_effect=GeminiError("invalid provider response"),
            ),
            patch(
                "core.channel_processor.mark_channel_post_pending",
                return_value=True,
            ) as mark_pending,
        ):
            result = await process_channel_messages(candidates)

        self.assertEqual(result, [])
        self.assertTrue(result.retryable_failure)
        self.assertEqual(mark_pending.call_count, 5)

if __name__ == "__main__":
    unittest.main()
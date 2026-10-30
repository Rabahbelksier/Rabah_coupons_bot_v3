import os
import threading
import time
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

for name in ("APP_KEY", "APP_SECRET", "TRACKING_ID", "TELEGRAM_TOKEN"):
    os.environ.setdefault(name, "test")

from core.ai_service import (
    AIResponse,
    AI_PROVIDER_FEATURE_KEYS,
    AIProviderConfigurationError,
    AIProviderError,
    AIProviderQuotaError,
    AIProviderTransientError,
    AIService,
    AIServiceUnavailableError,
    _OpenAICompatibleProvider,
    _CallableProvider,
    _cloudflare_endpoint,
    _is_configuration_error_response,
    _next_daily_reset,
    _parse_json_object,
    _retry_after_timestamp,
)
from config import (
    AI_RATE_LIMIT_DEFAULTS,
    CLOUDFLARE_DEFAULT_MODEL,
    DEEPSEEK_MODEL,
)


class _FakeProvider:
    def __init__(self, name, outcomes, configured=True):
        self.name = name
        self.model = {
            "Gemini": "gemini-3.5-flash-lite",
            "Groq": "qwen/qwen3.8-27b",
            "Cloudflare": CLOUDFLARE_DEFAULT_MODEL,
            "OpenRouter": "openrouter/free",
            "DeepSeek": DEEPSEEK_MODEL,
        }.get(name)
        self.configured = configured
        self.can_attempt = configured
        self.outcomes = list(outcomes)
        self.calls = 0

    def generate_json(self, _prompt):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class AIServiceTests(unittest.TestCase):
    def test_gemini_context_adds_closed_vocabulary_instruction(self):
        calls = []
        provider = _CallableProvider(
            "Gemini",
            lambda prompt, context=None: (
                calls.append((prompt, context)) or {"ok": True}
            ),
            lambda: True,
            lambda: "gemini-test",
            (),
            (),
            request_context_loader=lambda _kind: {
                "cached_content": "cachedContents/test",
                "allowed_keywords": ["smart_scale"],
            },
        )

        result = provider.generate_json("حلل نية البحث", request_kind="user")

        self.assertEqual(result, {"ok": True})
        self.assertIn("قاموس البحث المغلق", calls[0][0])
        self.assertIn("مهمة مطابقة عبارات كاش المنتجات", calls[0][0])
        self.assertIn("لا تشترط تطابق الكلمات حرفيًا", calls[0][0])
        self.assertIn("أيًا كانت اللغة أو الكتابة", calls[0][0])
        self.assertIn("قارن هوية المنتج لا مجرد الترابط", calls[0][0])
        self.assertIn("الملحق أو قطعة الغيار", calls[0][0])
        self.assertIn("عبارة كاملة واحدة من القاموس", calls[0][0])
        self.assertIn("الشرطة السفلية في العبارات القديمة", calls[0][0])
        self.assertIn("الواصلة الأصلية", calls[0][0])
        self.assertIn("keyword_groups", calls[0][0])
        self.assertIn("موجود داخل CachedContent", calls[0][0])
        self.assertIn("category_filter_requested", calls[0][0])
        self.assertEqual(calls[0][1]["allowed_keywords"], ["smart_scale"])

    def test_gemini_category_context_uses_its_own_cached_vocabulary(self):
        calls = []
        provider = _CallableProvider(
            "Gemini",
            lambda prompt, context=None: (
                calls.append((prompt, context)) or {"ok": True}
            ),
            lambda: True,
            lambda: "gemini-test",
            (),
            (),
            request_context_loader=lambda kind: (
                {
                    "cached_content": "cachedContents/categories",
                    "allowed_official_categories": [
                        "Mobile Phones",
                        "Laptops",
                    ],
                }
                if kind == "user_category"
                else None
            ),
        )

        result = provider.generate_json(
            "اختر الفئات",
            request_kind="user_category",
        )

        self.assertEqual(result, {"ok": True})
        self.assertIn("قاموس الفئات الرسمي المغلق", calls[0][0])
        self.assertIn("موجود داخل CachedContent", calls[0][0])
        self.assertNotIn("قاموس البحث المغلق", calls[0][0])
        self.assertEqual(
            calls[0][1]["allowed_official_categories"],
            ["Mobile Phones", "Laptops"],
        )

    def test_gemini_inline_phrase_vocabulary_displays_spaces(self):
        calls = []
        provider = _CallableProvider(
            "Gemini",
            lambda prompt, context=None: (
                calls.append((prompt, context)) or {"ok": True}
            ),
            lambda: True,
            lambda: "gemini-test",
            (),
            (),
            request_context_loader=lambda _kind: {
                "allowed_keywords": [
                    "smart_scale",
                    "car-code-reader",
                    "SSD_M.2 2280",
                    "SSD_M.2-2280",
                ],
            },
        )

        provider.generate_json("حلل نية البحث", request_kind="user")

        self.assertIn("- smart scale", calls[0][0])
        self.assertIn("- car-code-reader", calls[0][0])
        self.assertIn("- SSD_M.2 2280", calls[0][0])
        self.assertIn("- SSD_M.2-2280", calls[0][0])
        self.assertNotIn("- smart_scale", calls[0][0])
        self.assertNotIn("- car code reader", calls[0][0])
        self.assertNotIn("- SSD M.2 2280", calls[0][0])

    def test_gemini_tier1_limits_are_configured(self):
        self.assertEqual(
            {
                metric: AI_RATE_LIMIT_DEFAULTS["gemini"][metric]
                for metric in ("rpm", "tpm", "rpd")
            },
            {
                "rpm": 4_000,
                "tpm": 4_000_000,
                "rpd": 150_000,
            },
        )

    def test_deepseek_does_not_assume_fixed_quota_limits(self):
        self.assertEqual(
            AI_RATE_LIMIT_DEFAULTS["deepseek"],
            {
                "rpm": None,
                "tpm": None,
                "rpd": None,
                "tpd": None,
                "npd": None,
            },
        )

    def test_default_provider_order_inserts_deepseek_after_gemini(self):
        service = self._service()

        self.assertEqual(
            [provider.name for provider in service._providers],
            ["Groq", "OpenRouter", "Gemini", "DeepSeek", "Cloudflare"],
        )
        self.assertEqual(
            AI_PROVIDER_FEATURE_KEYS["DeepSeek"],
            "ai_provider_deepseek",
        )

    @patch("core.ai_service.requests.post")
    def test_deepseek_accepts_all_gemini_request_kinds(self, post):
        service = self._service()
        seen_request_kinds = []
        provider = _OpenAICompatibleProvider(
            "DeepSeek",
            "test-key",
            "deepseek-flash",
            "https://api.deepseek.com/chat/completions",
            request_context_loader=lambda kind: (
                seen_request_kinds.append(kind)
                or {"system_prompt": "JSON only"}
            ),
        )
        response = unittest.mock.Mock()
        response.status_code = 200
        response.ok = True
        response.headers = {}
        response.json.return_value = {
            "choices": [{"message": {"content": '{"ok":true}'}}],
            "usage": {},
        }
        post.return_value = response
        service._providers = (provider,)

        for request_kind in ("channel", "user", "user_category"):
            service.generate_json("prompt", request_kind=request_kind)

        self.assertEqual(
            seen_request_kinds,
            ["channel", "user", "user_category"],
        )
        self.assertEqual(post.call_count, 3)

    def test_disabled_deepseek_is_skipped(self):
        service = self._service()
        provider = _FakeProvider("DeepSeek", [{"ok": True}])
        service._providers = (provider,)
        service.set_provider_enabled("DeepSeek", False)

        with self.assertRaises(AIServiceUnavailableError):
            service.generate_json("prompt")

        self.assertEqual(provider.calls, 0)

    @patch("core.ai_service.time.time", return_value=1_000)
    def test_local_exhaustion_skips_provider_until_reset(self, _now):
        service = self._service()
        gemini = _FakeProvider("Gemini", [{"provider": "Gemini"}])
        groq = _FakeProvider("Groq", [{"provider": "Groq"}])
        service._providers = (gemini, groq)

        with service._lock:
            service._ensure_default_limits_locked(gemini)
            service._usage_state["Gemini"]["requests_minute"] = 4_000
            service._usage_windows["Gemini"]["minute_reset_at"] = 1_300
            service._rate_state["Gemini"]["rpm"]["remaining"] = 0
            service._rate_state["Gemini"]["rpm"]["reset_at"] = 1_300

        result = service.generate_json("prompt")

        self.assertEqual(result.provider, "Groq")
        self.assertEqual(gemini.calls, 0)
        self.assertEqual(groq.calls, 1)
        status = next(
            row
            for row in service.status()["providers"]
            if row["name"] == "Gemini"
        )
        self.assertFalse(status["available"])
        self.assertEqual(status["next_reset_seconds"], 300)

    @patch("core.ai_service.time.time")
    def test_short_usage_does_not_reset_while_below_limit(self, now):
        now.return_value = 1_000
        service = self._service()
        gemini = _FakeProvider(
            "Gemini",
            [AIResponse({"provider": "Gemini"}, usage={"total_tokens": 5})],
        )
        service._providers = (gemini,)

        service.generate_json("prompt")

        now.return_value = 1_061
        status = service.status()["providers"][0]
        limits = {
            row["metric"]: row
            for row in status["limits"]
        }
        self.assertEqual(status["usage"]["requests_minute"], 1)
        self.assertEqual(status["usage"]["tokens_minute"], 5)
        self.assertEqual(limits["rpm"]["remaining"], 3_999)
        self.assertEqual(limits["tpm"]["remaining"], 3_999_995)
        self.assertIsNone(limits["rpm"]["reset_at"])
        self.assertIsNone(limits["tpm"]["reset_at"])
        self.assertIsNone(status["usage_window"]["minute_reset_at"])
        self.assertIsNone(status["usage_window"]["tokens_minute_reset_at"])

    @patch("core.ai_service.time.time")
    def test_short_usage_resets_after_reaching_limit(self, now):
        now.return_value = 1_000
        service = self._service()
        gemini = _FakeProvider("Gemini", [])
        service._providers = (gemini,)

        with service._lock:
            service._ensure_default_limits_locked(gemini)
            service._rate_state["Gemini"]["rpm"]["remaining"] = 1
            service._rate_state["Gemini"]["tpm"]["remaining"] = 5
            service._record_attempt_locked(gemini)
            service._record_success_locked(
                gemini,
                AIResponse({"provider": "Gemini"}, usage={"total_tokens": 5}),
            )

        self.assertEqual(
            service._rate_state["Gemini"]["rpm"]["reset_at"],
            1_020,
        )
        self.assertEqual(
            service._rate_state["Gemini"]["tpm"]["reset_at"],
            1_020,
        )
        self.assertEqual(
            service._usage_windows["Gemini"]["minute_reset_at"],
            1_020,
        )
        self.assertEqual(
            service._usage_windows["Gemini"]["tokens_minute_reset_at"],
            1_020,
        )

        now.return_value = 1_021
        status = service.status()["providers"][0]
        limits = {
            row["metric"]: row
            for row in status["limits"]
        }
        self.assertEqual(status["usage"]["requests_minute"], 0)
        self.assertEqual(status["usage"]["tokens_minute"], 0)
        self.assertEqual(limits["rpm"]["remaining"], 4_000)
        self.assertEqual(limits["tpm"]["remaining"], 4_000_000)
        self.assertIsNone(limits["rpm"]["reset_at"])
        self.assertIsNone(limits["tpm"]["reset_at"])
        self.assertIsNone(status["usage_window"]["minute_reset_at"])
        self.assertIsNone(status["usage_window"]["tokens_minute_reset_at"])

    @patch("core.ai_service.time.time", return_value=1_000)
    def test_local_daily_exhaustion_skips_provider_until_reset(self, _now):
        service = self._service()
        gemini = _FakeProvider("Gemini", [{"provider": "Gemini"}])
        groq = _FakeProvider("Groq", [{"provider": "Groq"}])
        service._providers = (gemini, groq)

        with service._lock:
            service._ensure_default_limits_locked(gemini)
            service._usage_state["Gemini"]["requests_day"] = 150_000
            service._usage_windows["Gemini"]["daily_reset_at"] = 2_000
            service._rate_state["Gemini"]["rpd"]["remaining"] = 0
            service._rate_state["Gemini"]["rpd"]["reset_at"] = 2_000

        result = service.generate_json("prompt")

        self.assertEqual(result.provider, "Groq")
        self.assertEqual(gemini.calls, 0)
        self.assertEqual(groq.calls, 1)

    @patch("core.ai_service.time.sleep")
    @patch("core.ai_service.time.time", return_value=1_000)
    def test_groq_tpd_overage_success_keeps_provider_available(
        self,
        _now,
        sleep,
    ):
        service = self._service()
        groq = _FakeProvider(
            "Groq",
            [
                AIResponse(
                    {"provider": "Groq"},
                    usage={"total_tokens": 25},
                ),
            ],
        )
        service._providers = (groq,)

        with service._lock:
            service._ensure_default_limits_locked(groq)
            service._usage_state["Groq"]["tokens_day"] = 200_000
            service._usage_windows["Groq"]["tokens_reset_at"] = 2_000
            service._rate_state["Groq"]["tpd"]["remaining"] = 0
            service._rate_state["Groq"]["tpd"]["reset_at"] = 2_000
            service._rate_state["Groq"]["rpd"]["remaining"] = 999

        result = service.generate_json("prompt")

        self.assertEqual(result.provider, "Groq")
        self.assertEqual(groq.calls, 1)
        sleep.assert_not_called()
        self.assertEqual(
            service.status()["providers"][0]["usage"]["tokens_day"],
            200_025,
        )
        self.assertNotIn("Groq", service._unavailable)

    @patch("core.ai_service.time.sleep")
    @patch("core.ai_service.time.time", return_value=1_000)
    def test_groq_tpd_overage_retries_and_succeeds(
        self,
        _now,
        sleep,
    ):
        service = self._service()
        groq = _FakeProvider(
            "Groq",
            [
                AIProviderTransientError("temporary failure"),
                AIResponse(
                    {"provider": "Groq"},
                    usage={"total_tokens": 25},
                ),
            ],
        )
        service._providers = (groq,)

        with service._lock:
            service._ensure_default_limits_locked(groq)
            service._usage_state["Groq"]["tokens_day"] = 200_000
            service._usage_windows["Groq"]["tokens_reset_at"] = 2_000
            service._rate_state["Groq"]["tpd"]["remaining"] = 0
            service._rate_state["Groq"]["tpd"]["reset_at"] = 2_000
            service._rate_state["Groq"]["rpd"]["remaining"] = 999

        result = service.generate_json("prompt")

        self.assertEqual(result.provider, "Groq")
        self.assertEqual(groq.calls, 2)
        sleep.assert_called_once_with(60)
        self.assertEqual(
            service.status()["providers"][0]["usage"]["requests_day"],
            2,
        )
        self.assertEqual(
            service.status()["providers"][0]["usage"]["tokens_day"],
            200_025,
        )

    @patch("core.ai_service.time.sleep")
    @patch("core.ai_service.time.time", return_value=1_000)
    def test_groq_tpd_overage_failure_quarantines_and_falls_back(
        self,
        _now,
        sleep,
    ):
        service = self._service()
        groq = _FakeProvider(
            "Groq",
            [
                AIProviderTransientError("failure 1"),
                AIProviderTransientError("failure 2"),
                AIProviderTransientError("failure 3"),
            ],
        )
        fallback = _FakeProvider(
            "OpenRouter",
            [{"provider": "OpenRouter"}],
        )
        service._providers = (groq, fallback)

        with service._lock:
            service._ensure_default_limits_locked(groq)
            service._usage_state["Groq"]["tokens_day"] = 200_000
            service._usage_windows["Groq"]["tokens_reset_at"] = 2_000
            service._rate_state["Groq"]["tpd"]["remaining"] = 0
            service._rate_state["Groq"]["tpd"]["reset_at"] = 2_000
            service._rate_state["Groq"]["rpd"]["remaining"] = 999

        result = service.generate_json("prompt")

        self.assertEqual(result.provider, "OpenRouter")
        self.assertEqual(groq.calls, 3)
        self.assertEqual(fallback.calls, 1)
        self.assertEqual(sleep.call_args_list, [((60,),), ((60,),)])
        self.assertEqual(service._unavailable["Groq"]["kind"], "quota")
        self.assertEqual(
            service._rate_state["Groq"]["tpd"]["reset_at"],
            2_000,
        )

    @patch("core.ai_service.time.sleep")
    @patch("core.ai_service.time.time", return_value=1_000)
    def test_groq_tpd_overage_does_not_bypass_rpd_exhaustion(
        self,
        _now,
        sleep,
    ):
        service = self._service()
        groq = _FakeProvider("Groq", [])
        fallback = _FakeProvider(
            "OpenRouter",
            [{"provider": "OpenRouter"}],
        )
        service._providers = (groq, fallback)

        with service._lock:
            service._ensure_default_limits_locked(groq)
            service._usage_state["Groq"]["tokens_day"] = 200_000
            service._usage_state["Groq"]["requests_day"] = 1_000
            service._usage_windows["Groq"]["tokens_reset_at"] = 2_000
            service._usage_windows["Groq"]["daily_reset_at"] = 2_000
            service._rate_state["Groq"]["tpd"]["remaining"] = 0
            service._rate_state["Groq"]["tpd"]["reset_at"] = 2_000
            service._rate_state["Groq"]["rpd"]["remaining"] = 0
            service._rate_state["Groq"]["rpd"]["reset_at"] = 2_000

        result = service.generate_json("prompt")

        self.assertEqual(result.provider, "OpenRouter")
        self.assertEqual(groq.calls, 0)
        sleep.assert_not_called()

    def test_background_worker_resets_expired_window_without_provider_call(self):
        service = self._service()
        gemini = _FakeProvider("Gemini", [])
        service._providers = (gemini,)
        reset_at = time.time() + 0.05

        with service._lock:
            service._ensure_default_limits_locked(gemini)
            service._usage_state["Gemini"]["requests_minute"] = 4_000
            service._usage_windows["Gemini"]["minute_reset_at"] = reset_at
            service._rate_state["Gemini"]["rpm"]["remaining"] = 0
            service._rate_state["Gemini"]["rpm"]["reset_at"] = reset_at

        self.assertTrue(service.start_background_worker())
        try:
            deadline = time.time() + 2
            while time.time() < deadline:
                with service._lock:
                    available = "Gemini" not in service._unavailable
                    requests_minute = service._usage_state["Gemini"][
                        "requests_minute"
                    ]
                if available and requests_minute == 0:
                    break
                time.sleep(0.01)
            self.assertTrue(available)
            self.assertEqual(requests_minute, 0)
            self.assertEqual(gemini.calls, 0)
        finally:
            self.assertTrue(service.stop_background_worker())

    def test_background_worker_resets_disabled_provider_on_its_own_window(self):
        service = self._service()
        gemini = _FakeProvider("Gemini", [])
        groq = _FakeProvider("Groq", [])
        service._providers = (gemini, groq)
        service.set_provider_enabled(
            "Gemini",
            False,
            request_kind="user",
        )
        service.set_provider_enabled(
            "Gemini",
            False,
            request_kind="channel",
        )

        clock = {"now": 1_000}
        reset_times = {"Gemini": 1_002, "Groq": 1_005}
        saved_states = []
        first_reset_state = {}
        service._usage_state_saver = lambda state: (
            saved_states.append(deepcopy(state)) or True
        )

        with service._lock:
            for provider in service._providers:
                service._ensure_default_limits_locked(provider)
                name = provider.name
                snapshot = service._rate_state[name]["rpd"]
                limit = snapshot["limit"]
                service._usage_state[name]["requests_day"] = limit
                snapshot["remaining"] = 0
                snapshot["reset_at"] = reset_times[name]
                service._usage_windows[name]["daily_reset_at"] = (
                    reset_times[name]
                )
                service._unavailable[name] = {
                    "kind": "quota",
                    "eligible_at": reset_times[name],
                    "reason": "test quota",
                }

        class AdvancingStopEvent:
            def __init__(self):
                self.wait_count = 0

            def is_set(self):
                return False

            def wait(self, delay):
                self.wait_count += 1
                if self.wait_count == 2:
                    first_reset_state.update(deepcopy({
                        "usage": service._usage_state,
                        "rates": service._rate_state,
                        "windows": service._usage_windows,
                        "unavailable": service._unavailable,
                    }))
                if self.wait_count >= 3:
                    return True
                clock["now"] += delay
                return False

        service._quota_worker_stop = AdvancingStopEvent()
        with patch(
            "core.ai_service.time.time",
            side_effect=lambda: clock["now"],
        ):
            service._quota_window_loop()

        gemini_limit = service._rate_state["Gemini"]["rpd"]["limit"]
        groq_limit = service._rate_state["Groq"]["rpd"]["limit"]
        self.assertEqual(
            first_reset_state["usage"]["Gemini"]["requests_day"],
            0,
        )
        self.assertEqual(
            first_reset_state["rates"]["Gemini"]["rpd"]["remaining"],
            gemini_limit,
        )
        self.assertEqual(
            first_reset_state["rates"]["Gemini"]["rpd"]["reset_at"],
            _next_daily_reset("Gemini", reset_times["Gemini"]),
        )
        self.assertEqual(
            first_reset_state["usage"]["Groq"]["requests_day"],
            groq_limit,
        )
        self.assertEqual(
            first_reset_state["rates"]["Groq"]["rpd"]["reset_at"],
            reset_times["Groq"],
        )
        self.assertEqual(
            set(first_reset_state["unavailable"]),
            {"Groq"},
        )

        self.assertEqual(service._usage_state["Gemini"]["requests_day"], 0)
        self.assertEqual(service._usage_state["Groq"]["requests_day"], 0)
        self.assertEqual(service._unavailable, {})
        self.assertEqual(gemini.calls, 0)
        self.assertEqual(groq.calls, 0)
        self.assertEqual(
            [provider.name for provider in service._ordered_providers("user")],
            ["Gemini", "Groq"],
        )
        self.assertFalse(
            service._is_provider_enabled_locked("Gemini", "user")
        )
        self.assertTrue(
            service._is_provider_enabled_locked("Groq", "user")
        )

        persisted = saved_states[-1]
        for name in ("Gemini", "Groq"):
            expected_reset = _next_daily_reset(name, clock["now"])
            self.assertEqual(
                persisted["usage"][name]["requests_day"],
                0,
            )
            self.assertEqual(
                persisted["rates"][name]["rpd"]["remaining"],
                persisted["rates"][name]["rpd"]["limit"],
            )
            self.assertEqual(
                persisted["rates"][name]["rpd"]["reset_at"],
                expected_reset,
            )
            self.assertEqual(
                persisted["usage_windows"][name]["daily_reset_at"],
                expected_reset,
            )

        restored = self._service()
        restored._usage_state_loader = lambda: deepcopy(persisted)
        with patch(
            "core.ai_service.time.time",
            side_effect=lambda: clock["now"],
        ):
            with restored._lock:
                restored._load_persisted_state_locked()
                restored._refresh_expired_locked()
                for name in ("Gemini", "Groq"):
                    self.assertEqual(
                        restored._usage_state[name]["requests_day"],
                        0,
                    )
                    self.assertEqual(
                        restored._usage_windows[name]["daily_reset_at"],
                        persisted["usage_windows"][name]["daily_reset_at"],
                    )

    def test_background_worker_is_started_once_and_stops_cleanly(self):
        service = self._service()
        self.assertTrue(service.start_background_worker())
        first_thread = service._quota_worker_thread
        try:
            self.assertFalse(service.start_background_worker())
            self.assertIs(service._quota_worker_thread, first_thread)
        finally:
            self.assertTrue(service.stop_background_worker())
        self.assertIsNone(service._quota_worker_thread)

    def test_json_parser_accepts_prose_and_structured_content_parts(self):
        content = [
            {"type": "text", "text": "النتيجة:\n"},
            {"type": "text", "text": '```json\n{"ok": true}\n```'},
        ]

        self.assertEqual(
            _parse_json_object(content, "Test provider"),
            {"ok": True},
        )

    def test_json_parser_rejects_empty_non_text_content_as_provider_error(self):
        with self.assertRaises(AIProviderError):
            _parse_json_object([{"type": "image"}], "Test provider")

    @patch("core.ai_service.requests.post")
    def test_openai_compatible_providers_accept_channel_json_objects(self, post):
        response = unittest.mock.Mock()
        response.status_code = 200
        response.ok = True
        response.headers = {}
        response.json.return_value = {
            "choices": [{
                "message": {
                    "content": '{"posts": [{"post_id": "101"}]}',
                },
            }],
            "usage": {"total_tokens": 7},
            "model": "selected-model",
        }
        post.return_value = response

        for provider_name in ("Groq", "Cloudflare", "OpenRouter", "DeepSeek"):
            with self.subTest(provider=provider_name):
                provider = _OpenAICompatibleProvider(
                    provider_name,
                    "test-key",
                    "selected-model",
                    "https://example.test",
                )

                result = provider.generate_json("channel prompt")

                self.assertEqual(
                    result,
                    {"posts": [{"post_id": "101"}]},
                )
                self.assertEqual(result.model, "selected-model")
                self.assertEqual(result.usage["total_tokens"], 7)

    @patch("core.ai_service.requests.post")
    def test_deepseek_uses_json_mode_and_stable_context_messages(self, post):
        response = unittest.mock.Mock()
        response.status_code = 200
        response.ok = True
        response.headers = {}
        response.json.return_value = {
            "choices": [{
                "message": {"content": '{"request_type":"trending"}'},
            }],
            "usage": {
                "prompt_tokens": 12,
                "prompt_cache_hit_tokens": 8,
                "completion_tokens": 3,
            },
            "model": "deepseek-flash",
        }
        post.return_value = response
        provider = _OpenAICompatibleProvider(
            "DeepSeek",
            "test-key",
            "deepseek-flash",
            "https://api.deepseek.com/chat/completions",
            request_options={"thinking": {"type": "disabled"}},
            request_context_loader=lambda request_kind: {
                "system_prompt": "JSON policy\nAllowed phrases:\n- smart scale",
                "allowed_keywords": ["smart_scale"],
            },
        )

        result = provider.generate_json(
            "Analyze this request",
            request_kind="user",
        )

        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["model"], "deepseek-flash")
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertEqual(payload["messages"][0]["role"], "system")
        self.assertIn("Allowed phrases", payload["messages"][0]["content"])
        self.assertEqual(
            payload["messages"][1],
            {"role": "user", "content": "Analyze this request"},
        )
        self.assertNotIn("cached_content", payload)
        self.assertTrue(result.keyword_cache_active)
        self.assertEqual(result.keyword_cache_keywords, ["smart_scale"])
        self.assertEqual(result.usage["prompt_cache_hit_tokens"], 8)

    def test_deepseek_missing_key_is_unconfigured(self):
        provider = _OpenAICompatibleProvider(
            "DeepSeek",
            "",
            "deepseek-flash",
            "https://api.deepseek.com/chat/completions",
        )

        self.assertFalse(provider.configured)
        with self.assertRaises(AIProviderConfigurationError):
            provider.generate_json("prompt")

    @patch("core.ai_service.requests.post")
    def test_deepseek_429_uses_existing_quota_failover_classification(
        self,
        post,
    ):
        response = unittest.mock.Mock()
        response.status_code = 429
        response.ok = False
        response.headers = {"Retry-After": "45"}
        response.json.return_value = {
            "error": {"message": "Requests per minute rate limit exceeded"},
        }
        post.return_value = response
        provider = _OpenAICompatibleProvider(
            "DeepSeek",
            "test-key",
            "deepseek-flash",
            "https://api.deepseek.com/chat/completions",
        )

        with self.assertRaises(AIProviderQuotaError) as raised:
            provider.generate_json("prompt")

        self.assertTrue(raised.exception.limits)
        self.assertEqual(raised.exception.limits[0]["metric"], "rpm")

    @patch("core.ai_service.requests.post")
    def test_deepseek_insufficient_balance_is_quarantined_as_quota(self, post):
        response = unittest.mock.Mock()
        response.status_code = 402
        response.ok = False
        response.headers = {}
        response.json.return_value = {
            "error": {"message": "Insufficient Balance"},
        }
        post.return_value = response
        provider = _OpenAICompatibleProvider(
            "DeepSeek",
            "test-key",
            "deepseek-flash",
            "https://api.deepseek.com/chat/completions",
        )

        with self.assertRaises(AIProviderQuotaError) as raised:
            provider.generate_json("prompt")

        self.assertEqual(raised.exception.limits[0]["metric"], "unknown")
        self.assertFalse(raised.exception.limits[0]["exact"])

    @patch("core.ai_service.requests.post")
    def test_openai_compatible_providers_reject_missing_channel_json(
        self,
        post,
    ):
        response = unittest.mock.Mock()
        response.status_code = 200
        response.ok = True
        response.headers = {}
        post.return_value = response

        for provider_name, body in (
            (
                "Groq",
                {"choices": [{"message": {}}]},
            ),
            (
                "Cloudflare",
                {"choices": [{"message": {"content": None}}]},
            ),
            (
                "OpenRouter",
                {"choices": []},
            ),
            (
                "DeepSeek",
                {"choices": []},
            ),
        ):
            with self.subTest(provider=provider_name):
                response.json.return_value = body
                provider = _OpenAICompatibleProvider(
                    provider_name,
                    "test-key",
                    "selected-model",
                    "https://example.test",
                )

                with self.assertRaises(AIProviderError):
                    provider.generate_json("channel prompt")

    @patch("core.ai_service.time.time", return_value=1000)
    @patch("core.ai_service.requests.post")
    def test_groq_429_preserves_provider_recovery_details(self, post, _now):
        response = unittest.mock.Mock()
        response.status_code = 429
        response.ok = False
        response.headers = {
            "Retry-After": "30",
            "x-ratelimit-limit-requests": "1000",
            "x-ratelimit-remaining-requests": "0",
            "x-ratelimit-reset-requests": "30",
            "x-ratelimit-limit-tokens": "8000",
            "x-ratelimit-remaining-tokens": "0",
            "x-ratelimit-reset-tokens": "45",
        }
        response.json.return_value = {
            "error": {"message": "rate limit reached"},
        }
        post.return_value = response
        provider = _OpenAICompatibleProvider(
            "Groq",
            "test-key",
            "selected-model",
            "https://example.test",
        )

        with self.assertRaises(AIProviderQuotaError) as raised:
            provider.generate_json("channel prompt")

        limits = {
            limit["metric"]: limit
            for limit in raised.exception.limits
        }
        self.assertEqual(limits["rpm"]["limit"], 1000)
        self.assertEqual(limits["rpm"]["remaining"], 0)
        self.assertEqual(limits["tpm"]["limit"], 8000)
        self.assertEqual(limits["tpm"]["remaining"], 0)
        self.assertEqual(limits["rpm"]["reset_at"], 1030)
        self.assertEqual(limits["tpm"]["reset_at"], 1045)

    @patch("core.ai_service.time.time", return_value=1000)
    @patch("core.ai_service.requests.post")
    def test_groq_429_quarantines_only_the_exhausted_window(
        self,
        post,
        _now,
    ):
        response = unittest.mock.Mock()
        response.status_code = 429
        response.ok = False
        response.headers = {
            "Retry-After": "45",
            "x-ratelimit-limit-requests": "30",
            "x-ratelimit-remaining-requests": "29",
            "x-ratelimit-reset-requests": "3m",
            "x-ratelimit-limit-tokens": "8000",
            "x-ratelimit-remaining-tokens": "0",
            "x-ratelimit-reset-tokens": "45s",
        }
        response.json.return_value = {
            "error": {"message": "rate limit reached"},
        }
        post.return_value = response
        provider = _OpenAICompatibleProvider(
            "Groq",
            "test-key",
            "model",
            "https://example.test",
        )

        with self.assertRaises(AIProviderQuotaError) as raised:
            provider.generate_json("prompt")

        self.assertEqual(
            [limit["metric"] for limit in raised.exception.limits],
            ["tpm"],
        )
        self.assertEqual(raised.exception.limits[0]["reset_at"], 1045)

    def _service(self):
        service = AIService(
            gemini_callable=lambda _prompt: {"ok": True},
            gemini_configured=lambda: True,
            gemini_quota_error_types=(AIProviderQuotaError,),
            gemini_error_types=(AIProviderError,),
            recovery_interval_seconds=60,
            usage_state_loader=lambda: None,
            usage_state_saver=lambda _state: True,
            provider_enabled_reader=lambda _feature_key: True,
            provider_enabled_writer=lambda _feature_key, enabled: bool(enabled),
        )
        # Unit tests must never use workspace credentials or call live services.
        for provider in service._providers:
            if isinstance(provider, _OpenAICompatibleProvider):
                provider.api_key = ""
        return service

    @patch("core.ai_service.time.time")
    def test_deepseek_observed_daily_usage_resets_at_utc_plus_one_midnight(
        self,
        now,
    ):
        start = datetime(
            2026,
            9,
            29,
            22,
            59,
            tzinfo=timezone.utc,
        ).timestamp()
        reset_at = datetime(
            2026,
            9,
            29,
            23,
            0,
            tzinfo=timezone.utc,
        ).timestamp()
        now.return_value = start

        saved = []
        service = self._service()
        service._usage_state_saver = lambda state: saved.append(state)
        deepseek = _FakeProvider(
            "DeepSeek",
            [
                AIResponse(
                    {"ok": True},
                    usage={
                        "prompt_tokens": 31,
                        "completion_tokens": 12,
                        "total_tokens": 43,
                    },
                ),
            ],
        )
        service._providers = (deepseek,)

        service.generate_json("user request")

        deepseek_status = service.status()["providers"][0]
        self.assertEqual(
            deepseek_status["usage"]["observed_requests_day"],
            1,
        )
        self.assertEqual(
            deepseek_status["usage"]["observed_tokens_day"],
            43,
        )
        self.assertEqual(
            service._usage_windows["DeepSeek"]["observed_reset_at"],
            reset_at,
        )
        self.assertEqual(
            saved[-1]["usage"]["DeepSeek"]["observed_tokens_day"],
            43,
        )

        restored = self._service()
        restored._providers = (_FakeProvider("DeepSeek", []),)
        restored._usage_state_loader = lambda: saved[-1]
        restored_status = restored.status()["providers"][0]
        self.assertEqual(
            restored_status["usage"]["observed_requests_day"],
            1,
        )
        self.assertEqual(
            restored_status["usage"]["observed_tokens_day"],
            43,
        )

        now.return_value = reset_at
        reset_status = service.status()["providers"][0]
        self.assertEqual(
            reset_status["usage"]["observed_requests_day"],
            0,
        )
        self.assertEqual(
            reset_status["usage"]["observed_tokens_day"],
            0,
        )
        self.assertEqual(
            service._usage_windows["DeepSeek"]["observed_reset_at"],
            datetime(
                2026,
                9,
                30,
                23,
                0,
                tzinfo=timezone.utc,
            ).timestamp(),
        )

    def test_restores_and_updates_persisted_usage_state(self):
        saved = []
        persisted = {
            "usage": {
                "Gemini": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                    "neurons": 0,
                },
            },
            "rates": {
                "Gemini": {
                    "tpd": {
                        "metric": "tpd",
                        "limit": 1000,
                        "remaining": 985,
                        "reset_at": 4_000_000_000,
                        "source": "estimated",
                        "exact": False,
                    },
                },
            },
            "usage_windows": {
                "Gemini": {
                    "tokens_reset_at": 4_000_000_000,
                    "neurons_reset_at": None,
                },
            },
        }
        service = AIService(
            gemini_callable=lambda _prompt: AIResponse(
                {"ok": True},
                usage={
                    "promptTokenCount": 4,
                    "candidatesTokenCount": 2,
                },
            ),
            gemini_configured=lambda: True,
            gemini_quota_error_types=(AIProviderQuotaError,),
            gemini_error_types=(AIProviderError,),
            usage_state_loader=lambda: persisted,
            usage_state_saver=lambda state: saved.append(state),
        )
        for provider in service._providers:
            if isinstance(provider, _OpenAICompatibleProvider):
                provider.api_key = ""

        service.generate_json("prompt")

        gemini = next(
            row
            for row in service.status()["providers"]
            if row["name"] == "Gemini"
        )
        usage = gemini["usage"]
        self.assertEqual(usage["prompt_tokens"], 14)
        self.assertEqual(usage["completion_tokens"], 7)
        self.assertEqual(usage["total_tokens"], 21)
        self.assertEqual(saved[-1]["usage"]["Gemini"]["total_tokens"], 21)
        self.assertTrue(gemini["usage_estimated"])

    def test_migrates_stale_gemini_daily_limit_without_resetting_usage(self):
        persisted = {
            "usage": {
                "Gemini": {
                    "requests_day": 7,
                    "total_tokens": 1_200,
                },
            },
            "rates": {
                "Gemini": {
                    "rpd": {
                        "metric": "rpd",
                        "limit": 20,
                        "remaining": 13,
                        "reset_at": 4_000_000_000,
                        "source": "configured",
                        "exact": True,
                    },
                },
            },
        }
        saved = []
        service = AIService(
            gemini_callable=lambda _prompt: {"ok": True},
            gemini_configured=lambda: True,
            gemini_quota_error_types=(AIProviderQuotaError,),
            gemini_error_types=(AIProviderError,),
            usage_state_loader=lambda: persisted,
            usage_state_saver=lambda state: saved.append(state),
        )

        gemini = next(
            row
            for row in service.status()["providers"]
            if row["name"] == "Gemini"
        )
        limits = {row["metric"]: row for row in gemini["limits"]}

        self.assertEqual(gemini["usage"]["requests_day"], 7)
        self.assertEqual(limits["rpd"]["limit"], 150_000)
        self.assertEqual(limits["rpd"]["used"], 7)
        self.assertEqual(limits["rpd"]["remaining"], 149_993)
        self.assertEqual(limits["rpd"]["source"], "configured")
        self.assertTrue(limits["rpd"]["exact"])
        self.assertEqual(
            saved[-1]["rates"]["Gemini"]["rpd"]["limit"],
            150_000,
        )

    def test_migrates_stale_provider_gemini_limit_without_resetting_usage(self):
        persisted = {
            "usage": {
                "Gemini": {
                    "requests_day": 579,
                    "total_tokens": 533_600,
                },
            },
            "rates": {
                "Gemini": {
                    "rpd": {
                        "metric": "rpd",
                        "limit": 20,
                        "remaining": 0,
                        "reset_at": 4_000_000_000,
                        "source": "provider",
                        "exact": True,
                    },
                    "tpd": {
                        "metric": "tpd",
                        "limit": 533_600,
                        "remaining": 0,
                        "reset_at": 4_000_000_000,
                        "source": "estimated",
                        "exact": False,
                    },
                },
            },
        }
        saved = []
        service = AIService(
            gemini_callable=lambda _prompt: {"ok": True},
            gemini_configured=lambda: True,
            gemini_quota_error_types=(AIProviderQuotaError,),
            gemini_error_types=(AIProviderError,),
            usage_state_loader=lambda: persisted,
            usage_state_saver=lambda state: saved.append(state),
        )

        gemini = next(
            row
            for row in service.status()["providers"]
            if row["name"] == "Gemini"
        )
        limits = {row["metric"]: row for row in gemini["limits"]}

        self.assertEqual(gemini["usage"]["requests_day"], 579)
        self.assertEqual(limits["rpd"]["limit"], 150_000)
        self.assertEqual(limits["rpd"]["used"], 579)
        self.assertEqual(limits["rpd"]["remaining"], 149_421)
        self.assertEqual(limits["rpd"]["source"], "configured")
        self.assertTrue(limits["rpd"]["exact"])
        self.assertEqual(limits["tpd"]["limit"], 533_600)
        self.assertEqual(limits["tpd"]["source"], "estimated")
        self.assertEqual(saved[-1]["rates"]["Gemini"]["rpd"]["limit"], 150_000)

    def test_expired_persisted_windows_reset_with_provider_timezone(self):
        expired = 1_700_000_000
        persisted = {
            "usage": {
                "Gemini": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                    "neurons": 0,
                },
                "Cloudflare": {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                    "neurons": 50,
                },
            },
            "rates": {
                "Gemini": {
                    "rpm": {
                        "limit": 10,
                        "remaining": 1,
                        "reset_at": expired,
                        "source": "provider",
                        "exact": True,
                    },
                    "tpd": {
                        "limit": 100,
                        "remaining": 20,
                        "reset_at": expired,
                        "source": "estimated",
                        "exact": False,
                    },
                },
            },
            "usage_windows": {
                "Gemini": {
                    "tokens_reset_at": expired,
                    "neurons_reset_at": None,
                },
                "Cloudflare": {
                    "tokens_reset_at": None,
                    "neurons_reset_at": expired,
                },
            },
        }
        service = AIService(
            gemini_callable=lambda _prompt: {"ok": True},
            gemini_configured=lambda: True,
            gemini_quota_error_types=(AIProviderQuotaError,),
            gemini_error_types=(AIProviderError,),
            usage_state_loader=lambda: persisted,
            usage_state_saver=lambda _state: True,
        )

        with patch("core.ai_service.time.time", return_value=1_800_000_000):
            status = service.status()

        providers = {row["name"]: row for row in status["providers"]}
        self.assertEqual(providers["Gemini"]["usage"]["total_tokens"], 0)
        self.assertEqual(providers["Cloudflare"]["usage"]["neurons"], 0)
        limits = {
            row["metric"]: row for row in providers["Gemini"]["limits"]
        }
        self.assertEqual(limits["rpm"]["remaining"], 4_000)
        self.assertEqual(limits["rpm"]["limit"], 4_000)
        self.assertEqual(limits["rpm"]["source"], "configured")
        self.assertTrue(limits["rpm"]["exact"])
        self.assertEqual(
            service._usage_windows["Gemini"]["tokens_reset_at"],
            _next_daily_reset("Gemini", 1_800_000_000),
        )
        self.assertEqual(
            service._usage_windows["Cloudflare"]["neurons_reset_at"],
            _next_daily_reset("Cloudflare", 1_800_000_000),
        )

    def test_disabled_provider_is_skipped_without_losing_quota_state(self):
        service = self._service()
        providers = (
            _FakeProvider("Gemini", [{"provider": "Gemini"}]),
            _FakeProvider("Groq", [{"provider": "Groq"}]),
            _FakeProvider("OpenRouter", [{"provider": "OpenRouter"}]),
        )
        service._providers = providers
        service.set_provider_enabled("Gemini", False)

        self.assertEqual(
            service.generate_json("prompt"),
            {"provider": "Groq"},
        )
        self.assertEqual([provider.calls for provider in providers], [0, 1, 0])
        self.assertFalse(
            next(
                provider["enabled"]
                for provider in service.status()["providers"]
                if provider["name"] == "Gemini"
            )
        )

    def test_successful_result_carries_the_provider_name(self):
        service = self._service()

        result = service.generate_json("prompt")

        self.assertEqual(result, {"ok": True})
        self.assertEqual(result.provider, "Gemini")

    def test_user_requests_follow_their_independent_priority_order(self):
        service = self._service()
        providers = (
            _FakeProvider("Groq", [AIProviderError("Groq unavailable")]),
            _FakeProvider(
                "OpenRouter",
                [AIProviderError("OpenRouter unavailable")],
            ),
            _FakeProvider("Gemini", [AIProviderError("Gemini unavailable")]),
            _FakeProvider("DeepSeek", [AIProviderError("DeepSeek unavailable")]),
            _FakeProvider("Cloudflare", [{"provider": "Cloudflare"}]),
        )
        service._providers = providers

        result = service.generate_json("prompt", request_kind="user")

        self.assertEqual(result.provider, "Cloudflare")
        self.assertEqual(
            [provider.calls for provider in providers],
            [0, 0, 1, 1, 1],
        )

    def test_provider_enable_state_is_independent_for_each_request_kind(self):
        service = self._service()
        providers = (
            _FakeProvider("Groq", [{"provider": "Groq"}]),
            _FakeProvider("OpenRouter", [{"provider": "OpenRouter"}]),
            _FakeProvider("Gemini", [{"provider": "Gemini"}]),
            _FakeProvider("DeepSeek", [{"provider": "DeepSeek"}]),
            _FakeProvider("Cloudflare", [{"provider": "Cloudflare"}]),
        )
        service._providers = providers
        service.set_provider_enabled("Groq", False, request_kind="channel")

        channel_result = service.generate_json(
            "channel request",
            request_kind="channel",
        )
        user_result = service.generate_json(
            "user request",
            request_kind="user_category",
        )
        channel_status = service.status(request_kind="channel")
        user_status = service.status(request_kind="user")
        channel_groq = next(
            provider
            for provider in channel_status["providers"]
            if provider["name"] == "Groq"
        )
        user_groq = next(
            provider
            for provider in user_status["providers"]
            if provider["name"] == "Groq"
        )

        self.assertEqual(channel_result.provider, "OpenRouter")
        self.assertEqual(user_result.provider, "DeepSeek")
        self.assertEqual(channel_status["active_provider"], "OpenRouter")
        self.assertEqual(user_status["active_provider"], "DeepSeek")
        self.assertFalse(channel_groq["enabled"])
        self.assertTrue(user_groq["enabled"])
        self.assertEqual(
            [provider.calls for provider in providers],
            [0, 1, 0, 1, 0],
        )
        channel_usage = {
            provider["name"]: provider["usage"]
            for provider in channel_status["providers"]
        }
        user_usage = {
            provider["name"]: provider["usage"]
            for provider in user_status["providers"]
        }
        self.assertEqual(channel_usage, user_usage)

    def test_cloudflare_cannot_be_enabled_for_channel_but_remains_a_user_provider(
        self,
    ):
        service = self._service()
        cloudflare = _FakeProvider(
            "Cloudflare",
            [{"provider": "Cloudflare"}],
        )
        service._providers = (cloudflare,)
        writer_calls = []
        service._provider_enabled_writer = (
            lambda key, enabled: writer_calls.append((key, enabled))
            or bool(enabled)
        )

        self.assertFalse(
            service.set_provider_enabled(
                "Cloudflare",
                True,
                request_kind="channel",
            )
        )
        with self.assertRaises(AIServiceUnavailableError):
            service.generate_json("channel request", request_kind="channel")
        user_result = service.generate_json(
            "user request",
            request_kind="user",
        )

        channel_status = service.status(request_kind="channel")
        user_status = service.status(request_kind="user")
        channel_cloudflare = channel_status["providers"][0]
        user_cloudflare = user_status["providers"][0]

        self.assertEqual(user_result.provider, "Cloudflare")
        self.assertEqual(cloudflare.calls, 1)
        self.assertFalse(channel_cloudflare["enabled"])
        self.assertFalse(
            channel_cloudflare["enabled_by_request_kind"]["channel"]
        )
        self.assertTrue(user_cloudflare["enabled"])
        self.assertTrue(
            user_cloudflare["enabled_by_request_kind"]["user"]
        )
        self.assertEqual(writer_calls, [])

    def test_provider_usage_is_aggregated_across_channel_and_user_requests(self):
        service = self._service()
        groq = _FakeProvider(
            "Groq",
            [
                AIResponse({"ok": True}, usage={"total_tokens": 7}),
                AIResponse({"ok": True}, usage={"total_tokens": 5}),
            ],
        )
        service._providers = (groq,)

        service.generate_json("channel request", request_kind="channel")
        service.generate_json("user request", request_kind="user")

        channel_groq = service.status(request_kind="channel")["providers"][0]
        user_groq = service.status(request_kind="user")["providers"][0]

        self.assertEqual(channel_groq["usage"], user_groq["usage"])
        self.assertEqual(channel_groq["usage"]["requests_day"], 2)
        self.assertEqual(channel_groq["usage"]["tokens_day"], 12)

    def test_explicit_provider_request_does_not_fall_back_to_another_provider(self):
        service = self._service()
        providers = (
            _FakeProvider("Groq", [{"provider": "Groq"}]),
            _FakeProvider("Gemini", [{"provider": "Gemini"}]),
            _FakeProvider("OpenRouter", [{"provider": "OpenRouter"}]),
            _FakeProvider("Cloudflare", [{"provider": "Cloudflare"}]),
        )
        service._providers = providers

        result = service.generate_json(
            "phrase-match prompt",
            request_kind="user",
            provider_name="Gemini",
        )

        self.assertEqual(result.provider, "Gemini")
        self.assertEqual([provider.calls for provider in providers], [0, 1, 0, 0])

    def test_available_cloudflare_restores_user_mode_from_stale_local_state(self):
        service = self._service()
        providers = (
            _FakeProvider("Gemini", [], configured=False),
            _FakeProvider("Groq", [], configured=False),
            _FakeProvider("OpenRouter", [], configured=False),
            _FakeProvider("Cloudflare", [{"provider": "Cloudflare"}]),
        )
        service._providers = providers
        service._active_index = len(providers)
        service._local_mode = True

        status = service.status()

        self.assertEqual(status["mode"], "external")
        self.assertEqual(status["active_provider"], "Cloudflare")
        self.assertEqual(
            service.generate_json("prompt", request_kind="user").provider,
            "Cloudflare",
        )
        self.assertEqual(providers[3].calls, 1)

    def test_channel_requests_never_fall_back_to_cloudflare(self):
        service = self._service()
        providers = (
            _FakeProvider("Gemini", [AIProviderError("Gemini unavailable")]),
            _FakeProvider("Groq", [AIProviderError("Groq unavailable")]),
            _FakeProvider(
                "OpenRouter",
                [AIProviderError("OpenRouter unavailable")],
            ),
            _FakeProvider("Cloudflare", [{"provider": "Cloudflare"}]),
        )
        service._providers = providers

        with self.assertRaises(AIServiceUnavailableError):
            service.generate_json("prompt", request_kind="channel")

        self.assertEqual(providers[3].calls, 0)

    def test_channel_failure_does_not_enter_user_local_mode(self):
        service = self._service()
        providers = (
            _FakeProvider("Gemini", [], configured=False),
            _FakeProvider("Groq", [], configured=False),
            _FakeProvider("OpenRouter", [], configured=False),
            _FakeProvider("Cloudflare", [{"provider": "Cloudflare"}]),
        )
        service._providers = providers

        with self.assertRaises(AIServiceUnavailableError):
            service.generate_json("prompt", request_kind="channel")

        self.assertFalse(service._local_mode)
        self.assertEqual(service.status()["active_provider"], "Cloudflare")

    def test_user_subtypes_keep_the_local_fallback(self):
        for request_kind in ("user_category", "admin"):
            with self.subTest(request_kind=request_kind):
                service = self._service()
                service._providers = tuple(
                    _FakeProvider(
                        name,
                        [AIProviderQuotaError("quota exhausted")],
                    )
                    for name in (
                        "Groq",
                        "OpenRouter",
                        "Gemini",
                        "DeepSeek",
                        "Cloudflare",
                    )
                )

                with self.assertRaises(AIServiceUnavailableError):
                    service.generate_json(
                        "prompt",
                        request_kind=request_kind,
                    )

                self.assertTrue(service._local_mode)
                self.assertEqual(
                    service.status(request_kind=request_kind)["mode"],
                    "local",
                )

    def test_channel_status_ignores_cloudflare_availability(self):
        service = self._service()
        providers = (
            _FakeProvider("Gemini", [], configured=False),
            _FakeProvider("Groq", [], configured=False),
            _FakeProvider("OpenRouter", [], configured=False),
            _FakeProvider("Cloudflare", [], configured=True),
        )
        service._providers = providers

        self.assertEqual(service.status()["mode"], "external")
        self.assertEqual(
            service.status(request_kind="channel")["mode"],
            "local",
        )
        self.assertIsNone(
            service.status(request_kind="channel")["next_recovery_at"]
        )

    def test_channel_requests_use_openrouter_before_cloudflare(self):
        service = self._service()
        providers = (
            _FakeProvider("Groq", [AIProviderError("Groq unavailable")]),
            _FakeProvider("OpenRouter", [{"provider": "OpenRouter"}]),
            _FakeProvider("Gemini", [AIProviderError("Gemini unavailable")]),
            _FakeProvider("DeepSeek", [{"provider": "DeepSeek"}]),
            _FakeProvider("Cloudflare", []),
        )
        service._providers = providers

        result = service.generate_json("prompt", request_kind="channel")

        self.assertEqual(result.provider, "OpenRouter")
        self.assertEqual(providers[4].calls, 0)

    def test_all_disabled_providers_enter_local_mode(self):
        service = self._service()
        for name in ("Groq", "OpenRouter", "Gemini", "DeepSeek", "Cloudflare"):
            service.set_provider_enabled(name, False)

        with self.assertRaises(AIServiceUnavailableError):
            service.generate_json("prompt")

        self.assertEqual(service.status()["mode"], "local")

    def test_reenable_does_not_clear_existing_quota_quarantine(self):
        service = self._service()
        service._providers = (
            _FakeProvider(
                "Gemini",
                [AIProviderQuotaError("quota exhausted")],
            ),
            _FakeProvider("Groq", [{"provider": "Groq"}]),
            _FakeProvider("OpenRouter", []),
        )
        self.assertEqual(
            service.generate_json("first"),
            {"provider": "Groq"},
        )
        service.set_provider_enabled("Gemini", False)
        service.set_provider_enabled("Gemini", True)

        self.assertEqual(service.status()["active_provider"], "Groq")
        self.assertEqual(service._providers[0].calls, 1)

    def test_concurrent_toggles_keep_persisted_and_runtime_state_aligned(self):
        saved_values = []
        first_writer_started = threading.Event()
        release_first_writer = threading.Event()
        second_toggle_done = threading.Event()

        def save_enabled(_key, enabled):
            if not saved_values:
                first_writer_started.set()
                release_first_writer.wait(timeout=2)
            saved_values.append(bool(enabled))
            return bool(enabled)

        service = AIService(
            gemini_callable=lambda _prompt: {"ok": True},
            gemini_configured=lambda: True,
            gemini_quota_error_types=(AIProviderQuotaError,),
            gemini_error_types=(AIProviderError,),
            provider_enabled_writer=save_enabled,
        )
        first = threading.Thread(
            target=service.set_provider_enabled,
            args=("Gemini", False),
        )
        second = threading.Thread(
            target=lambda: (
                service.set_provider_enabled("Gemini", True),
                second_toggle_done.set(),
            ),
        )

        first.start()
        self.assertTrue(first_writer_started.wait(timeout=1))
        second.start()
        self.assertFalse(second_toggle_done.wait(timeout=0.1))
        release_first_writer.set()
        first.join()
        second.join()

        gemini = next(
            row
            for row in service.status()["providers"]
            if row["name"] == "Gemini"
        )
        self.assertEqual(saved_values, [False, True])
        self.assertEqual(gemini["enabled"], saved_values[-1])

    def test_toggle_does_not_wait_for_an_in_flight_provider_request(self):
        started = threading.Event()
        release = threading.Event()
        toggle_done = threading.Event()

        class _BlockingProvider(_FakeProvider):
            def generate_json(self, _prompt):
                self.calls += 1
                started.set()
                release.wait(timeout=2)
                return {"provider": self.name}

        service = self._service()
        providers = (
            _BlockingProvider("Gemini", []),
            _FakeProvider("Groq", [{"provider": "Groq"}]),
            _FakeProvider("OpenRouter", []),
        )
        service._providers = providers
        first_result = []
        request = threading.Thread(
            target=lambda: first_result.append(service.generate_json("first"))
        )
        request.start()
        self.assertTrue(started.wait(timeout=1))

        toggle = threading.Thread(
            target=lambda: (
                service.set_provider_enabled("Gemini", False),
                toggle_done.set(),
            )
        )
        toggle.start()
        try:
            self.assertTrue(toggle_done.wait(timeout=0.2))
            self.assertFalse(service.status()["providers"][0]["enabled"])
        finally:
            release.set()
            toggle.join(timeout=1)
            request.join(timeout=1)

        self.assertEqual(first_result, [{"provider": "Gemini"}])
        self.assertEqual(
            service.generate_json("second"),
            {"provider": "Groq"},
        )
        self.assertEqual([provider.calls for provider in providers], [1, 1, 0])

    def test_external_requests_are_not_serialized_by_one_global_lock(self):
        started_count = 0
        started_lock = threading.Lock()
        both_started = threading.Event()
        release = threading.Event()

        class _ConcurrentProvider(_FakeProvider):
            def generate_json(self, _prompt):
                nonlocal started_count
                self.calls += 1
                with started_lock:
                    started_count += 1
                    if started_count == 2:
                        both_started.set()
                release.wait(timeout=2)
                return {"provider": self.name}

        service = self._service()
        providers = (
            _ConcurrentProvider("Gemini", []),
            _FakeProvider("Groq", []),
            _FakeProvider("Cloudflare", []),
            _FakeProvider("OpenRouter", []),
        )
        service._providers = providers
        results = []
        threads = [
            threading.Thread(
                target=lambda: results.append(service.generate_json("prompt"))
            )
            for _ in range(2)
        ]
        for thread in threads:
            thread.start()
        try:
            self.assertTrue(both_started.wait(timeout=0.5))
        finally:
            release.set()
            for thread in threads:
                thread.join(timeout=1)

        self.assertEqual(len(results), 2)

    def test_in_flight_success_does_not_leave_all_disabled_service_external(self):
        started = threading.Event()
        release = threading.Event()

        class _BlockingProvider(_FakeProvider):
            def generate_json(self, _prompt):
                self.calls += 1
                started.set()
                release.wait(timeout=2)
                return {"provider": self.name}

        service = self._service()
        service._providers = (
            _BlockingProvider("Gemini", []),
            _FakeProvider("Groq", []),
            _FakeProvider("Cloudflare", []),
            _FakeProvider("OpenRouter", []),
        )
        request = threading.Thread(
            target=lambda: service.generate_json("in flight")
        )
        request.start()
        self.assertTrue(started.wait(timeout=1))

        try:
            for provider_name in (
                "Gemini",
                "Groq",
                "DeepSeek",
                "Cloudflare",
                "OpenRouter",
            ):
                service.set_provider_enabled(provider_name, False)
        finally:
            release.set()
            request.join(timeout=1)

        status = service.status()
        self.assertEqual(status["mode"], "local")
        self.assertEqual(status["active_provider"], "Local service")

    def test_provider_test_works_while_disabled_without_changing_state(self):
        saved = []
        service = AIService(
            gemini_callable=lambda _prompt: AIResponse(
                {"ok": True},
                usage={
                    "promptTokenCount": 2,
                    "candidatesTokenCount": 1,
                },
                model="gemini-test",
            ),
            gemini_configured=lambda: True,
            gemini_model=lambda: "gemini-test",
            gemini_quota_error_types=(AIProviderQuotaError,),
            gemini_error_types=(AIProviderError,),
            usage_state_loader=lambda: {},
            usage_state_saver=lambda state: saved.append(state),
        )
        service.set_provider_enabled("Gemini", False)

        result = service.test_provider("Gemini")

        self.assertTrue(result["ok"])
        self.assertEqual(result["model"], "gemini-test")
        gemini = next(
            row
            for row in service.status()["providers"]
            if row["name"] == "Gemini"
        )
        self.assertFalse(gemini["enabled"])
        self.assertEqual(gemini["usage"]["total_tokens"], 3)
        self.assertEqual(saved[-1]["usage"]["Gemini"]["total_tokens"], 3)

    def test_provider_test_classifies_failure_without_changing_quarantine(self):
        service = self._service()
        service._providers = (
            _FakeProvider("Gemini", [AIProviderQuotaError("secret detail")]),
            _FakeProvider("Groq", [], configured=False),
            _FakeProvider("Cloudflare", [], configured=False),
            _FakeProvider("OpenRouter", [], configured=False),
        )

        before = dict(service._unavailable)
        result = service.test_provider("Gemini")

        self.assertEqual(result, {
            "ok": False,
            "kind": "quota",
            "provider": "Gemini",
        })
        self.assertEqual(service._unavailable, before)

    def test_duplicate_provider_tests_are_rejected_while_one_is_running(self):
        started = threading.Event()
        release = threading.Event()

        def slow_test(_prompt):
            started.set()
            release.wait(timeout=1)
            return {"ok": True}

        service = AIService(
            gemini_callable=slow_test,
            gemini_configured=lambda: True,
            gemini_quota_error_types=(AIProviderQuotaError,),
            gemini_error_types=(AIProviderError,),
            usage_state_loader=lambda: {},
            usage_state_saver=lambda _state: True,
        )
        first_result = []
        first = threading.Thread(
            target=lambda: first_result.append(service.test_provider("Gemini"))
        )
        first.start()
        self.assertTrue(started.wait(timeout=1))

        duplicate = service.test_provider("Gemini")
        release.set()
        first.join(timeout=1)

        self.assertEqual(duplicate["kind"], "busy")
        self.assertTrue(first_result[0]["ok"])

    def test_uses_providers_in_required_order(self):
        service = self._service()
        providers = (
            _FakeProvider(
                "Groq",
                [AIProviderQuotaError("quota exhausted")],
            ),
            _FakeProvider(
                "OpenRouter",
                [AIProviderQuotaError("quota exhausted")],
            ),
            _FakeProvider("Gemini", [AIProviderQuotaError("quota exhausted")]),
            _FakeProvider("DeepSeek", [{"provider": "DeepSeek"}]),
            _FakeProvider("Cloudflare", [{"provider": "Cloudflare"}]),
        )
        service._providers = providers

        self.assertEqual(
            service.generate_json("prompt"),
            {"provider": "DeepSeek"},
        )
        self.assertEqual(
            [provider.calls for provider in providers],
            [0, 0, 0, 1, 0],
        )
        self.assertEqual(service.status()["active_provider"], "DeepSeek")

    def test_enters_local_mode_after_all_external_providers_fail(self):
        service = self._service()
        service._providers = tuple(
            _FakeProvider(
                name,
                [AIProviderQuotaError("quota exhausted")],
            )
            for name in (
                "Groq",
                "OpenRouter",
                "Gemini",
                "DeepSeek",
                "Cloudflare",
            )
        )

        with self.assertRaises(AIServiceUnavailableError):
            service.generate_json("prompt")

        status = service.status()
        self.assertEqual(status["mode"], "local")
        self.assertEqual(status["active_provider"], "Local service")
        self.assertIsNotNone(status["next_gemini_probe_at"])

        with self.assertRaises(AIServiceUnavailableError) as repeated:
            service.generate_json("another prompt")
        self.assertTrue(repeated.exception.quota_exhausted)

    def test_successful_gemini_probe_restores_primary_provider(self):
        service = self._service()
        providers = (
            _FakeProvider(
                "Gemini",
                [
                    AIProviderQuotaError("quota exhausted"),
                    {"ok": True},
                ],
            ),
            _FakeProvider("Groq", [AIProviderError("unavailable")]),
            _FakeProvider("Cloudflare", [AIProviderError("unavailable")]),
            _FakeProvider("OpenRouter", [AIProviderError("unavailable")]),
        )
        service._providers = providers

        with self.assertRaises(AIServiceUnavailableError):
            service.generate_json("prompt")
        self.assertTrue(service.probe_primary_now())

        status = service.status()
        self.assertEqual(status["mode"], "external")
        self.assertEqual(status["active_provider"], "Gemini")
        self.assertIsNone(status["next_gemini_probe_at"])

    def test_in_flight_probe_does_not_restore_an_admin_disabled_chain(self):
        started = threading.Event()
        release = threading.Event()

        def blocking_probe(_prompt):
            started.set()
            release.wait(timeout=2)
            return {"ok": True}

        service = AIService(
            gemini_callable=blocking_probe,
            gemini_configured=lambda: True,
            gemini_quota_error_types=(AIProviderQuotaError,),
            gemini_error_types=(AIProviderError,),
            usage_state_loader=lambda: {},
            usage_state_saver=lambda _state: True,
        )
        result = []
        probe = threading.Thread(
            target=lambda: result.append(service.probe_primary_now())
        )
        probe.start()
        self.assertTrue(started.wait(timeout=1))

        try:
            for provider_name in (
                "Gemini",
                "Groq",
                "DeepSeek",
                "Cloudflare",
                "OpenRouter",
            ):
                service.set_provider_enabled(provider_name, False)
        finally:
            release.set()
            probe.join(timeout=1)

        self.assertFalse(probe.is_alive())
        self.assertEqual(result, [True])
        self.assertTrue(service._local_mode)
        self.assertIsNone(service._next_probe_at)
        self.assertEqual(service.status()["mode"], "local")

    def test_request_scoped_failure_does_not_quarantine_provider(self):
        service = self._service()
        providers = (
            _FakeProvider(
                "Gemini",
                [AIProviderError("invalid response"), {"provider": "Gemini"}],
            ),
            _FakeProvider("Groq", [{"provider": "Groq"}]),
            _FakeProvider("OpenRouter", [{"provider": "OpenRouter"}]),
        )
        service._providers = providers

        self.assertEqual(
            service.generate_json("first"),
            {"provider": "Groq"},
        )
        self.assertEqual(
            service.generate_json("second"),
            {"provider": "Gemini"},
        )
        self.assertEqual(service.status()["active_provider"], "Gemini")

    def test_transient_failure_falls_through_without_quarantining_provider(self):
        service = self._service()
        providers = (
            _FakeProvider(
                "Gemini",
                [
                    AIProviderTransientError("temporary provider outage"),
                    {"provider": "Gemini"},
                ],
            ),
            _FakeProvider("Groq", [{"provider": "Groq"}]),
            _FakeProvider("OpenRouter", []),
        )
        service._providers = providers

        self.assertEqual(
            service.generate_json("first"),
            {"provider": "Groq"},
        )
        self.assertNotIn("Gemini", service._unavailable)
        self.assertEqual(
            service.generate_json("second"),
            {"provider": "Gemini"},
        )
        self.assertEqual(providers[0].calls, 2)

    def test_non_quota_exhaustion_keeps_non_quota_classification(self):
        service = self._service()
        service._providers = (
            _FakeProvider(
                "Gemini",
                [AIProviderQuotaError("quota exhausted")],
            ),
            _FakeProvider(
                "Groq",
                [AIProviderConfigurationError("invalid key")],
            ),
            _FakeProvider(
                "Cloudflare",
                [AIProviderQuotaError("quota exhausted")],
            ),
            _FakeProvider(
                "OpenRouter",
                [AIProviderQuotaError("quota exhausted")],
            ),
        )

        with self.assertRaises(AIServiceUnavailableError) as raised:
            service.generate_json("prompt")

        self.assertFalse(raised.exception.quota_exhausted)

    def test_transient_primary_failure_is_retried_on_next_request(self):
        service = self._service()
        providers = (
            _FakeProvider(
                "Gemini",
                [AIProviderError("temporary malformed response"), {"ok": True}],
            ),
            _FakeProvider("Groq", [], configured=False),
            _FakeProvider("Cloudflare", [], configured=False),
            _FakeProvider("OpenRouter", [], configured=False),
        )
        service._providers = providers

        with self.assertRaises(AIServiceUnavailableError):
            service.generate_json("first")
        self.assertEqual(service.status()["active_provider"], "Gemini")
        self.assertEqual(service.status()["mode"], "external")

        self.assertEqual(service.generate_json("second"), {"ok": True})
        self.assertEqual(providers[0].calls, 2)

    def test_quarantined_provider_is_not_called_again(self):
        service = self._service()
        providers = (
            _FakeProvider(
                "Gemini",
                [
                    AIProviderError("temporary failure"),
                    AIProviderError("temporary failure"),
                ],
            ),
            _FakeProvider(
                "Groq",
                [AIProviderQuotaError("quota exhausted")],
            ),
            _FakeProvider(
                "Cloudflare",
                [],
                configured=False,
            ),
            _FakeProvider(
                "OpenRouter",
                [{"provider": "OpenRouter"}, {"provider": "OpenRouter"}],
            ),
        )
        service._providers = providers

        self.assertEqual(
            service.generate_json("first"),
            {"provider": "OpenRouter"},
        )
        self.assertEqual(
            service.generate_json("second"),
            {"provider": "OpenRouter"},
        )
        self.assertEqual(providers[1].calls, 1)

    def test_request_scoped_failure_prevents_false_quota_classification(self):
        service = self._service()
        service._providers = (
            _FakeProvider("Gemini", [AIProviderError("invalid response")]),
            _FakeProvider(
                "Groq",
                [AIProviderQuotaError("quota exhausted")],
            ),
            _FakeProvider("Cloudflare", [], configured=False),
            _FakeProvider("OpenRouter", [], configured=False),
        )

        with self.assertRaises(AIServiceUnavailableError) as raised:
            service.generate_json("prompt")

        self.assertFalse(raised.exception.quota_exhausted)
        self.assertTrue(raised.exception.retryable_recovery)
        self.assertEqual(service.status()["mode"], "external")

    def test_mixed_quota_and_configuration_failure_remains_retryable(self):
        service = self._service()
        service._providers = (
            _FakeProvider(
                "Gemini",
                [AIProviderQuotaError("RPM exhausted")],
            ),
            _FakeProvider(
                "Groq",
                [AIProviderConfigurationError("invalid key")],
            ),
            _FakeProvider("Cloudflare", [], configured=False),
            _FakeProvider("OpenRouter", [], configured=False),
        )

        with self.assertRaises(AIServiceUnavailableError) as raised:
            service.generate_json("prompt")

        self.assertFalse(raised.exception.quota_exhausted)
        self.assertTrue(raised.exception.retryable_recovery)

    @patch("core.ai_service.time.time")
    def test_returns_to_higher_priority_provider_after_minute_reset(self, now):
        now.return_value = 1_000
        service = self._service()
        providers = (
            _FakeProvider(
                "Gemini",
                [
                    AIProviderQuotaError(
                        "RPM exhausted",
                        limits=[{
                            "metric": "rpm",
                            "limit": 10,
                            "remaining": 0,
                            "reset_at": 1_060,
                            "source": "provider",
                            "exact": True,
                        }],
                    ),
                    {"provider": "Gemini"},
                ],
            ),
            _FakeProvider(
                "Groq",
                [{"provider": "Groq"}, {"provider": "Groq"}],
            ),
            _FakeProvider("OpenRouter", []),
        )
        service._providers = providers

        self.assertEqual(
            service.generate_json("first"),
            {"provider": "Groq"},
        )
        self.assertEqual(service.status()["active_provider"], "Groq")

        now.return_value = 1_061
        self.assertEqual(
            service.generate_json("second"),
            {"provider": "Gemini"},
        )
        self.assertEqual([provider.calls for provider in providers], [2, 1, 0])

    @patch("core.ai_service.requests.post")
    def test_groq_uses_authoritative_rate_limit_headers(self, post):
        response = unittest.mock.Mock()
        response.status_code = 200
        response.ok = True
        response.headers = {
            "x-ratelimit-limit-requests": "14400",
            "x-ratelimit-remaining-requests": "14370",
            "x-ratelimit-reset-requests": "2m59.56s",
            "x-ratelimit-limit-tokens": "18000",
            "x-ratelimit-remaining-tokens": "17997",
            "x-ratelimit-reset-tokens": "7.66s",
        }
        response.json.return_value = {
            "choices": [{"message": {"content": '{"ok": true}'}}],
            "usage": {"total_tokens": 3},
        }
        post.return_value = response
        provider = _OpenAICompatibleProvider(
            "Groq",
            "key",
            "model",
            "https://example.test",
        )

        result = provider.generate_json("prompt")

        snapshots = {item["metric"]: item for item in result.limits}
        self.assertEqual(snapshots["rpm"]["limit"], 14400)
        self.assertEqual(snapshots["rpm"]["remaining"], 14370)
        self.assertEqual(snapshots["tpm"]["remaining"], 17997)
        self.assertTrue(snapshots["rpm"]["exact"])

    @patch("core.ai_service.time.time", return_value=1_000)
    def test_groq_headers_sync_configured_windows_without_replacing_limits(
        self,
        _now,
    ):
        service = self._service()
        service._providers = (
            _FakeProvider("Gemini", [], configured=False),
            _FakeProvider(
                "Groq",
                [AIResponse(
                    {"provider": "Groq"},
                    usage={"total_tokens": 10},
                    limits=[
                        {
                            "metric": "rpm",
                            "limit": 14_400,
                            "remaining": 14_399,
                            "reset_at": 1_300,
                            "source": "provider",
                            "exact": True,
                        },
                        {
                            "metric": "tpm",
                            "limit": 18_000,
                            "remaining": 17_990,
                            "reset_at": 1_050,
                            "source": "provider",
                            "exact": True,
                        },
                    ],
                )],
            ),
            _FakeProvider("Cloudflare", [], configured=False),
            _FakeProvider("OpenRouter", [], configured=False),
        )

        service.generate_json("prompt")

        groq = next(
            row for row in service.status()["providers"]
            if row["name"] == "Groq"
        )
        limits = {row["metric"]: row for row in groq["limits"]}
        self.assertEqual(limits["rpd"]["limit"], 1_000)
        self.assertEqual(
            limits["rpd"]["reset_at"],
            "1970-01-02T00:00:00+00:00",
        )
        self.assertEqual(
            limits["rpm"]["reset_at"],
            "1970-01-01T00:21:40+00:00",
        )
        self.assertEqual(limits["tpm"]["limit"], 8_000)
        self.assertEqual(limits["tpm"]["reset_at"], "1970-01-01T00:17:30+00:00")
        self.assertEqual(groq["usage"]["requests_day"], 1)
        self.assertEqual(groq["usage"]["tokens_minute"], 10)
        self.assertEqual(
            groq["usage_window"]["daily_reset_at"],
            _next_daily_reset("Groq", 1_000),
        )
        self.assertEqual(groq["usage_window"]["minute_reset_at"], 1_300)
        self.assertEqual(groq["usage_window"]["tokens_minute_reset_at"], 1_050)

    @patch("core.ai_service.time.time", return_value=1_000)
    def test_groq_quota_headers_schedule_provider_recovery(self, _now):
        service = self._service()
        service._providers = (
            _FakeProvider("Gemini", [], configured=False),
            _FakeProvider("Groq", []),
            _FakeProvider("Cloudflare", [], configured=False),
            _FakeProvider("OpenRouter", [], configured=False),
        )

        service._quarantine_provider(
            1,
            AIProviderQuotaError(
                "Groq quota exhausted",
                limits=[
                    {
                        "metric": "rpd",
                        "limit": 14_400,
                        "remaining": 0,
                        "reset_at": 1_300,
                        "source": "provider",
                        "exact": True,
                    },
                    {
                        "metric": "tpm",
                        "limit": 18_000,
                        "remaining": 0,
                        "reset_at": 1_050,
                        "source": "provider",
                        "exact": True,
                    },
                ],
            ),
        )

        groq = next(
            row for row in service.status()["providers"]
            if row["name"] == "Groq"
        )
        self.assertFalse(groq["available"])
        self.assertTrue(groq["quota_exhausted"])
        self.assertEqual(groq["next_reset_seconds"], 300)
        self.assertEqual(groq["usage_window"]["daily_reset_at"], 1_300)
        self.assertEqual(groq["usage_window"]["tokens_minute_reset_at"], 1_050)

    @patch("core.ai_service.time.time", return_value=1_000)
    def test_groq_provider_reset_headers_do_not_show_countdown_while_available(
        self,
        _now,
    ):
        service = self._service()
        service._providers = (
            _FakeProvider("Gemini", [], configured=False),
            _FakeProvider("Groq", []),
            _FakeProvider("Cloudflare", [], configured=False),
            _FakeProvider("OpenRouter", [], configured=False),
        )
        service._usage_windows["Groq"]["provider_requests_reset_at"] = 1_300
        service._usage_windows["Groq"]["provider_tokens_reset_at"] = 1_050

        groq = next(
            row for row in service.status()["providers"]
            if row["name"] == "Groq"
        )

        self.assertTrue(groq["available"])
        self.assertFalse(groq["quota_exhausted"])
        self.assertIsNone(groq["next_reset_seconds"])

    @patch("core.ai_service.time.time")
    def test_groq_tpm_reset_preserves_daily_request_usage(self, now):
        now.return_value = 1_000
        service = self._service()
        providers = (
            _FakeProvider("Gemini", [], configured=False),
            _FakeProvider(
                "Groq",
                [AIResponse(
                    {"provider": "Groq"},
                    usage={"total_tokens": 1},
                )],
            ),
            _FakeProvider("Cloudflare", [], configured=False),
            _FakeProvider("OpenRouter", [], configured=False),
        )
        service._providers = providers
        groq = providers[1]

        with service._lock:
            service._ensure_default_limits_locked(groq)
            service._usage_state["Groq"]["requests_day"] = 7
            service._usage_state["Groq"]["tokens_minute"] = 8_000
            service._usage_windows["Groq"]["daily_reset_at"] = (
                _next_daily_reset("Groq", 1_000)
            )
            service._usage_windows["Groq"]["tokens_minute_reset_at"] = 1_060
            service._rate_state["Groq"]["rpd"]["remaining"] = 993
            service._rate_state["Groq"]["tpm"]["remaining"] = 0
            service._rate_state["Groq"]["tpm"]["reset_at"] = 1_060

        with self.assertRaises(AIServiceUnavailableError):
            service.generate_json("prompt")
        self.assertEqual(groq.calls, 0)

        now.return_value = 1_061
        result = service.generate_json("prompt")

        self.assertEqual(result.provider, "Groq")
        self.assertEqual(groq.calls, 1)
        self.assertEqual(service._usage_state["Groq"]["requests_day"], 8)
        self.assertEqual(
            service._usage_windows["Groq"]["daily_reset_at"],
            _next_daily_reset("Groq", 1_061),
        )
        self.assertIsNone(
            service._usage_windows["Groq"]["tokens_minute_reset_at"],
        )

    @patch("core.ai_service.time.time")
    def test_groq_rpd_remains_exhausted_until_daily_reset(self, now):
        now.return_value = 1_000
        service = self._service()
        providers = (
            _FakeProvider("Gemini", [], configured=False),
            _FakeProvider(
                "Groq",
                [AIResponse({"provider": "Groq"})],
            ),
            _FakeProvider("Cloudflare", [], configured=False),
            _FakeProvider("OpenRouter", [], configured=False),
        )
        service._providers = providers
        groq = providers[1]
        daily_reset = _next_daily_reset("Groq", 1_000)

        with service._lock:
            service._ensure_default_limits_locked(groq)
            service._usage_state["Groq"]["requests_day"] = 1_000
            service._usage_windows["Groq"]["daily_reset_at"] = daily_reset
            service._rate_state["Groq"]["rpd"]["remaining"] = 0
            service._rate_state["Groq"]["rpd"]["reset_at"] = daily_reset

        with self.assertRaises(AIServiceUnavailableError):
            service.generate_json("prompt")
        self.assertEqual(groq.calls, 0)

        now.return_value = 1_061
        with self.assertRaises(AIServiceUnavailableError):
            service.generate_json("prompt")
        self.assertEqual(groq.calls, 0)

        now.return_value = daily_reset + 1
        result = service.generate_json("prompt")

        self.assertEqual(result.provider, "Groq")
        self.assertEqual(groq.calls, 1)
        self.assertEqual(service._usage_state["Groq"]["requests_day"], 1)
        self.assertEqual(
            service._usage_windows["Groq"]["daily_reset_at"],
            _next_daily_reset("Groq", daily_reset + 1),
        )

    @patch("core.ai_service.time.time", return_value=1_000)
    def test_groq_persisted_windows_survive_restart_then_expire(
        self,
        now,
    ):
        persisted = {
            "usage": {
                "Groq": {
                    "requests_day": 7,
                    "tokens_minute": 10,
                    "tokens_day": 10,
                },
            },
            "rates": {
                "Groq": {
                    "rpd": {
                        "metric": "rpd",
                        "limit": 1_000,
                        "remaining": 993,
                        "reset_at": 1_300,
                        "source": "provider",
                        "exact": True,
                    },
                    "tpm": {
                        "metric": "tpm",
                        "limit": 8_000,
                        "remaining": 7_990,
                        "reset_at": 1_060,
                        "source": "provider",
                        "exact": True,
                    },
                },
            },
            "usage_windows": {
                "Groq": {
                    "daily_reset_at": 1_300,
                    "tokens_minute_reset_at": 1_060,
                    "provider_requests_reset_at": 1_300,
                    "provider_tokens_reset_at": 1_060,
                },
            },
        }
        saved = []
        service = AIService(
            gemini_callable=lambda _prompt: {"ok": True},
            gemini_configured=lambda: True,
            gemini_quota_error_types=(AIProviderQuotaError,),
            gemini_error_types=(AIProviderError,),
            usage_state_loader=lambda: persisted,
            usage_state_saver=lambda state: saved.append(state),
        )
        service._providers = (
            _FakeProvider("Gemini", [], configured=False),
            _FakeProvider("Groq", []),
            _FakeProvider("Cloudflare", [], configured=False),
            _FakeProvider("OpenRouter", [], configured=False),
        )

        status = service.status()
        groq = next(
            row for row in status["providers"]
            if row["name"] == "Groq"
        )
        limits = {
            row["metric"]: row
            for row in groq["limits"]
        }
        self.assertEqual(
            groq["usage_window"]["daily_reset_at"],
            _next_daily_reset("Groq", 1_000),
        )
        self.assertEqual(
            groq["usage_window"]["tokens_minute_reset_at"],
            1_060,
        )
        self.assertEqual(
            groq["usage_window"]["provider_requests_reset_at"],
            None,
        )
        self.assertEqual(
            groq["usage_window"]["provider_tokens_reset_at"],
            1_060,
        )
        self.assertEqual(limits["rpd"]["limit"], 1_000)
        self.assertEqual(limits["tpm"]["limit"], 8_000)
        self.assertTrue(limits["rpd"]["exact"])
        self.assertTrue(limits["tpm"]["exact"])

        now.return_value = 1_301
        status = service.status()
        groq = next(
            row for row in status["providers"]
            if row["name"] == "Groq"
        )
        limits = {
            row["metric"]: row
            for row in groq["limits"]
        }
        self.assertEqual(
            groq["usage_window"]["daily_reset_at"],
            _next_daily_reset("Groq", 1_301),
        )
        self.assertIsNone(groq["usage_window"]["tokens_minute_reset_at"])
        self.assertIsNone(
            groq["usage_window"]["provider_requests_reset_at"],
        )
        self.assertIsNone(
            groq["usage_window"]["provider_tokens_reset_at"],
        )
        self.assertIsNone(groq["next_reset_seconds"])
        self.assertEqual(limits["rpd"]["limit"], 1_000)
        self.assertEqual(limits["tpm"]["limit"], 8_000)
        self.assertTrue(limits["rpd"]["exact"])
        self.assertFalse(limits["tpm"]["exact"])
        self.assertTrue(saved)
        self.assertIsNone(
            saved[-1]["usage_windows"]["Groq"]["provider_requests_reset_at"],
        )
        self.assertIsNone(
            saved[-1]["usage_windows"]["Groq"]["provider_tokens_reset_at"],
        )

    @patch("core.ai_service.time.time", return_value=1_000)
    def test_groq_current_rpm_window_is_not_migrated_as_daily_state(
        self,
        _now,
    ):
        expected_daily_reset = _next_daily_reset("Groq", 1_000)
        persisted = {
            "usage": {
                "Groq": {
                    "requests_day": 7,
                    "requests_minute": 2,
                },
            },
            "rates": {
                "Groq": {
                    "rpd": {
                        "metric": "rpd",
                        "limit": 1_000,
                        "remaining": 993,
                        "reset_at": expected_daily_reset,
                        "source": "configured",
                        "exact": True,
                    },
                    "rpm": {
                        "metric": "rpm",
                        "limit": 30,
                        "remaining": 28,
                        "reset_at": 1_060,
                        "source": "configured",
                        "exact": True,
                    },
                },
            },
            "usage_windows": {
                "Groq": {
                    "daily_reset_at": expected_daily_reset,
                    "minute_reset_at": 1_060,
                    "provider_requests_reset_at": 1_060,
                },
            },
        }
        service = AIService(
            gemini_callable=lambda _prompt: {"ok": True},
            gemini_configured=lambda: True,
            gemini_quota_error_types=(AIProviderQuotaError,),
            gemini_error_types=(AIProviderError,),
            usage_state_loader=lambda: persisted,
            usage_state_saver=lambda _state: True,
        )
        service._providers = (
            _FakeProvider("Gemini", [], configured=False),
            _FakeProvider("Groq", []),
            _FakeProvider("Cloudflare", [], configured=False),
            _FakeProvider("OpenRouter", [], configured=False),
        )

        groq = next(
            row
            for row in service.status()["providers"]
            if row["name"] == "Groq"
        )
        limits = {
            row["metric"]: row
            for row in groq["limits"]
        }

        self.assertEqual(
            groq["usage_window"]["daily_reset_at"],
            expected_daily_reset,
        )
        self.assertEqual(
            groq["usage_window"]["provider_requests_reset_at"],
            1_060,
        )
        self.assertEqual(limits["rpd"]["remaining"], 993)

    @patch("core.ai_service.requests.post")
    def test_openrouter_429_uses_retry_and_reset_headers(self, post):
        response = unittest.mock.Mock()
        response.status_code = 429
        response.ok = False
        response.headers = {
            "Retry-After": "60",
            "X-RateLimit-Limit": "20",
            "X-RateLimit-Remaining": "0",
            "X-RateLimit-Reset": "60",
        }
        response.json.return_value = {
            "error": {
                "code": 429,
                "message": "Requests per minute limit reached",
            }
        }
        post.return_value = response
        provider = _OpenAICompatibleProvider(
            "OpenRouter",
            "key",
            "model:free",
            "https://example.test",
        )

        with self.assertRaises(AIProviderQuotaError) as raised:
            provider.generate_json("prompt")

        limit = raised.exception.limits[0]
        self.assertEqual(limit["metric"], "rpm")
        self.assertEqual(limit["limit"], 20)
        self.assertEqual(limit["remaining"], 0)
        self.assertTrue(limit["exact"])

    @patch("core.ai_service.requests.post")
    def test_cloudflare_disables_thinking_and_reads_neuron_usage(self, post):
        response = unittest.mock.Mock()
        response.status_code = 200
        response.ok = True
        response.headers = {"cf-ai-neurons": "12.5"}
        response.json.return_value = {
            "choices": [{"message": {"content": '{"ok": true}'}}],
            "usage": {
                "prompt_tokens": 20,
                "completion_tokens": 5,
                "total_tokens": 25,
            },
        }
        post.return_value = response
        account_id = "a" * 32
        provider = _OpenAICompatibleProvider(
            "Cloudflare",
            "key",
            CLOUDFLARE_DEFAULT_MODEL,
            _cloudflare_endpoint(account_id),
            request_options={
                "chat_template_kwargs": {
                    "enable_thinking": False,
                },
            },
        )

        result = provider.generate_json("prompt")

        self.assertTrue(result["ok"])
        request = post.call_args.kwargs
        self.assertEqual(
            post.call_args.args[0],
            _cloudflare_endpoint(account_id),
        )
        self.assertEqual(
            request["json"]["model"],
            CLOUDFLARE_DEFAULT_MODEL,
        )
        self.assertFalse(
            request["json"]["chat_template_kwargs"]["enable_thinking"]
        )
        self.assertNotIn("enable_thinking", request["json"])
        self.assertEqual(result.usage["neurons"], 12.5)

    @patch("core.ai_service.time.time", return_value=1_000)
    @patch("core.ai_service.requests.post")
    def test_cloudflare_daily_neuron_allocation_error_is_quota(
        self,
        post,
        _now,
    ):
        response = unittest.mock.Mock()
        response.status_code = 429
        response.ok = False
        response.headers = {}
        response.json.return_value = {
            "success": False,
            "errors": [{
                "code": 4006,
                "message": (
                    "AiError: you have used up your daily free allocation "
                    "of 10,000 neurons"
                ),
            }],
            "messages": [],
            "result": None,
        }
        post.return_value = response
        provider = _OpenAICompatibleProvider(
            "Cloudflare",
            "key",
            CLOUDFLARE_DEFAULT_MODEL,
            _cloudflare_endpoint("a" * 32),
        )

        with self.assertRaises(AIProviderQuotaError) as raised:
            provider.generate_json("prompt")

        self.assertIn("daily free allocation", str(raised.exception))
        self.assertEqual(raised.exception.limits[0]["metric"], "npd")
        self.assertEqual(raised.exception.limits[0]["remaining"], 0)
        self.assertGreater(raised.exception.limits[0]["reset_at"], 1_000)

    @patch("core.ai_service.requests.post")
    def test_cloudflare_limit_exceeded_error_is_quota(self, post):
        response = unittest.mock.Mock()
        response.status_code = 400
        response.ok = False
        response.headers = {}
        response.json.return_value = {
            "success": False,
            "errors": [{
                "code": 1000,
                "message": "Service limit exceeded",
            }],
        }
        post.return_value = response
        provider = _OpenAICompatibleProvider(
            "Cloudflare",
            "key",
            CLOUDFLARE_DEFAULT_MODEL,
            _cloudflare_endpoint("a" * 32),
        )

        with self.assertRaises(AIProviderQuotaError) as raised:
            provider.generate_json("prompt")

        self.assertEqual(raised.exception.limits[0]["metric"], "unknown")
        self.assertEqual(raised.exception.limits[0]["remaining"], 0)
        self.assertIsNone(raised.exception.limits[0]["reset_at"])

    @patch("core.ai_service.time.time", return_value=1_000)
    def test_persisted_unknown_quota_keeps_cloudflare_inactive(
        self,
        _now,
    ):
        persisted = {
            "rates": {
                "Cloudflare": {
                    "unknown": {
                        "metric": "unknown",
                        "limit": None,
                        "remaining": 0,
                        "reset_at": 1_300,
                        "source": "provider",
                        "exact": False,
                    },
                },
            },
        }
        service = AIService(
            gemini_callable=lambda _prompt: {"ok": True},
            gemini_configured=lambda: True,
            gemini_quota_error_types=(AIProviderQuotaError,),
            gemini_error_types=(AIProviderError,),
            usage_state_loader=lambda: persisted,
            usage_state_saver=lambda _state: True,
        )
        providers = (
            _FakeProvider("Gemini", [], configured=False),
            _FakeProvider("Groq", [], configured=False),
            _FakeProvider("Cloudflare", []),
            _FakeProvider("OpenRouter", [{"provider": "OpenRouter"}]),
        )
        service._providers = providers

        status = service.status()
        cloudflare = next(
            row for row in status["providers"]
            if row["name"] == "Cloudflare"
        )
        self.assertFalse(cloudflare["available"])
        self.assertTrue(cloudflare["quota_exhausted"])
        self.assertEqual(status["active_provider"], "OpenRouter")

        self.assertEqual(
            service.generate_json("prompt"),
            {"provider": "OpenRouter"},
        )
        self.assertEqual(providers[2].calls, 0)

        _now.return_value = 1_301
        status = service.status()
        cloudflare = next(
            row for row in status["providers"]
            if row["name"] == "Cloudflare"
        )
        self.assertTrue(cloudflare["available"])
        self.assertFalse(cloudflare["quota_exhausted"])

    @patch("core.ai_service.time.time", return_value=1_000)
    def test_cloudflare_quota_failure_persists_zero_remaining_for_restart(
        self,
        _now,
    ):
        service = self._service()
        service._providers = (
            _FakeProvider("Gemini", [], configured=False),
            _FakeProvider("Groq", [], configured=False),
            _FakeProvider("Cloudflare", []),
            _FakeProvider("OpenRouter", [], configured=False),
        )
        reset_at = _next_daily_reset("Cloudflare", 1_000)

        service._quarantine_provider(
            2,
            AIProviderQuotaError(
                "Cloudflare daily free allocation exhausted",
                limits=[{
                    "metric": "npd",
                    "remaining": 0,
                    "reset_at": reset_at,
                    "source": "provider",
                    "exact": False,
                }],
            ),
        )

        cloudflare = next(
            row
            for row in service.status()["providers"]
            if row["name"] == "Cloudflare"
        )
        npd = next(
            row for row in cloudflare["limits"] if row["metric"] == "npd"
        )
        self.assertEqual(npd["limit"], 10_000)
        self.assertEqual(npd["remaining"], 0)
        self.assertEqual(npd["reset_at"], "1970-01-02T00:00:00+00:00")
        self.assertIn("daily free allocation", cloudflare["reason"])

    def test_cloudflare_uses_documented_model_and_openai_endpoint(self):
        self.assertEqual(
            CLOUDFLARE_DEFAULT_MODEL,
            "@cf/qwen/qwen3.8-27b",
        )
        self.assertEqual(
            _cloudflare_endpoint("a" * 32),
            "https://api.cloudflare.com/client/v4/accounts/"
            f'{"a" * 32}/ai/v1/chat/completions',
        )

    def test_cloudflare_endpoint_rejects_unsafe_account_ids(self):
        self.assertEqual(_cloudflare_endpoint("abc/ai?token=leak"), "")
        self.assertEqual(_cloudflare_endpoint(""), "")
        account_id = "a" * 32
        self.assertEqual(
            _cloudflare_endpoint(account_id),
            "https://api.cloudflare.com/client/v4/accounts/"
            f"{account_id}/ai/v1/chat/completions",
        )

    def test_http_400_configuration_detection_is_selective(self):
        self.assertTrue(_is_configuration_error_response(
            400,
            {"error": {"message": "Model not found"}},
        ))
        self.assertFalse(_is_configuration_error_response(
            400,
            {"error": {"message": "Request body is too large"}},
        ))

    def test_cloudflare_documented_limits_track_tokens_and_neurons(self):
        service = self._service()
        service._providers = (
            _FakeProvider("Gemini", [], configured=False),
            _FakeProvider("Groq", [], configured=False),
            _FakeProvider(
                "Cloudflare",
                [AIResponse(
                    {"provider": "Cloudflare"},
                    usage={
                        "prompt_tokens": 20,
                        "completion_tokens": 5,
                        "total_tokens": 25,
                        "neurons": 12.5,
                    },
                )],
            ),
            _FakeProvider("OpenRouter", [], configured=False),
        )

        self.assertEqual(
            service.generate_json("prompt"),
            {"provider": "Cloudflare"},
        )
        cloudflare = next(
            row for row in service.status()["providers"]
            if row["name"] == "Cloudflare"
        )
        limits = {row["metric"]: row for row in cloudflare["limits"]}
        self.assertNotIn("rpm", limits)
        self.assertEqual(limits["npd"]["limit"], 10_000)
        self.assertEqual(limits["npd"]["used"], 12.5)
        self.assertEqual(limits["npd"]["source"], "configured")
        self.assertTrue(limits["npd"]["exact"])
        self.assertEqual(cloudflare["usage"]["total_tokens"], 25)
        self.assertEqual(cloudflare["usage"]["neurons"], 12.5)
        self.assertEqual(cloudflare["capabilities"]["context_window"], 262_144)

    def test_cloudflare_neuron_quota_resets_at_midnight_utc(self):
        now = datetime(2026, 9, 10, 18, 0, tzinfo=timezone.utc)
        reset = datetime.fromtimestamp(
            _next_daily_reset("Cloudflare", now.timestamp()),
            timezone.utc,
        )

        self.assertEqual(
            reset,
            datetime(2026, 9, 11, 0, 0, tzinfo=timezone.utc),
        )

    @patch("core.ai_service.time.time")
    def test_cloudflare_documented_remaining_resets_after_midnight(self, now):
        now.return_value = datetime(
            2026, 9, 10, 23, 59, tzinfo=timezone.utc
        ).timestamp()
        service = self._service()
        service._providers = (
            _FakeProvider("Gemini", [], configured=False),
            _FakeProvider("Groq", [], configured=False),
            _FakeProvider(
                "Cloudflare",
                [AIResponse(
                    {"provider": "Cloudflare"},
                    usage={"total_tokens": 1, "neurons": 25},
                )],
            ),
            _FakeProvider("OpenRouter", [], configured=False),
        )

        service.generate_json("prompt")
        now.return_value = datetime(
            2026, 9, 11, 0, 1, tzinfo=timezone.utc
        ).timestamp()
        cloudflare = next(
            row for row in service.status()["providers"]
            if row["name"] == "Cloudflare"
        )
        npd = next(
            row for row in cloudflare["limits"]
            if row["metric"] == "npd"
        )

        self.assertEqual(npd["remaining"], 10_000)

    def test_retry_after_accepts_http_date(self):
        now = datetime.now(timezone.utc)
        retry_at = now + timedelta(seconds=90)
        parsed = _retry_after_timestamp(
            retry_at.strftime("%a, %d %b %Y %H:%M:%S GMT"),
            now.timestamp(),
        )
        self.assertAlmostEqual(parsed, retry_at.timestamp(), delta=1)

    def test_status_is_safe_during_quota_state_updates(self):
        service = self._service()
        errors = []
        quota_error = AIProviderQuotaError(
            "RPM exhausted",
            limits=[{
                "metric": "rpm",
                "limit": 10,
                "remaining": 0,
                "reset_at": 9_999_999_999,
                "source": "provider",
                "exact": True,
            }],
        )

        def update_state():
            try:
                for _ in range(100):
                    service._quarantine_provider(0, quota_error)
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=update_state)
        thread.start()
        for _ in range(100):
            service.status()
        thread.join()

        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()

import json
import logging
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

import requests

from config import (
    AI_RECOVERY_INTERVAL_SECONDS,
    AI_RATE_LIMIT_DEFAULTS,
    CLOUDFLARE_ACCOUNT_ID,
    CLOUDFLARE_DEFAULT_MODEL,
    CLOUDFLARE_API_TOKEN,
    CLOUDFLARE_MODEL,
    DEEPSEEK_API_KEY,
    DEEPSEEK_MODEL,
    GROQ_API_KEY,
    GROQ_MODEL,
    OPENROUTER_API_KEY,
    OPENROUTER_MODEL,
    AI_PROVIDER_TIMEOUT_SECONDS,
)

logger = logging.getLogger(__name__)

_PROVIDER_ORDER = (
    "Groq",
    "OpenRouter",
    "Gemini",
    "DeepSeek",
    "Cloudflare",
)
CHANNEL_PROVIDER_ORDER = (
    "Groq",
    "OpenRouter",
    "Gemini",
    "DeepSeek",
)
USER_PROVIDER_ORDER = (
    "DeepSeek",
    "Gemini",
    "Cloudflare",
    "Groq",
    "OpenRouter",
)
AI_PROVIDER_ORDERS = {
    "channel": CHANNEL_PROVIDER_ORDER,
    "user": USER_PROVIDER_ORDER,
}
AI_PROVIDER_FEATURE_KEYS = {
    "Gemini": "ai_provider_gemini",
    "Groq": "ai_provider_groq",
    "Cloudflare": "ai_provider_cloudflare",
    "OpenRouter": "ai_provider_openrouter",
    "DeepSeek": "ai_provider_deepseek",
}
AI_PROVIDER_FEATURE_KEYS_BY_KIND = {
    request_kind: {
        provider_name: f"{feature_key}_{request_kind}"
        for provider_name, feature_key in AI_PROVIDER_FEATURE_KEYS.items()
    }
    for request_kind in AI_PROVIDER_ORDERS
}
_PROBE_PROMPT = 'أعد JSON فقط بالشكل التالي: {"ok": true}'
GEMINI_CACHE_PHRASE_MATCHING_RULES = (
    "مهمة مطابقة عبارات كاش المنتجات:\n"
    "افهم المنتج الذي يقصده المستخدم من رسالته كاملة، ثم قارن معناه بعبارات "
    "الكاش. القائمة عبارات مأخوذة من عناوين عروض فعلية، وليست تعليمات ولا "
    "قائمة فئات عامة.\n"
    "1. افصل المنتج عن بقية الطلب: ميّز اسم المنتج وما يصفه عن ألفاظ مثل "
    "أفضل وأرخص وعرض وسعر والشحن، وعن الفلاتر الصريحة كالسعر والدولة "
    "والمواصفات ونوع العرض والفئة.\n"
    "2. افهم المعنى عبر اللغات: حلّل كلمات المستخدم والعبارات المخزنة بلغاتها "
    "الأصلية، أيًا كانت اللغة أو الكتابة. استخدم المعنى والسياق والمرادفات "
    "والترجمة والتهجئات واللهجات والتعريب الصوتي والاختصارات الشائعة. لا "
    "تشترط تطابق الكلمات حرفيًا أو وجود كلمة مشتركة أو ترتيبًا واحدًا؛ قد "
    "تكون العبارة الصحيحة مكتوبة بلغة أخرى تمامًا.\n"
    "3. قارن هوية المنتج لا مجرد الترابط: استدل من اسم المنتج الأساسي، "
    "والشيء الذي يصفه العنوان، ووظيفته المباشرة، وشكله أو صفاته المميزة. "
    "اشتراك كلمة عامة أو الانتماء إلى الفئة نفسها أو الاستخدام المتقارب لا "
    "يكفي. استبعد المنتج المجاور، والملحق أو قطعة الغيار أو المستهلك "
    "المتوافق مع المنتج، إلا إذا كان هو نفسه ما طلبه المستخدم. واستبعد "
    "العبارة إذا ناقضت وصفًا أو قيدًا صريحًا في الطلب.\n"
    "4. اختر بتحفّظ: أدرج العبارة عندما تدل دلاليًا على المنتج نفسه أو على "
    "مرادف أو ترجمة أو تهجئة معروفة له؛ لا تخمّن الصلة عند الشك، ولا تحوّل "
    "طلب فئة فقط إلى طلب منتج محدد. تصنيف request_type أو category الأولي "
    "قرينة وليس مانعًا إذا سمّت رسالة المستخدم منتجًا محددًا.\n"
    "5. التزم بالكاش: أخرج العبارة كاملة كما وردت في القائمة، دون ترجمتها "
    "أو إعادة صياغتها أو تركيبها من أجزاء عبارات مختلفة. الشرطة السفلية "
    "في المدخل القديم تعادل المسافة للمطابقة فقط؛ أبقِ النص الأصلي والواصلة "
    "كما هما في القيمة المعادة. لا تضف أي كلمة أو مرادف غير موجود في "
    "القائمة.\n"
    "6. حافظ على منطق البحث: ضع العبارات البديلة التي تعني المنتج نفسه "
    "داخل مجموعة واحدة بمنطق OR، واجعل المنتجات المستقلة مجموعات منفصلة "
    "بمنطق AND. إذا كانت المهمة الحالية مقتصرة على المطابقة المركزة "
    "للعبارات، فأعد keywords وkeyword_groups فقط؛ لا تغيّر نوع الطلب أو "
    "السعر أو الشحن أو المواصفات أو الفئة أو نوع العرض. إذا لم توجد عبارة "
    "تدل فعلًا على المنتج المطلوب، فأعد القائمتين فارغتين بدل توسيع البحث "
    "إلى فئة أو منتج قريب."
)
_LIMIT_METRICS = ("rpm", "tpm", "rpd", "tpd", "npd")
_GROQ_TPD_OVERAGE_MAX_ATTEMPTS = 3
_GROQ_TPD_OVERAGE_RETRY_DELAY_SECONDS = 60
_DISPLAY_LIMIT_METRICS = {
    "Gemini": ("rpm", "rpd", "tpm", "tpd"),
    "Groq": ("rpm", "rpd", "tpm", "tpd"),
    "Cloudflare": ("npd",),
    "OpenRouter": ("rpm", "rpd", "tpm", "tpd"),
}
_PACIFIC = ZoneInfo("America/Los_Angeles")
_UTC_PLUS_ONE = timezone(timedelta(hours=1))
_MODEL_CAPABILITIES = {
    ("Gemini", "gemini-3.5-flash-lite"): {
        "context_window": 1_048_576,
        "max_output_tokens": 65_536,
    },
    ("Groq", "qwen/qwen3.8-27b"): {
        "context_window": 131_042,
        "max_output_tokens": 16_384,
    },
    ("Cloudflare", "@cf/qwen/qwen3.8-27b"): {
        "context_window": 262_144,
        "max_output_tokens": None,
    },
    ("OpenRouter", "openrouter/free"): {
        "context_window": 200_000,
        "max_output_tokens": None,
        "dynamic_model": True,
    },
}


class AIProviderError(RuntimeError):
    """Raised when one external AI provider cannot complete a request."""


class AIProviderQuotaError(AIProviderError):
    """Raised when a provider reports exhausted quota or rate limiting."""

    def __init__(self, message, limits=None):
        super().__init__(message)
        self.limits = list(limits or [])


class AIProviderConfigurationError(AIProviderError):
    """Raised when credentials or a configured model are invalid."""


class AIProviderTransientError(AIProviderError):
    """Raised for request-scoped network or provider availability failures."""


class AIServiceUnavailableError(RuntimeError):
    """Raised after every external AI provider becomes unavailable."""

    def __init__(
        self,
        message,
        quota_exhausted=False,
        retryable_recovery=False,
    ):
        super().__init__(message)
        self.quota_exhausted = quota_exhausted
        self.retryable_recovery = retryable_recovery


class AIResponse(dict):
    """Dictionary response carrying provider usage and limit metadata."""

    def __init__(self, data, usage=None, limits=None, model=None, provider=None):
        super().__init__(data)
        self.usage = dict(usage or {})
        self.limits = list(limits or [])
        self.model = model
        self.provider = provider


def _safe_number(value):
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if number.is_integer() else number


def _duration_seconds(value):
    if value in (None, ""):
        return None
    text = str(value).strip().lower()
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    if not re.fullmatch(
        r"(?:\d+(?:\.\d+)?\s*(?:d|h|m|s)\s*)+",
        text,
    ):
        return None
    total = 0.0
    matches = re.findall(r"(\d+(?:\.\d+)?)\s*(d|h|m|s)", text)
    if not matches:
        return None
    multipliers = {"d": 86400, "h": 3600, "m": 60, "s": 1}
    for amount, unit in matches:
        total += float(amount) * multipliers[unit]
    return total


def _reset_timestamp(value, now=None):
    now = time.time() if now is None else now
    if value in (None, ""):
        return None
    text = str(value).strip()
    numeric = _safe_number(text)
    if numeric is not None:
        if numeric > 10_000_000_000:
            return float(numeric) / 1000
        if numeric >= 1_000_000_000:
            return max(now, float(numeric))
        return now + max(0.0, float(numeric))
    duration = _duration_seconds(text)
    return now + duration if duration is not None else None


def _retry_after_timestamp(value, now=None):
    now = time.time() if now is None else now
    seconds = _duration_seconds(value)
    if seconds is not None:
        return now + seconds
    try:
        parsed = parsedate_to_datetime(str(value))
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(now, parsed.astimezone(timezone.utc).timestamp())


def _next_daily_reset(provider_name, now=None):
    now_dt = datetime.fromtimestamp(
        time.time() if now is None else now,
        timezone.utc,
    )
    zone = _PACIFIC if provider_name == "Gemini" else timezone.utc
    local_now = now_dt.astimezone(zone)
    tomorrow = local_now.date() + timedelta(days=1)
    reset = datetime.combine(tomorrow, datetime.min.time(), tzinfo=zone)
    return reset.astimezone(timezone.utc).timestamp()


def _next_utc_plus_one_midnight(now=None):
    now_dt = datetime.fromtimestamp(
        time.time() if now is None else now,
        timezone.utc,
    )
    local_now = now_dt.astimezone(_UTC_PLUS_ONE)
    tomorrow = local_now.date() + timedelta(days=1)
    reset = datetime.combine(
        tomorrow,
        datetime.min.time(),
        tzinfo=_UTC_PLUS_ONE,
    )
    return reset.astimezone(timezone.utc).timestamp()


def _cloudflare_endpoint(account_id):
    normalized = str(account_id or "").strip()
    if not re.fullmatch(r"[0-9a-fA-F]{32}", normalized):
        return ""
    return (
        "https://api.cloudflare.com/client/v4/accounts/"
        f"{normalized}/ai/v1/chat/completions"
    )


def _limit_snapshot(
    metric,
    limit=None,
    remaining=None,
    reset_at=None,
    source="provider",
    exact=True,
):
    return {
        "metric": metric if metric in _LIMIT_METRICS else "unknown",
        "limit": _safe_number(limit),
        "remaining": _safe_number(remaining),
        "reset_at": reset_at,
        "source": source,
        "exact": bool(exact),
    }


def _response_json(response):
    try:
        data = response.json()
    except (AttributeError, ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _error_message(data):
    if not isinstance(data, dict):
        return ""

    messages = []
    error = data.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        if message:
            messages.append(str(message))
    elif error:
        messages.append(str(error))

    errors = data.get("errors")
    if isinstance(errors, list):
        for item in errors:
            if isinstance(item, dict):
                message = item.get("message")
                if message:
                    messages.append(str(message))
            elif item:
                messages.append(str(item))

    return "; ".join(messages)


def _error_codes(data):
    if not isinstance(data, dict):
        return set()

    codes = set()
    entries = []
    error = data.get("error")
    if isinstance(error, dict):
        entries.append(error)
    errors = data.get("errors")
    if isinstance(errors, list):
        entries.extend(item for item in errors if isinstance(item, dict))
    for entry in entries:
        code = entry.get("code")
        if code not in (None, ""):
            codes.add(str(code))
    return codes


def _error_detail(provider_name, status_code, data):
    message = _error_message(data)
    suffix = f": {message}" if message else ""
    return f"{provider_name} request failed with HTTP {status_code}{suffix}"


def _is_quota_error_response(status_code, data):
    if status_code == 429:
        return True
    codes = _error_codes(data)
    if "4006" in codes:
        return True
    message = _error_message(data).casefold()
    return any(
        marker in message
        for marker in (
            "rate limit",
            "rate-limit",
            "rate_limit",
            "too many requests",
            "limit exceeded",
            "limits exceeded",
            "allocation",
            "quota exceeded",
            "quota exhausted",
            "daily free allocation",
            "insufficient balance",
        )
    )


def _is_configuration_error_response(status_code, data):
    if status_code in {401, 403, 404}:
        return True
    if status_code != 400:
        return False
    message = _error_message(data).casefold()
    return any(
        marker in message
        for marker in (
            "invalid api key",
            "authentication",
            "model not found",
            "model does not exist",
            "invalid model",
            "unknown model",
            "decommissioned",
            "model is no longer",
            "model does not support",
        )
    )


def _metric_from_text(text):
    normalized = str(text or "").casefold()
    is_daily = any(word in normalized for word in ("perday", "per day", "daily", "tpd", "rpd"))
    is_neuron = "neuron" in normalized
    is_token = any(word in normalized for word in ("token", "tpm", "tpd"))
    is_request = any(word in normalized for word in ("request", "rpm", "rpd"))
    if is_neuron:
        return "npd"
    if is_token:
        return "tpd" if is_daily else "tpm"
    if is_request:
        return "rpd" if is_daily else "rpm"
    return "unknown"


def gemini_quota_limits(response):
    """Extract Gemini QuotaFailure and RetryInfo details from a 429 response."""
    data = _response_json(response)
    error = data.get("error") if isinstance(data.get("error"), dict) else {}
    details = error.get("details") if isinstance(error.get("details"), list) else []
    now = time.time()
    retry_at = _retry_after_timestamp(
        response.headers.get("Retry-After"),
        now,
    )
    violations = []
    for detail in details:
        type_name = str(detail.get("@type") or "")
        if type_name.endswith("RetryInfo"):
            retry_at = (
                _retry_after_timestamp(detail.get("retryDelay"), now)
                or retry_at
            )
        if type_name.endswith("QuotaFailure"):
            violations.extend(detail.get("violations") or [])

    limits = []
    for violation in violations:
        quota_id = str(violation.get("quotaId") or "")
        metric = _metric_from_text(
            f"{quota_id} {violation.get('quotaMetric') or ''}"
        )
        reset_at = (
            _next_daily_reset("Gemini", now)
            if metric in {"rpd", "tpd"}
            else retry_at
            if retry_at is not None
            else None
        )
        limits.append(
            _limit_snapshot(
                metric,
                limit=violation.get("quotaValue"),
                remaining=0,
                reset_at=reset_at,
            )
        )
    if limits:
        return limits

    message = _error_message(data)
    metric = _metric_from_text(message)
    reset_at = (
        _next_daily_reset("Gemini", now)
        if metric in {"rpd", "tpd"}
        else retry_at
        if retry_at is not None
        else None
    )
    return [
        _limit_snapshot(
            metric,
            remaining=0,
            reset_at=reset_at,
            source="provider" if message or retry_at else "estimated",
            exact=False,
        )
    ]


def _openai_rate_limits(provider_name, response, data):
    headers = response.headers
    now = time.time()
    limits = []
    if provider_name == "Groq":
        request_limit = headers.get("x-ratelimit-limit-requests")
        token_limit = headers.get("x-ratelimit-limit-tokens")
        if request_limit is not None:
            limits.append(_limit_snapshot(
                "rpm",
                request_limit,
                headers.get("x-ratelimit-remaining-requests"),
                _reset_timestamp(headers.get("x-ratelimit-reset-requests"), now),
            ))
        if token_limit is not None:
            limits.append(_limit_snapshot(
                "tpm",
                token_limit,
                headers.get("x-ratelimit-remaining-tokens"),
                _reset_timestamp(headers.get("x-ratelimit-reset-tokens"), now),
            ))
    elif provider_name == "OpenRouter":
        generic_limit = headers.get("X-RateLimit-Limit")
        if generic_limit is not None:
            metric = _metric_from_text(_error_message(data))
            limits.append(_limit_snapshot(
                metric,
                generic_limit,
                headers.get("X-RateLimit-Remaining"),
                _reset_timestamp(headers.get("X-RateLimit-Reset"), now),
            ))
    return limits


def _quota_limits_for_openai_provider(provider_name, response, data):
    limits = _openai_rate_limits(provider_name, response, data)
    retry_at = _retry_after_timestamp(response.headers.get("Retry-After"))
    message = _error_message(data)
    if limits:
        exhausted = [
            limit
            for limit in limits
            if (
                _safe_number(limit.get("remaining")) is not None
                and _safe_number(limit.get("remaining")) <= 0
            )
        ]
        if exhausted:
            limits = exhausted
        else:
            metric = _metric_from_text(message)
            limits = [_limit_snapshot(
                metric,
                remaining=0,
                reset_at=retry_at,
                source="provider" if retry_at is not None else "estimated",
                exact=False,
            )]
    if not limits:
        metric = _metric_from_text(message)
        if (
            provider_name == "Cloudflare"
            and metric == "unknown"
            and any(word in message.casefold() for word in ("daily", "allocation"))
        ):
            metric = "npd"
        reset_at = (
            _next_daily_reset(provider_name)
            if metric in {"rpd", "tpd", "npd"}
            else retry_at
            if retry_at is not None
            else None
        )
        limits.append(_limit_snapshot(
            metric,
            remaining=0,
            reset_at=reset_at,
            source="provider" if message or retry_at else "estimated",
            exact=False,
        ))
    elif retry_at is not None:
        for limit in limits:
            if limit["reset_at"] is None:
                limit["reset_at"] = retry_at
    return limits


def _parse_json_object(text, provider_name):
    def content_text(value):
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return "".join(content_text(item) for item in value)
        if isinstance(value, dict):
            for key in ("text", "content", "value"):
                if key in value:
                    return content_text(value[key])
        return ""

    cleaned = content_text(text).strip()
    if not cleaned:
        raise AIProviderError(f"{provider_name} returned no text content")
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        result = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        decoder = json.JSONDecoder()
        result = None
        for index, character in enumerate(cleaned):
            if character != "{":
                continue
            try:
                candidate, _ = decoder.raw_decode(cleaned[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict):
                result = candidate
                break
        if result is None:
            raise AIProviderError(
                f"{provider_name} returned invalid JSON"
            ) from exc
    if not isinstance(result, dict):
        raise AIProviderError(
            f"{provider_name} returned a JSON value instead of an object"
        )
    return result


def _format_keyword_vocabulary_entry(keyword):
    """Render legacy underscore-joined phrases while preserving title text."""
    phrase = str(keyword or "").strip()
    if re.fullmatch(
        r"[\w\u0600-\u06ff]+(?:_[\w\u0600-\u06ff]+)+",
        phrase,
    ):
        return phrase.replace("_", " ")
    return phrase


class _CallableProvider:
    def __init__(
        self,
        name,
        callable_,
        configured,
        model,
        quota_error_types,
        error_types,
        configuration_error_types=(),
        request_context_loader=None,
    ):
        self.name = name
        self._callable = callable_
        self._configured = configured
        self._model = model
        self._quota_error_types = quota_error_types
        self._error_types = error_types
        self._configuration_error_types = configuration_error_types
        self._request_context_loader = request_context_loader

    @property
    def configured(self):
        return bool(self._configured())

    @property
    def can_attempt(self):
        return self.configured

    @property
    def model(self):
        return self._model()

    def generate_json(self, prompt, request_kind=None):
        try:
            context = (
                self._request_context_loader(request_kind)
                if self._request_context_loader
                else None
            )
            if context:
                request_prompt = prompt
                allowed_keywords = context.get("allowed_keywords")
                if allowed_keywords is not None:
                    vocabulary = (
                        "\n".join(
                            f"- {_format_keyword_vocabulary_entry(keyword)}"
                            for keyword in allowed_keywords
                        )
                        if not context.get("cached_content")
                        else "موجود داخل CachedContent المرفق"
                    )
                    request_prompt = (
                        f"{prompt}\n\n"
                        f"{GEMINI_CACHE_PHRASE_MATCHING_RULES}\n"
                        "قاعدة قاموس البحث المغلق: عند استخراج keywords، "
                        "يجب أن تطابق كل قيمة "
                        "في keywords وكل بديل في keyword_groups عبارة كاملة "
                        "واحدة من القاموس؛ لا تركّب عبارة من كلمات أو أجزاء "
                        "مأخوذة من عبارات مختلفة. اعتبر الشرطة السفلية في "
                        "العبارات القديمة بديلًا عن المسافة فقط، وحافظ على "
                        "الواصلة الأصلية في اسم المنتج. ضع المرادفات التي تصف "
                        "المفهوم نفسه داخل مجموعة واحدة كبدائل OR، ولا تضف "
                        "ترجمة أو عبارة غير موجودة في القاموس. إذا لم تجد "
                        "عبارة ذات صلة فعلية بالمنتج، أعد keywords و"
                        "keyword_groups كقائمتين فارغتين.\n"
                        "قاموس العبارات المسموح:\n"
                        f"{vocabulary}"
                    )
                allowed_categories = context.get(
                    "allowed_official_categories"
                )
                if allowed_categories is not None:
                    vocabulary = (
                        "\n".join(
                            f"- {category}" for category in allowed_categories
                        )
                        if not context.get("cached_content")
                        else "موجود داخل CachedContent المرفق"
                    )
                    request_prompt = (
                        f"{request_prompt}\n\n"
                        "قاموس الفئات الرسمي المغلق: عند تحديد قيد فئة صريح "
                        "في طلب المستخدم، اختر الاسم الرسمي الدقيق من قائمة "
                        "أسماء المستوى الثاني. لا تخترع اسمًا أو فئة عليا، "
                        "ولا تضف فئة لم يطلبها المستخدم. أعد البدائل المطلوبة "
                        "داخل official_categories؛ البدائل OR وبقية قيود البحث "
                        "تبقى AND. إذا لم توجد فئة مناسبة فأعد قائمة فارغة.\n"
                        "أسماء الفئات الرسمية:\n"
                        f"{vocabulary}"
                    )
                if request_kind == "user" and allowed_keywords is not None:
                    request_prompt = (
                        f"{request_prompt}\n\n"
                        "أضف إلى نتيجة JSON الحقل category_filter_requested "
                        "كقيمة boolean: true فقط إذا طلب المستخدم صراحةً فئة "
                        "أو قيد نوع/فئة على العروض، وfalse خلاف ذلك. عند true "
                        "لا تحوّل اسم الفئة إلى كلمات منتج؛ ستتم مطابقتها "
                        "من قاموس رسمي مستقل. إذا كان الطلب بحثًا صالحًا عن "
                        "فئة غير ممثلة بمفتاح داخلي، استخدم نوع category "
                        "المناسب وcategory=other عند الحاجة، ولا تستخدم other "
                        "إلا مع هذا القيد الصريح. أبقِ الطلبات غير البحثية "
                        "unsupported. هذا هو المفتاح الإضافي الوحيد المسموح "
                        "في نتيجة هذا الطلب."
                    )
                return self._callable(request_prompt, context=context)
            return self._callable(prompt)
        except self._configuration_error_types as exc:
            raise AIProviderConfigurationError(str(exc)) from exc
        except self._quota_error_types as exc:
            raise AIProviderQuotaError(
                str(exc),
                limits=getattr(exc, "limits", None),
            ) from exc
        except self._error_types as exc:
            raise AIProviderError(str(exc)) from exc
        except Exception as exc:
            raise AIProviderError(
                f"{self.name} returned an unexpected error"
            ) from exc


class _OpenAICompatibleProvider:
    def __init__(
        self,
        name,
        api_key,
        model,
        url,
        request_options=None,
        request_context_loader=None,
    ):
        self.name = name
        self.api_key = api_key
        self.model = model
        self.url = url
        self.request_options = dict(request_options or {})
        self._request_context_loader = request_context_loader

    @property
    def configured(self):
        return bool(self.api_key and self.model and self.url)

    @property
    def can_attempt(self):
        return self.configured

    def generate_json(self, prompt, request_kind=None):
        if not self.configured:
            raise AIProviderConfigurationError(
                f"{self.name} is not configured"
            )

        try:
            context = (
                self._request_context_loader(request_kind)
                if self._request_context_loader
                else None
            )
            messages = [{"role": "user", "content": prompt}]
            system_prompt = (
                context.get("system_prompt")
                if isinstance(context, dict)
                else None
            )
            if system_prompt:
                messages.insert(
                    0,
                    {"role": "system", "content": system_prompt},
                )
            logger.info(
                "AI provider request started provider=%s model=%s",
                self.name,
                self.model,
            )
            payload = {
                "model": self.model,
                "messages": messages,
                "temperature": 0.1,
                "response_format": {"type": "json_object"},
            }
            payload.update(self.request_options)
            response = requests.post(
                self.url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=AI_PROVIDER_TIMEOUT_SECONDS,
            )
            logger.info(
                "AI provider response received provider=%s status=%s",
                self.name,
                response.status_code,
            )
        except requests.RequestException as exc:
            raise AIProviderTransientError(
                f"{self.name} request failed while contacting the provider"
            ) from exc

        data = _response_json(response)
        if _is_quota_error_response(response.status_code, data):
            raise AIProviderQuotaError(
                _error_detail(self.name, response.status_code, data),
                limits=_quota_limits_for_openai_provider(
                    self.name,
                    response,
                    data,
                ),
            )
        if _is_configuration_error_response(response.status_code, data):
            raise AIProviderConfigurationError(
                _error_detail(self.name, response.status_code, data)
            )
        if response.status_code in {500, 502, 503, 504}:
            raise AIProviderTransientError(
                _error_detail(self.name, response.status_code, data)
            )
        if not response.ok:
            raise AIProviderError(
                _error_detail(self.name, response.status_code, data)
            )

        if (data.get("error") or data.get("errors")) and not data.get("choices"):
            if _is_quota_error_response(response.status_code, data):
                raise AIProviderQuotaError(
                    f"{self.name} returned an error response"
                    f": {_error_message(data)}"
                    if _error_message(data)
                    else f"{self.name} returned an error response",
                    limits=_quota_limits_for_openai_provider(
                        self.name,
                        response,
                        data,
                    ),
                )
            detail = _error_message(data)
            raise AIProviderError(
                f"{self.name} returned an error response"
                f": {detail}" if detail else f"{self.name} returned an error response"
            )

        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise AIProviderError(
                f"{self.name} returned no usable content"
            ) from exc
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        if usage.get("neurons") is None:
            neurons = _safe_number(response.headers.get("cf-ai-neurons"))
            if neurons is not None:
                usage = {**usage, "neurons": neurons}
        result = AIResponse(
            _parse_json_object(content, self.name),
            usage=usage,
            limits=_openai_rate_limits(self.name, response, data),
            model=data.get("model"),
        )
        if isinstance(context, dict):
            if "allowed_keywords" in context:
                result.keyword_cache_active = True
                result.keyword_cache_keywords = list(
                    context.get("allowed_keywords") or []
                )
            if "allowed_official_categories" in context:
                result.official_category_cache_active = True
                result.official_category_names = list(
                    context.get("allowed_official_categories") or []
                )
        return result


class AIService:
    """Route AI requests through external providers in a fixed order."""

    def __init__(
        self,
        gemini_callable,
        gemini_configured,
        gemini_quota_error_types,
        gemini_error_types,
        gemini_model=lambda: "",
        recovery_interval_seconds=AI_RECOVERY_INTERVAL_SECONDS,
        gemini_configuration_error_types=(),
        gemini_request_context_loader=None,
        provider_enabled_reader=None,
        provider_enabled_writer=None,
        usage_state_loader=None,
        usage_state_saver=None,
        deepseek_request_context_loader=None,
    ):
        self._providers = (
            _OpenAICompatibleProvider(
                "Groq",
                GROQ_API_KEY,
                GROQ_MODEL,
                "https://api.groq.com/openai/v1/chat/completions",
            ),
            _OpenAICompatibleProvider(
                "OpenRouter",
                OPENROUTER_API_KEY,
                OPENROUTER_MODEL,
                "https://openrouter.ai/api/v1/chat/completions",
            ),
            _CallableProvider(
                "Gemini",
                gemini_callable,
                gemini_configured,
                gemini_model,
                gemini_quota_error_types,
                gemini_error_types,
                gemini_configuration_error_types,
                gemini_request_context_loader,
            ),
            _OpenAICompatibleProvider(
                "DeepSeek",
                DEEPSEEK_API_KEY,
                DEEPSEEK_MODEL,
                "https://api.deepseek.com/chat/completions",
                request_options={
                    "thinking": {
                        "type": "disabled",
                    },
                },
                request_context_loader=deepseek_request_context_loader,
            ),
            _OpenAICompatibleProvider(
                "Cloudflare",
                CLOUDFLARE_API_TOKEN,
                CLOUDFLARE_MODEL or CLOUDFLARE_DEFAULT_MODEL,
                _cloudflare_endpoint(CLOUDFLARE_ACCOUNT_ID),
                request_options={
                    "chat_template_kwargs": {
                        "enable_thinking": False,
                    },
                },
            ),
        )
        self._recovery_interval = max(60, int(recovery_interval_seconds))
        self._lock = threading.RLock()
        self._request_lock = threading.Lock()
        self._provider_state_lock = threading.Lock()
        self._active_index = 0
        self._unavailable = {}
        self._rate_state = {
            provider.name: {}
            for provider in self._providers
        }
        self._usage_state = {
            provider.name: {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "neurons": 0,
                "requests_minute": 0,
                "requests_day": 0,
                "tokens_minute": 0,
                "tokens_day": 0,
                **(
                    {
                        "observed_requests_day": 0,
                        "observed_tokens_day": 0,
                    }
                    if provider.name == "DeepSeek"
                    else {}
                ),
            }
            for provider in self._providers
        }
        self._usage_windows = {
            provider.name: {
                "minute_reset_at": None,
                "tokens_minute_reset_at": None,
                "daily_reset_at": None,
                "tokens_reset_at": None,
                "neurons_reset_at": None,
                "provider_requests_reset_at": None,
                "provider_tokens_reset_at": None,
                **(
                    {"observed_reset_at": None}
                    if provider.name == "DeepSeek"
                    else {}
                ),
            }
            for provider in self._providers
        }
        self._usage_state_loader = usage_state_loader
        self._usage_state_saver = usage_state_saver
        self._storage_loaded = False
        self._local_mode = False
        self._next_probe_at = None
        self._recovery_thread = None
        self._quota_worker_thread = None
        self._quota_worker_stop = threading.Event()
        self._provider_enabled_reader = provider_enabled_reader
        self._provider_enabled_writer = provider_enabled_writer
        self._provider_enabled = {
            (request_kind, provider.name): None
            for request_kind in AI_PROVIDER_ORDERS
            for provider in self._providers
        }
        self._provider_test_locks = {
            provider.name: threading.Lock()
            for provider in self._providers
        }

    def generate_json(self, prompt, request_kind="user", provider_name=None):
        if provider_name and not any(
            provider.name == provider_name for provider in self._providers
        ):
            raise ValueError(f"Unknown AI provider: {provider_name}")
        started_at = time.perf_counter()
        result = None
        try:
            result = self._generate_json(
                prompt,
                request_kind=request_kind,
                provider_name=provider_name,
            )
            return result
        finally:
            logger.info(
                "AI request completed kind=%s provider=%s duration_ms=%.1f",
                request_kind,
                getattr(result, "provider", None),
                (time.perf_counter() - started_at) * 1000,
            )

    def _generate_json(self, prompt, request_kind="user", provider_name=None):
        with self._lock:
            self._load_persisted_state_locked()
            self._refresh_expired_locked()
            ordered_providers = self._ordered_providers(request_kind)
            start_index = (
                self._next_active_index_locked(request_kind)
                if provider_name is None
                else None
            )

        last_error = None
        provider_candidates = (
            ordered_providers[start_index:]
            if provider_name is None
            else (
                provider
                for provider in ordered_providers
                if provider.name == provider_name
            )
        )
        for provider in provider_candidates:
            if not self._provider_allowed_for_request(provider, request_kind):
                continue
            with self._lock:
                if (
                    not self._is_provider_enabled_locked(
                        provider.name,
                        request_kind,
                    )
                    or provider.name in self._unavailable
                ):
                    continue
            if not provider.can_attempt:
                error = AIProviderConfigurationError(
                    f"{provider.name} is not configured"
                )
                self._quarantine_provider(
                    self._providers.index(provider),
                    error,
                )
                last_error = error
                continue
            try:
                with self._lock:
                    self._load_persisted_state_locked()
                    self._refresh_expired_locked()
                    quota_blockers = self._local_quota_blockers_locked(provider)
                    allow_groq_tpd_overage = (
                        self._allows_groq_tpd_overage_locked(
                            provider,
                            quota_blockers,
                        )
                    )
                    if quota_blockers and not allow_groq_tpd_overage:
                        self._quarantine_local_quota_locked(
                            provider,
                            quota_blockers,
                        )
                        last_error = AIProviderQuotaError(
                            f"{provider.name} local quota window is exhausted"
                        )
                        logger.info(
                            "AI provider skipped provider=%s reason=local_quota "
                            "reset_at=%s",
                            provider.name,
                            max(item["reset_at"] for item in quota_blockers),
                        )
                        continue
                    if allow_groq_tpd_overage:
                        logger.info(
                            "AI provider overage attempt provider=%s metric=TPD "
                            "request_limits_available=true",
                            provider.name,
                        )
                logger.info(
                    "AI provider attempt provider=%s model=%s",
                    provider.name,
                    getattr(provider, "model", None),
                )
                result = self._generate_provider_request(
                    provider,
                    prompt,
                    allow_groq_tpd_overage,
                    request_kind,
                )
            except (AIProviderQuotaError, AIProviderConfigurationError) as exc:
                self._quarantine_provider(
                    self._providers.index(provider),
                    exc,
                )
                logger.warning(
                    "AI provider failed provider=%s error_type=%s",
                    provider.name,
                    type(exc).__name__,
                )
                last_error = exc
                continue
            except AIProviderError as exc:
                last_error = exc
                logger.warning(
                    "AI provider failed this request provider=%s error_type=%s; "
                    "trying the next provider without quarantining it",
                    provider.name,
                    type(exc).__name__,
                )
                continue

            with self._lock:
                self._record_success_locked(provider, result)
                self._active_index = self._next_active_index_locked()
                self._local_mode = self._active_index >= len(
                    self._ordered_providers("user")
                )
                self._next_probe_at = (
                    self._nearest_recovery_locked()
                    if self._local_mode
                    else None
                )
            logger.info(
                "AI provider succeeded provider=%s model=%s",
                provider.name,
                getattr(result, "model", None)
                or getattr(provider, "model", None),
            )
            if isinstance(result, AIResponse):
                result.provider = provider.name
                return result
            if isinstance(result, dict):
                return AIResponse(result, provider=provider.name)
            return result

        with self._lock:
            enabled_providers = [
                provider
                for provider in self._providers
                if (
                    self._provider_allowed_for_request(provider, request_kind)
                    and self._is_provider_enabled_locked(
                        provider.name,
                        request_kind,
                    )
                )
            ]
            all_persistently_unavailable = not enabled_providers or all(
                provider.name in self._unavailable
                for provider in enabled_providers
            )
            quota_exhausted = self._quota_exhausted_locked(request_kind)
            retryable_recovery = self._has_scheduled_recovery_locked(request_kind)
        if (
            self._request_profile(request_kind) == "user"
            and all_persistently_unavailable
        ):
            self._enter_local_mode()

        detail = str(last_error) if last_error else "no provider is available"
        raise AIServiceUnavailableError(
            f"All external AI providers are unavailable: {detail}",
            quota_exhausted=quota_exhausted,
            retryable_recovery=retryable_recovery,
        )

    def _allows_groq_tpd_overage_locked(self, provider, blockers):
        """Allow real Groq requests when only the daily token limit is full."""
        return (
            provider.name == "Groq"
            and bool(blockers)
            and {item.get("metric") for item in blockers} == {"tpd"}
        )

    def _generate_provider_request(
        self,
        provider,
        prompt,
        allow_groq_tpd_overage,
        request_kind="user",
    ):
        attempts = (
            _GROQ_TPD_OVERAGE_MAX_ATTEMPTS
            if allow_groq_tpd_overage
            else 1
        )
        last_error = None

        for attempt in range(attempts):
            with self._lock:
                self._load_persisted_state_locked()
                self._refresh_expired_locked()
                if attempt:
                    blockers = self._local_quota_blockers_locked(provider)
                    if blockers and not self._allows_groq_tpd_overage_locked(
                        provider,
                        blockers,
                    ):
                        limits = [
                            _limit_snapshot(
                                item["metric"],
                                limit=item.get("limit"),
                                remaining=0,
                                reset_at=item.get("reset_at"),
                                source="estimated",
                                exact=False,
                            )
                            for item in blockers
                        ]
                        last_error = AIProviderQuotaError(
                            f"{provider.name} local quota window is exhausted",
                            limits=limits,
                        )
                        break
                self._record_attempt_locked(provider)

            try:
                if isinstance(
                    provider,
                    (_CallableProvider, _OpenAICompatibleProvider),
                ):
                    return provider.generate_json(
                        prompt,
                        request_kind=request_kind,
                    )
                return provider.generate_json(prompt)
            except AIProviderConfigurationError:
                raise
            except AIProviderError as exc:
                last_error = exc
                if attempt + 1 >= attempts:
                    break
                logger.warning(
                    "AI provider overage attempt failed provider=%s "
                    "attempt=%s/%s; retrying in %ss",
                    provider.name,
                    attempt + 1,
                    attempts,
                    _GROQ_TPD_OVERAGE_RETRY_DELAY_SECONDS,
                )
                time.sleep(_GROQ_TPD_OVERAGE_RETRY_DELAY_SECONDS)

        if allow_groq_tpd_overage:
            raise self._groq_tpd_overage_failure(provider, last_error)
        if last_error is not None:
            raise last_error
        raise AIProviderError(f"{provider.name} request failed")

    def _groq_tpd_overage_failure(self, provider, error):
        now = time.time()
        with self._lock:
            self._load_persisted_state_locked()
            self._ensure_default_limits_locked(provider)
            reset_at = self._quota_reset_at_locked(provider, "tpd", now)
            configured_limit = self._configured_limit(provider, "tpd")

        message = str(error) if error else (
            f"{provider.name} failed after the TPD overage attempts"
        )
        limits = (
            list(error.limits)
            if isinstance(error, AIProviderQuotaError)
            else []
        )
        if not any(limit.get("metric") == "tpd" for limit in limits):
            limits.append(
                _limit_snapshot(
                    "tpd",
                    limit=configured_limit,
                    remaining=0,
                    reset_at=reset_at,
                    source="estimated",
                    exact=False,
                )
            )
        return AIProviderQuotaError(
            message,
            limits=limits,
        )

    def _quarantine_provider(self, index, error):
        provider = self._providers[index]
        if isinstance(error, AIProviderQuotaError):
            kind = "quota"
            limits = list(error.limits or [])
            if not limits:
                limits = [_limit_snapshot(
                    "unknown",
                    remaining=0,
                    reset_at=time.time() + self._recovery_interval,
                    source="estimated",
                    exact=False,
                )]
            now = time.time()
            for limit in limits:
                if limit.get("reset_at") is None:
                    metric = limit.get("metric")
                    limit["reset_at"] = (
                        _next_daily_reset(provider.name, now)
                        if metric in {"rpd", "tpd", "npd"}
                        else now + self._recovery_interval
                    )
            eligible_at = max(
                limit["reset_at"]
                for limit in limits
                if limit.get("reset_at") is not None
            )
            metrics = "/".join(
                sorted({limit.get("metric", "unknown").upper() for limit in limits})
            )
            reason = str(error) or f"{metrics} limit reached"
        elif provider.configured:
            kind = "configuration"
            reason = str(error)
            eligible_at = None
        else:
            kind = "unconfigured"
            reason = str(error)
            eligible_at = None
        with self._lock:
            self._load_persisted_state_locked()
            if kind == "quota":
                for limit in limits:
                    metric = limit.get("metric", "unknown")
                    self._sync_provider_window_locked(
                        provider,
                        limit,
                        quota_exhausted=True,
                    )
                    configured_limit = self._configured_limit(provider, metric)
                    if configured_limit is None:
                        self._rate_state[provider.name][metric] = dict(limit)
                        continue

                    # A provider can reject a request because its account has
                    # exhausted a fixed quota before this process observed the
                    # same usage. Keep the configured limit for display, but
                    # persist zero remaining so a restart does not immediately
                    # repeat the rejected request.
                    snapshot = self._rate_state[provider.name].get(metric)
                    if snapshot is None:
                        snapshot = _limit_snapshot(
                            metric,
                            limit=configured_limit,
                            remaining=0,
                            reset_at=limit.get("reset_at"),
                            source="configured",
                            exact=True,
                        )
                        self._rate_state[provider.name][metric] = snapshot
                    else:
                        snapshot["remaining"] = 0
                        if limit.get("reset_at") is not None:
                            snapshot["reset_at"] = limit["reset_at"]
                self._learn_unknown_limit_locked(provider, limits)
            self._unavailable[provider.name] = {
                "kind": kind,
                "reason": reason,
                "since": datetime.now(timezone.utc),
                "eligible_at": eligible_at,
            }
            self._active_index = self._next_active_index_locked()
            self._persist_state_locked()
        logger.warning(
            "AI provider %s is unavailable kind=%s; moving to the next provider",
            provider.name,
            kind,
        )

    @staticmethod
    def _provider_allowed_for_request(provider, request_kind):
        return provider.name in AI_PROVIDER_ORDERS[
            AIService._request_profile(request_kind)
        ]

    @staticmethod
    def _request_profile(request_kind):
        return "channel" if request_kind == "channel" else "user"

    def _ordered_providers(self, request_kind="user"):
        providers_by_name = {
            provider.name: provider
            for provider in self._providers
        }
        return tuple(
            providers_by_name[name]
            for name in AI_PROVIDER_ORDERS[
                self._request_profile(request_kind)
            ]
            if name in providers_by_name
        )

    def _next_active_index_locked(self, request_kind="user"):
        ordered_providers = self._ordered_providers(request_kind)
        for index, provider in enumerate(ordered_providers):
            if (
                self._provider_allowed_for_request(provider, request_kind)
                and self._is_provider_enabled_locked(
                    provider.name,
                    request_kind,
                )
                and provider.can_attempt
                and provider.name not in self._unavailable
            ):
                return index
        return len(ordered_providers)

    def _active_provider_name_locked(self, request_kind="user", local_mode=False):
        ordered_providers = self._ordered_providers(request_kind)
        active_index = self._next_active_index_locked(request_kind)
        if active_index < len(ordered_providers):
            return ordered_providers[active_index].name
        if self._request_profile(request_kind) == "user" and local_mode:
            return "Local service"
        return None

    def _provider_enabled_for_any_request_locked(self, provider_name):
        provider = next(
            (
                candidate
                for candidate in self._providers
                if candidate.name == provider_name
            ),
            None,
        )
        if provider is None:
            return False
        return any(
            self._provider_allowed_for_request(provider, request_kind)
            and self._is_provider_enabled_locked(
                provider_name,
                request_kind,
            )
            for request_kind in AI_PROVIDER_ORDERS
        )

    def _is_provider_enabled_locked(self, provider_name, request_kind="user"):
        request_profile = self._request_profile(request_kind)
        cache_key = (request_profile, provider_name)
        enabled = self._provider_enabled.get(cache_key)
        if enabled is not None:
            return enabled
        feature_key = AI_PROVIDER_FEATURE_KEYS_BY_KIND[
            request_profile
        ][provider_name]
        try:
            enabled = (
                bool(self._provider_enabled_reader(feature_key))
                if self._provider_enabled_reader
                else True
            )
        except Exception:
            logger.exception(
                "Could not load admin state for AI provider %s (%s); "
                "enabling it",
                provider_name,
                request_profile,
            )
            enabled = True
        self._provider_enabled[cache_key] = enabled
        return enabled

    def set_provider_enabled(
        self,
        provider_name,
        enabled,
        request_kind="user",
    ):
        if provider_name not in AI_PROVIDER_FEATURE_KEYS:
            raise ValueError(f"Unknown AI provider: {provider_name}")
        request_profile = self._request_profile(request_kind)
        if (
            provider_name == "Cloudflare"
            and request_profile == "channel"
            and enabled
        ):
            logger.info(
                "Ignoring attempt to enable Cloudflare for channel analysis"
            )
            return False
        feature_key = AI_PROVIDER_FEATURE_KEYS_BY_KIND[
            request_profile
        ][provider_name]
        with self._provider_state_lock:
            saved = (
                bool(self._provider_enabled_writer(feature_key, enabled))
                if self._provider_enabled_writer
                else bool(enabled)
            )
            with self._lock:
                self._provider_enabled[(request_profile, provider_name)] = saved
                if request_profile == "user":
                    next_index = self._next_active_index_locked("user")
                    self._active_index = next_index
                    self._local_mode = next_index >= len(
                        self._ordered_providers("user")
                    )
                    self._next_probe_at = (
                        self._nearest_recovery_locked("user")
                        if self._local_mode
                        else None
                    )
            return saved

    def test_provider(self, provider_name):
        """Test one provider without changing routing or enabled state."""
        provider = next(
            (
                candidate
                for candidate in self._providers
                if candidate.name == provider_name
            ),
            None,
        )
        if provider is None:
            raise ValueError(f"Unknown AI provider: {provider_name}")

        test_lock = self._provider_test_locks[provider_name]
        if not test_lock.acquire(blocking=False):
            return {
                "ok": False,
                "kind": "busy",
                "provider": provider_name,
            }
        try:
            if not provider.configured:
                return {
                    "ok": False,
                    "kind": "configuration",
                    "provider": provider_name,
                }
            try:
                with self._lock:
                    self._load_persisted_state_locked()
                    self._refresh_expired_locked()
                    quota_blockers = self._local_quota_blockers_locked(provider)
                    if quota_blockers:
                        self._quarantine_local_quota_locked(
                            provider,
                            quota_blockers,
                        )
                        return {
                            "ok": False,
                            "kind": "quota",
                            "provider": provider_name,
                        }
                    self._record_attempt_locked(provider)
                result = provider.generate_json(_PROBE_PROMPT)
            except AIProviderQuotaError:
                return {
                    "ok": False,
                    "kind": "quota",
                    "provider": provider_name,
                }
            except AIProviderConfigurationError:
                return {
                    "ok": False,
                    "kind": "configuration",
                    "provider": provider_name,
                }
            except AIProviderTransientError:
                return {
                    "ok": False,
                    "kind": "transient",
                    "provider": provider_name,
                }
            except AIProviderError:
                return {
                    "ok": False,
                    "kind": "provider",
                    "provider": provider_name,
                }
            except Exception:
                logger.exception(
                    "Unexpected error while testing AI provider %s",
                    provider_name,
                )
                return {
                    "ok": False,
                    "kind": "provider",
                    "provider": provider_name,
                }

            with self._lock:
                self._load_persisted_state_locked()
                self._refresh_expired_locked()
                self._record_success_locked(provider, result)
            return {
                "ok": True,
                "kind": "success",
                "provider": provider_name,
                "model": getattr(result, "model", None) or provider.model,
            }
        finally:
            test_lock.release()

    def _refresh_expired_locked(self):
        now = time.time()
        changed = False
        expired = [
            name
            for name, state in self._unavailable.items()
            if state.get("kind") == "quota"
            and state.get("eligible_at") is not None
            and state["eligible_at"] <= now
        ]
        for name in expired:
            self._unavailable.pop(name, None)
            changed = True

        for provider in self._providers:
            for metric, snapshot in list(
                self._rate_state[provider.name].items()
            ):
                if metric == "unknown":
                    if (
                        snapshot.get("reset_at") is not None
                        and snapshot["reset_at"] <= now
                    ):
                        snapshot["remaining"] = None
                        snapshot["reset_at"] = None
                        snapshot["source"] = "estimated"
                        snapshot["exact"] = False
                        changed = True
                    continue
                if (
                    metric in {"rpm", "tpm"}
                    and snapshot.get("reset_at") is not None
                    and not self._short_metric_exhausted_locked(
                        provider,
                        metric,
                        snapshot,
                    )
                    and not self._provider_window_is_authoritative_locked(
                        provider,
                        metric,
                        snapshot,
                    )
                ):
                    self._clear_short_metric_schedule_locked(
                        provider,
                        metric,
                        snapshot,
                    )
                    changed = True
                    continue
                if (
                    metric in _LIMIT_METRICS
                    and snapshot.get("reset_at") is not None
                    and snapshot["reset_at"] <= now
                ):
                    snapshot["remaining"] = snapshot.get("limit")
                    if snapshot.get("source") == "provider":
                        snapshot["source"] = "estimated"
                        snapshot["exact"] = False
                    if metric in {"rpm", "tpm"}:
                        self._reset_short_metric_locked(
                            provider,
                            metric,
                            snapshot,
                        )
                    else:
                        snapshot["reset_at"] = self._window_reset_at(
                            provider.name,
                            metric,
                            now,
                        )
                    changed = True
            windows = self._usage_windows[provider.name]
            if (
                provider.name == "DeepSeek"
                and self._refresh_deepseek_observed_usage_window_locked(now)
            ):
                changed = True
            if (
                windows.get("tokens_reset_at") is not None
                and windows["tokens_reset_at"] <= now
            ):
                usage = self._usage_state[provider.name]
                usage["prompt_tokens"] = 0
                usage["completion_tokens"] = 0
                usage["total_tokens"] = 0
                usage["tokens_day"] = 0
                windows["tokens_reset_at"] = _next_daily_reset(
                    provider.name,
                    now,
                )
                changed = True
            if (
                windows.get("minute_reset_at") is not None
                and windows["minute_reset_at"] <= now
            ):
                usage = self._usage_state[provider.name]
                usage["requests_minute"] = 0
                windows["minute_reset_at"] = None
                if (
                    provider.name == "Groq"
                    and windows.get("provider_requests_reset_at") is not None
                    and windows["provider_requests_reset_at"] <= now
                ):
                    windows["provider_requests_reset_at"] = None
                changed = True
            if (
                windows.get("tokens_minute_reset_at") is not None
                and windows["tokens_minute_reset_at"] <= now
            ):
                usage = self._usage_state[provider.name]
                usage["tokens_minute"] = 0
                windows["tokens_minute_reset_at"] = None
                if (
                    provider.name == "Groq"
                    and windows.get("provider_tokens_reset_at") is not None
                    and windows["provider_tokens_reset_at"] <= now
                ):
                    windows["provider_tokens_reset_at"] = None
                changed = True
            if (
                windows.get("daily_reset_at") is not None
                and windows["daily_reset_at"] <= now
            ):
                self._usage_state[provider.name]["requests_day"] = 0
                windows["daily_reset_at"] = _next_daily_reset(
                    provider.name,
                    now,
                )
                changed = True
            if (
                windows.get("neurons_reset_at") is not None
                and windows["neurons_reset_at"] <= now
            ):
                self._usage_state[provider.name]["neurons"] = 0
                windows["neurons_reset_at"] = _next_daily_reset(
                    provider.name,
                    now,
                )
                changed = True
        active_index = self._next_active_index_locked("user")
        if active_index < len(self._ordered_providers("user")):
            self._active_index = active_index
            self._local_mode = False
            self._next_probe_at = None
        elif expired:
            self._active_index = len(self._ordered_providers("user"))
        if changed:
            self._persist_state_locked()

    def _refresh_deepseek_observed_usage_window_locked(self, now=None):
        usage = self._usage_state.get("DeepSeek")
        windows = self._usage_windows.get("DeepSeek")
        if not isinstance(usage, dict) or not isinstance(windows, dict):
            return False

        current = time.time() if now is None else now
        reset_at = _safe_number(windows.get("observed_reset_at"))
        if reset_at is None:
            windows["observed_reset_at"] = _next_utc_plus_one_midnight(current)
            return True
        if reset_at > current:
            return False

        usage["observed_requests_day"] = 0
        usage["observed_tokens_day"] = 0
        windows["observed_reset_at"] = _next_utc_plus_one_midnight(current)
        return True

    def _short_metric_window_key(self, metric):
        return {
            "rpm": "minute_reset_at",
            "tpm": "tokens_minute_reset_at",
        }.get(metric)

    def _short_metric_exhausted_locked(self, provider, metric, snapshot):
        if metric not in {"rpm", "tpm"}:
            return False
        limit = _safe_number(snapshot.get("limit"))
        if limit is None:
            return False
        remaining = _safe_number(snapshot.get("remaining"))
        used = self._usage_for_metric_locked(provider, metric)
        return (
            (remaining is not None and remaining <= 0)
            or used >= limit
        )

    def _provider_window_is_authoritative_locked(
        self,
        provider,
        metric,
        snapshot,
    ):
        if snapshot.get("source") == "provider":
            return True
        if provider.name == "Groq":
            provider_window_key = {
                "rpm": "provider_requests_reset_at",
                "tpm": "provider_tokens_reset_at",
            }.get(metric)
            if provider_window_key:
                return (
                    self._usage_windows[provider.name].get(
                        provider_window_key
                    )
                    == snapshot.get("reset_at")
                )
        return False

    def _clear_short_metric_schedule_locked(
        self,
        provider,
        metric,
        snapshot,
    ):
        snapshot["reset_at"] = None
        window_key = self._short_metric_window_key(metric)
        if window_key:
            self._usage_windows[provider.name][window_key] = None
        if provider.name == "Groq":
            provider_window_key = {
                "rpm": "provider_requests_reset_at",
                "tpm": "provider_tokens_reset_at",
            }.get(metric)
            if provider_window_key:
                self._usage_windows[provider.name][provider_window_key] = None

    def _reset_short_metric_locked(self, provider, metric, snapshot):
        self._clear_short_metric_schedule_locked(provider, metric, snapshot)
        if metric == "rpm":
            self._usage_state[provider.name]["requests_minute"] = 0
        elif metric == "tpm":
            self._usage_state[provider.name]["tokens_minute"] = 0

    def _window_reset_at(self, provider_name, metric, now):
        if metric in {"rpm", "tpm"}:
            return (int(now // 60) + 1) * 60
        if metric in {"rpd", "tpd", "npd"}:
            return _next_daily_reset(provider_name, now)
        return None

    def _configured_limit(self, provider, metric):
        return (
            AI_RATE_LIMIT_DEFAULTS
            .get(provider.name.casefold(), {})
            .get(metric)
        )

    def _sync_provider_window_locked(
        self,
        provider,
        limit,
        quota_exhausted=False,
    ):
        """Keep provider-reported reset windows without replacing fixed limits."""
        if provider.name != "Groq":
            return
        metric = limit.get("metric")
        if metric not in {"rpm", "tpm"}:
            return
        reset_at = _safe_number(limit.get("reset_at"))
        if reset_at is None:
            return

        snapshot = self._rate_state[provider.name].get(metric)
        if snapshot is None:
            configured = self._configured_limit(provider, metric)
            snapshot = _limit_snapshot(
                metric,
                limit=configured,
                remaining=configured,
                reset_at=reset_at,
                source="configured" if configured is not None else "provider",
                exact=configured is not None,
            )
            self._rate_state[provider.name][metric] = snapshot
        else:
            snapshot["reset_at"] = reset_at
        if quota_exhausted:
            snapshot["remaining"] = 0

        windows = self._usage_windows[provider.name]
        if metric == "rpm":
            windows["minute_reset_at"] = reset_at
            windows["provider_requests_reset_at"] = reset_at
        elif metric == "tpm":
            windows["tokens_minute_reset_at"] = reset_at
            windows["provider_tokens_reset_at"] = reset_at

    def _usage_for_metric_locked(self, provider, metric):
        usage = self._usage_state[provider.name]
        if metric == "rpm":
            return usage.get("requests_minute", 0)
        if metric == "rpd":
            return usage.get("requests_day", 0)
        if metric == "tpm":
            return usage.get("tokens_minute", 0)
        if metric == "tpd":
            return usage.get("tokens_day", usage.get("total_tokens", 0))
        if metric == "npd":
            return usage.get("neurons", 0)
        return 0

    def _quota_reset_at_locked(self, provider, metric, now):
        windows = self._usage_windows[provider.name]
        window_keys = {
            "rpm": "minute_reset_at",
            "tpm": "tokens_minute_reset_at",
            "rpd": "daily_reset_at",
            "tpd": "tokens_reset_at",
            "npd": "neurons_reset_at",
        }
        reset_at = windows.get(window_keys.get(metric))
        if reset_at is not None and reset_at > now:
            return reset_at

        reset_at = self._window_reset_at(provider.name, metric, now)
        if reset_at is None:
            reset_at = (
                _next_daily_reset(provider.name, now)
                if metric in {"rpd", "tpd", "npd"}
                else (int(now // 60) + 1) * 60
            )
        return reset_at

    def _local_quota_blockers_locked(self, provider):
        """Return exhausted local windows that must block network requests."""
        self._ensure_default_limits_locked(provider)
        now = time.time()
        blockers = []
        for metric, snapshot in self._rate_state[provider.name].items():
            limit = _safe_number(snapshot.get("limit"))
            remaining = _safe_number(snapshot.get("remaining"))
            used = self._usage_for_metric_locked(provider, metric)
            if metric == "unknown":
                unavailable = self._unavailable.get(provider.name)
                if (
                    unavailable
                    and unavailable.get("kind") == "quota"
                ):
                    continue
                if remaining is None or remaining > 0:
                    continue
                limit = None
            else:
                if metric not in _LIMIT_METRICS or limit is None:
                    continue
                if not (
                    (remaining is not None and remaining <= 0)
                    or used >= limit
                ):
                    continue
            reset_at = snapshot.get("reset_at")
            if reset_at is None or reset_at <= now:
                reset_at = self._quota_reset_at_locked(provider, metric, now)
            if reset_at <= now:
                continue
            blockers.append({
                "metric": metric,
                "limit": limit,
                "used": used,
                "reset_at": reset_at,
            })
        return blockers

    def _quarantine_local_quota_locked(self, provider, blockers):
        eligible_at = max(item["reset_at"] for item in blockers)
        metrics = "/".join(
            sorted(item["metric"].upper() for item in blockers)
        )
        windows = self._usage_windows[provider.name]
        for item in blockers:
            metric = item["metric"]
            reset_at = item["reset_at"]
            snapshot = self._rate_state[provider.name].get(metric)
            if snapshot is not None:
                snapshot["reset_at"] = reset_at
            if metric == "rpm":
                windows["minute_reset_at"] = reset_at
            elif metric == "tpm":
                windows["tokens_minute_reset_at"] = reset_at
            elif metric == "rpd":
                windows["daily_reset_at"] = reset_at
            elif metric == "tpd":
                windows["tokens_reset_at"] = reset_at
            elif metric == "npd":
                windows["neurons_reset_at"] = reset_at
        self._unavailable[provider.name] = {
            "kind": "quota",
            "reason": f"{metrics} local limit reached",
            "since": datetime.now(timezone.utc),
            "eligible_at": eligible_at,
        }
        self._active_index = self._next_active_index_locked()
        self._persist_state_locked()

    def _consume_rate_limit_locked(self, provider, metric, amount):
        if not amount:
            return
        snapshot = self._rate_state[provider.name].get(metric)
        if not snapshot:
            return
        remaining = snapshot.get("remaining")
        if remaining is not None:
            snapshot["remaining"] = max(0, remaining - amount)
            if (
                metric in {"rpm", "tpm"}
                and snapshot["remaining"] == 0
                and snapshot.get("reset_at") is None
            ):
                reset_at = self._quota_reset_at_locked(
                    provider,
                    metric,
                    time.time(),
                )
                snapshot["reset_at"] = reset_at
                window_key = self._short_metric_window_key(metric)
                if window_key:
                    self._usage_windows[provider.name][window_key] = reset_at

    def _record_attempt_locked(self, provider):
        self._load_persisted_state_locked()
        self._refresh_expired_locked()
        now = time.time()
        usage = self._usage_state[provider.name]
        windows = self._usage_windows[provider.name]
        if (
            windows.get("tokens_reset_at") is None
            or windows["tokens_reset_at"] <= now
        ):
            usage["tokens_day"] = 0
            usage["prompt_tokens"] = 0
            usage["completion_tokens"] = 0
            usage["total_tokens"] = 0
            windows["tokens_reset_at"] = _next_daily_reset(
                provider.name,
                now,
            )
        if (
            windows.get("daily_reset_at") is not None
            and windows["daily_reset_at"] <= now
        ):
            usage["requests_day"] = 0
            windows["daily_reset_at"] = _next_daily_reset(
                provider.name,
                now,
            )
        usage["requests_minute"] += 1
        usage["requests_day"] += 1
        if provider.name == "DeepSeek":
            usage["observed_requests_day"] += 1
        self._consume_rate_limit_locked(provider, "rpm", 1)
        self._consume_rate_limit_locked(provider, "rpd", 1)
        self._persist_state_locked()

    def _learn_unknown_limit_locked(self, provider, limits):
        """Persist a conservative estimate after a quota failure.

        The bot never replaces a configured limit. It only fills a missing
        token limit after the provider identifies the limited metric and the
        accumulated usage gives us a lower bound for that window.
        """
        configured = AI_RATE_LIMIT_DEFAULTS.get(provider.name.casefold(), {})
        for limit in limits:
            metric = limit.get("metric")
            if metric not in {"tpm", "tpd"}:
                continue
            if configured.get(metric) is not None:
                continue
            observed = self._usage_for_metric_locked(provider, metric)
            if not observed:
                continue
            if limit.get("limit") is not None:
                continue
            previous = self._rate_state[provider.name].get(metric) or {}
            previous_limit = _safe_number(previous.get("limit"))
            estimate = max(observed, previous_limit or 0)
            reset_at = limit.get("reset_at") or self._window_reset_at(
                provider.name,
                metric,
                time.time(),
            )
            self._rate_state[provider.name][metric] = _limit_snapshot(
                metric,
                limit=estimate,
                remaining=0,
                reset_at=reset_at,
                source="estimated",
                exact=False,
            )

    def _ensure_default_limits_locked(self, provider):
        defaults = AI_RATE_LIMIT_DEFAULTS.get(provider.name.casefold(), {})
        now = time.time()
        changed = False
        for metric in _LIMIT_METRICS:
            limit = defaults.get(metric)
            if not limit:
                continue
            snapshot = self._rate_state[provider.name].get(metric)
            if snapshot is None:
                source = "configured"
                self._rate_state[provider.name][metric] = _limit_snapshot(
                    metric,
                    limit=limit,
                    remaining=limit,
                    reset_at=(
                        None
                        if metric in {"rpm", "tpm"}
                        else self._window_reset_at(
                            provider.name,
                            metric,
                            now,
                        )
                    ),
                    source=source,
                    exact=True,
                )
                changed = True
                continue

            if _safe_number(snapshot.get("limit")) == limit:
                continue
            if provider.name != "Gemini" and snapshot.get("source") != "configured":
                continue

            # Configured limits are the trusted display baseline. A persisted
            # Gemini snapshot may have been created from an older provider
            # response (for example RPD=20); migrate it without changing usage.
            used = self._usage_for_metric_locked(provider, metric)
            migrated = dict(snapshot)
            migrated["metric"] = metric
            migrated["limit"] = limit
            migrated["remaining"] = max(0, limit - (used or 0))
            migrated["source"] = "configured"
            migrated["exact"] = True
            if (
                migrated.get("reset_at") is None
                and metric not in {"rpm", "tpm"}
            ):
                migrated["reset_at"] = self._window_reset_at(
                    provider.name,
                    metric,
                    now,
                )
            self._rate_state[provider.name][metric] = migrated
            changed = True
        for metric in _LIMIT_METRICS:
            snapshot = self._rate_state[provider.name].get(metric)
            if snapshot:
                self._initialize_usage_window_locked(
                    provider,
                    metric,
                    snapshot.get("reset_at"),
                )
        if changed:
            self._persist_state_locked()

    def _initialize_usage_window_locked(self, provider, metric, reset_at):
        if reset_at is None:
            return
        window_keys = {
            "rpm": "minute_reset_at",
            "tpm": "tokens_minute_reset_at",
            "rpd": "daily_reset_at",
            "tpd": "tokens_reset_at",
            "npd": "neurons_reset_at",
        }
        window_key = window_keys.get(metric)
        if not window_key:
            return
        windows = self._usage_windows[provider.name]
        if windows.get(window_key) is None:
            windows[window_key] = reset_at

    def _record_success_locked(self, provider, result):
        self._ensure_default_limits_locked(provider)
        if provider.name == "DeepSeek":
            self._refresh_deepseek_observed_usage_window_locked()
        self._rate_state[provider.name].pop("unknown", None)
        official_metrics = set()
        if isinstance(result, AIResponse):
            for limit in result.limits:
                metric = limit.get("metric", "unknown")
                self._sync_provider_window_locked(provider, limit)
                if self._configured_limit(provider, metric) is None:
                    self._rate_state[provider.name][metric] = dict(limit)
                official_metrics.add(metric)
            usage = result.usage
        else:
            usage = {}

        total_tokens = _safe_number(usage.get("total_tokens"))
        prompt_tokens = _safe_number(
            usage.get("prompt_tokens")
            or usage.get("promptTokenCount")
        ) or 0
        completion_tokens = _safe_number(
            usage.get("completion_tokens")
            or usage.get("candidatesTokenCount")
        ) or 0
        if total_tokens is None:
            total_tokens = prompt_tokens + completion_tokens
        neurons = _safe_number(usage.get("neurons")) or 0

        observed = self._usage_state[provider.name]
        observed["prompt_tokens"] += prompt_tokens
        observed["completion_tokens"] += completion_tokens
        observed["total_tokens"] += total_tokens
        observed["tokens_minute"] += total_tokens
        observed["tokens_day"] += total_tokens
        observed["neurons"] += neurons
        if provider.name == "DeepSeek":
            observed["observed_tokens_day"] += total_tokens
        windows = self._usage_windows[provider.name]
        if total_tokens and windows["tokens_reset_at"] is None:
            windows["tokens_reset_at"] = _next_daily_reset(provider.name)
        if neurons and windows["neurons_reset_at"] is None:
            windows["neurons_reset_at"] = _next_daily_reset(provider.name)

        increments = {
            "tpm": total_tokens,
            "tpd": total_tokens,
            "npd": neurons,
        }
        for metric, amount in increments.items():
            if (
                not amount
                or (
                    metric in official_metrics
                    and self._configured_limit(provider, metric) is None
                )
            ):
                continue
            snapshot = self._rate_state[provider.name].get(metric)
            if not snapshot or snapshot.get("limit") is None:
                continue
            self._consume_rate_limit_locked(provider, metric, amount)
        self._persist_state_locked()

    def _quota_exhausted_locked(self, request_kind="user"):
        configured_provider_found = False
        for provider in self._ordered_providers(request_kind):
            if (
                not self._provider_allowed_for_request(provider, request_kind)
                or not self._is_provider_enabled_locked(
                    provider.name,
                    request_kind,
                )
                or not provider.configured
            ):
                continue
            configured_provider_found = True
            unavailable = self._unavailable.get(provider.name)
            if not unavailable or unavailable.get("kind") != "quota":
                return False
        return configured_provider_found

    def _has_scheduled_recovery_locked(self, request_kind="user"):
        providers = {
            provider.name: provider
            for provider in self._providers
        }
        for name, state in self._unavailable.items():
            provider = providers.get(name)
            if (
                provider is not None
                and self._provider_allowed_for_request(provider, request_kind)
                and state.get("kind") == "quota"
                and state.get("eligible_at") is not None
                and self._is_provider_enabled_locked(name, request_kind)
            ):
                return True
        return False

    def _enter_local_mode(self):
        with self._lock:
            self._active_index = len(self._ordered_providers("user"))
            self._local_mode = True
            self._next_probe_at = self._nearest_recovery_locked()
            if self._next_probe_at is None:
                return
            if self._recovery_thread and self._recovery_thread.is_alive():
                return
            self._recovery_thread = threading.Thread(
                target=self._recovery_loop,
                name="ai-service-recovery",
                daemon=True,
            )
            self._recovery_thread.start()
        logger.warning(
            "All external AI providers are unavailable; user requests will "
            "use the local parser until Gemini recovers"
        )

    def _recovery_loop(self):
        while True:
            with self._lock:
                if not self._local_mode:
                    return
                self._next_probe_at = self._nearest_recovery_locked()
                if self._next_probe_at is None:
                    return
                delay = max(0, self._next_probe_at - time.time())
            time.sleep(delay)
            with self._lock:
                self._refresh_expired_locked()
                if not self._local_mode:
                    return

    def start_background_worker(self):
        """Start local quota-window maintenance without making provider calls."""
        with self._lock:
            if (
                self._quota_worker_thread
                and self._quota_worker_thread.is_alive()
            ):
                return False
            self._load_persisted_state_locked()
            for provider in self._providers:
                self._ensure_default_limits_locked(provider)
            self._refresh_expired_locked()
            self._quarantine_local_quota_blockers_locked()
            self._quota_worker_stop.clear()
            self._quota_worker_thread = threading.Thread(
                target=self._quota_window_loop,
                name="ai-quota-window",
                daemon=True,
            )
            self._quota_worker_thread.start()
        logger.info("AI quota window worker started")
        return True

    def stop_background_worker(self, timeout=5):
        """Stop local quota-window maintenance and wait briefly for shutdown."""
        with self._lock:
            thread = self._quota_worker_thread
            if thread is None:
                return True
            self._quota_worker_stop.set()

        if thread is not threading.current_thread():
            thread.join(timeout=max(0, float(timeout)))

        stopped = not thread.is_alive()
        with self._lock:
            if self._quota_worker_thread is thread and stopped:
                self._quota_worker_thread = None
        if stopped:
            logger.info("AI quota window worker stopped")
        else:
            logger.warning("AI quota window worker did not stop before timeout")
        return stopped

    def _quota_window_loop(self):
        while not self._quota_worker_stop.is_set():
            try:
                with self._lock:
                    self._load_persisted_state_locked()
                    for provider in self._providers:
                        self._ensure_default_limits_locked(provider)
                    self._refresh_expired_locked()
                    self._quarantine_local_quota_blockers_locked()
                    now = time.time()
                    next_reset = self._next_quota_window_locked(now)
                    delay = (
                        max(0, next_reset - now)
                        if next_reset is not None
                        else 60
                    )
                if self._quota_worker_stop.wait(delay):
                    return
            except Exception:
                logger.exception("AI quota window worker iteration failed")
                if self._quota_worker_stop.wait(60):
                    return

    def _quarantine_local_quota_blockers_locked(self):
        for provider in self._providers:
            if not self._provider_enabled_for_any_request_locked(
                provider.name
            ):
                continue
            if provider.name in self._unavailable:
                continue
            blockers = self._local_quota_blockers_locked(provider)
            if blockers and not self._allows_groq_tpd_overage_locked(
                provider,
                blockers,
            ):
                self._quarantine_local_quota_locked(provider, blockers)

    def _next_quota_window_locked(self, now=None):
        now = time.time() if now is None else now
        candidates = []
        for state in self._unavailable.values():
            eligible_at = _safe_number(state.get("eligible_at"))
            if eligible_at is not None and eligible_at > now:
                candidates.append(eligible_at)
        for provider in self._providers:
            windows = self._usage_windows[provider.name]
            for reset_at in windows.values():
                reset_at = _safe_number(reset_at)
                if reset_at is not None and reset_at > now:
                    candidates.append(reset_at)
            for snapshot in self._rate_state[provider.name].values():
                reset_at = _safe_number(snapshot.get("reset_at"))
                if reset_at is not None and reset_at > now:
                    candidates.append(reset_at)
        return min(candidates) if candidates else None

    def _nearest_recovery_locked(self, request_kind="user"):
        recoveries = [
            state.get("eligible_at")
            for name, state in self._unavailable.items()
            if (
                self._is_provider_enabled_locked(name, request_kind)
                and state.get("eligible_at") is not None
                and any(
                    provider.name == name
                    and self._provider_allowed_for_request(
                        provider,
                        request_kind,
                    )
                    for provider in self._providers
                )
            )
        ]
        return min(recoveries) if recoveries else None

    def _next_provider_reset_locked(self, provider, unavailable, now):
        if (
            unavailable
            and unavailable.get("kind") == "quota"
            and unavailable.get("eligible_at") is not None
        ):
            eligible_at = _safe_number(unavailable["eligible_at"])
            return (
                eligible_at
                if eligible_at is not None and eligible_at > now
                else None
            )
        return None

    def probe_primary_now(self):
        """Probe Gemini and restore the external chain when it succeeds."""
        with self._request_lock:
            provider_index = next(
                index
                for index, candidate in enumerate(self._providers)
                if candidate.name == "Gemini"
            )
            provider = self._providers[provider_index]
            with self._lock:
                if not self._provider_enabled_for_any_request_locked(
                    provider.name
                ):
                    return False
            try:
                with self._lock:
                    self._load_persisted_state_locked()
                    self._refresh_expired_locked()
                    quota_blockers = self._local_quota_blockers_locked(provider)
                    if quota_blockers:
                        self._quarantine_local_quota_locked(
                            provider,
                            quota_blockers,
                        )
                        logger.info(
                            "Gemini recovery probe skipped because local quota "
                            "is exhausted"
                        )
                        return False
                    self._record_attempt_locked(provider)
                result = provider.generate_json(_PROBE_PROMPT)
            except Exception as exc:
                if isinstance(
                    exc,
                    (AIProviderQuotaError, AIProviderConfigurationError),
                ):
                    self._quarantine_provider(provider_index, exc)
                logger.info("Gemini recovery probe did not succeed: %s", exc)
                return False

            with self._lock:
                self._record_success_locked(provider, result)
                self._unavailable.pop(provider.name, None)
                self._active_index = self._next_active_index_locked()
                self._local_mode = self._active_index >= len(
                    self._ordered_providers("user")
                )
                self._next_probe_at = (
                    self._nearest_recovery_locked()
                    if self._local_mode
                    else None
                )
            logger.info(
                "Gemini recovered; AI service returned to the primary provider"
            )
            return True

    def status(self, request_kind="user"):
        with self._lock:
            self._load_persisted_state_locked()
            self._refresh_expired_locked()
            for provider in self._providers:
                self._ensure_default_limits_locked(provider)
            self._quarantine_local_quota_blockers_locked()
            now = time.time()
            active_index = self._next_active_index_locked(request_kind)
            ordered_providers = self._ordered_providers(request_kind)
            request_profile = self._request_profile(request_kind)
            local_mode = (
                (self._local_mode if request_profile == "user" else False)
                or active_index >= len(ordered_providers)
            )
            active_name = self._active_provider_name_locked(
                request_profile,
                local_mode=local_mode,
            )
            active_providers = {}
            for profile in AI_PROVIDER_ORDERS:
                profile_index = self._next_active_index_locked(profile)
                profile_local_mode = (
                    profile == "user"
                    and (
                        self._local_mode
                        or profile_index >= len(self._ordered_providers(profile))
                    )
                )
                active_providers[profile] = (
                    self._active_provider_name_locked(
                        profile,
                        local_mode=profile_local_mode,
                    )
                )
            providers = []
            for provider in self._providers:
                unavailable = self._unavailable.get(provider.name)
                limit_rows = []
                for metric in _DISPLAY_LIMIT_METRICS.get(
                    provider.name,
                    (),
                ):
                    limit = self._rate_state[provider.name].get(metric)
                    if not limit:
                        limit = _limit_snapshot(
                            metric,
                            source="estimated",
                            exact=False,
                        )
                    maximum = limit.get("limit")
                    used = self._usage_for_metric_locked(provider, metric)
                    if not used and maximum is not None:
                        remaining = limit.get("remaining")
                        if remaining is not None:
                            used = max(0, maximum - remaining)
                    limit_rows.append({
                        **limit,
                        "used": used,
                        "reset_at": (
                            datetime.fromtimestamp(
                                limit["reset_at"],
                                tz=timezone.utc,
                            ).isoformat()
                            if limit.get("reset_at") is not None
                            else None
                        ),
                    })
                configured_model = getattr(provider, "model", None)
                next_reset = self._next_provider_reset_locked(
                    provider,
                    unavailable,
                    now,
                )
                providers.append({
                    "name": provider.name,
                    "priority": len(providers) + 1,
                    "model": configured_model,
                    "last_served_model": None,
                    "capabilities": dict(
                        _MODEL_CAPABILITIES.get(
                            (provider.name, configured_model),
                            {},
                        )
                    ),
                    "usage": dict(self._usage_state[provider.name]),
                    "usage_estimated": True,
                    "usage_window": dict(self._usage_windows[provider.name]),
                    "configured": provider.configured,
                    "enabled": (
                        self._provider_allowed_for_request(
                            provider,
                            request_kind,
                        )
                        and self._is_provider_enabled_locked(
                            provider.name,
                            request_kind,
                        )
                    ),
                    "enabled_by_request_kind": {
                        profile: (
                            self._provider_allowed_for_request(
                                provider,
                                profile,
                            )
                            and self._is_provider_enabled_locked(
                                provider.name,
                                profile,
                            )
                        )
                        for profile in AI_PROVIDER_ORDERS
                    },
                    "available": unavailable is None,
                    "quota_exhausted": bool(
                        unavailable
                        and unavailable.get("kind") == "quota"
                    ),
                    "reason": unavailable["reason"] if unavailable else None,
                    "eligible_at": (
                        datetime.fromtimestamp(
                            unavailable["eligible_at"],
                            tz=timezone.utc,
                        ).isoformat()
                        if unavailable and unavailable.get("eligible_at")
                        else None
                    ),
                    "next_reset_at": (
                        datetime.fromtimestamp(
                            next_reset,
                            tz=timezone.utc,
                        ).isoformat()
                        if next_reset is not None
                        else None
                    ),
                    "next_reset_seconds": (
                        max(0, next_reset - now)
                        if next_reset is not None
                        else None
                    ),
                    "limits": limit_rows,
                })
            next_recovery = self._nearest_recovery_locked(request_kind)
            next_probe_at = (
                datetime.fromtimestamp(
                    next_recovery,
                    tz=timezone.utc,
                ).isoformat()
                if next_recovery is not None
                else None
            )
            return {
                "mode": "local" if local_mode else "external",
                "active_provider": active_name,
                "active_providers": active_providers,
                "provider_order": list(_PROVIDER_ORDER),
                "provider_order_by_request_kind": {
                    profile: list(order)
                    for profile, order in AI_PROVIDER_ORDERS.items()
                },
                "providers": providers,
                "next_gemini_probe_at": next_probe_at,
                "next_recovery_at": next_probe_at,
            }

    def reset(self):
        """Reset process-local routing state, primarily for isolated tests."""
        with self._lock:
            self._active_index = 0
            self._unavailable.clear()
            self._rate_state = {
                provider.name: {}
                for provider in self._providers
            }
            self._usage_state = {
                provider.name: {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                    "neurons": 0,
                    "requests_minute": 0,
                    "requests_day": 0,
                    "tokens_minute": 0,
                    "tokens_day": 0,
                    **(
                        {
                            "observed_requests_day": 0,
                            "observed_tokens_day": 0,
                        }
                        if provider.name == "DeepSeek"
                        else {}
                    ),
                }
                for provider in self._providers
            }
            self._usage_windows = {
                provider.name: {
                    "minute_reset_at": None,
                    "tokens_minute_reset_at": None,
                    "daily_reset_at": None,
                    "tokens_reset_at": None,
                    "neurons_reset_at": None,
                    "provider_requests_reset_at": None,
                    "provider_tokens_reset_at": None,
                    **(
                        {"observed_reset_at": None}
                        if provider.name == "DeepSeek"
                        else {}
                    ),
                }
                for provider in self._providers
            }
            self._local_mode = False
            self._next_probe_at = None
            self._provider_enabled = {
                (request_kind, provider.name): None
                for request_kind in AI_PROVIDER_ORDERS
                for provider in self._providers
            }

    def _load_persisted_state_locked(self):
        if self._storage_loaded:
            return
        self._storage_loaded = True
        loader = self._usage_state_loader
        if loader is None:
            try:
                from core.db import load_ai_service_usage_state
                loader = load_ai_service_usage_state
            except Exception:
                logger.exception("Could not configure AI usage state loading")
                return
        try:
            state = loader()
        except Exception:
            logger.exception("Could not restore AI usage state")
            return
        if not isinstance(state, dict):
            return
        persisted_usage = state.get("usage")
        persisted_rates = state.get("rates")
        persisted_windows = state.get("usage_windows")
        for provider in self._providers:
            name = provider.name
            usage = (
                persisted_usage.get(name)
                if isinstance(persisted_usage, dict)
                else None
            )
            if isinstance(usage, dict):
                usage_fields = (
                    "prompt_tokens",
                    "completion_tokens",
                    "total_tokens",
                    "neurons",
                    "requests_minute",
                    "requests_day",
                    "tokens_minute",
                    "tokens_day",
                )
                if name == "DeepSeek":
                    usage_fields += (
                        "observed_requests_day",
                        "observed_tokens_day",
                    )
                for field in usage_fields:
                    value = _safe_number(usage.get(field))
                    if value is not None and value >= 0:
                        self._usage_state[name][field] = value
                if (
                    "tokens_day" not in usage
                    and self._usage_state[name]["total_tokens"]
                ):
                    self._usage_state[name]["tokens_day"] = (
                        self._usage_state[name]["total_tokens"]
                    )
            rates = (
                persisted_rates.get(name)
                if isinstance(persisted_rates, dict)
                else None
            )
            if isinstance(rates, dict):
                for metric, snapshot in rates.items():
                    if metric not in (*_LIMIT_METRICS, "unknown"):
                        continue
                    if not isinstance(snapshot, dict):
                        continue
                    restored = _limit_snapshot(
                        metric,
                        snapshot.get("limit"),
                        snapshot.get("remaining"),
                        _safe_number(snapshot.get("reset_at")),
                        str(snapshot.get("source") or "estimated"),
                        snapshot.get("exact", False),
                    )
                    self._rate_state[name][metric] = restored
            windows = (
                persisted_windows.get(name)
                if isinstance(persisted_windows, dict)
                else None
            )
            if isinstance(windows, dict):
                window_fields = (
                    "minute_reset_at",
                    "tokens_minute_reset_at",
                    "daily_reset_at",
                    "tokens_reset_at",
                    "neurons_reset_at",
                    "provider_requests_reset_at",
                    "provider_tokens_reset_at",
                )
                if name == "DeepSeek":
                    window_fields += ("observed_reset_at",)
                for field in window_fields:
                    value = _safe_number(windows.get(field))
                    if value is not None:
                        self._usage_windows[name][field] = value
        if self._migrate_legacy_groq_state_locked():
            self._persist_state_locked()

    def _migrate_legacy_groq_state_locked(self):
        """Separate legacy Groq request headers from the daily request quota."""
        provider = next(
            (
                candidate
                for candidate in self._providers
                if candidate.name == "Groq"
            ),
            None,
        )
        if provider is None:
            return False

        snapshot = self._rate_state["Groq"].get("rpd")
        if snapshot is None:
            return False

        now = time.time()
        rpm_snapshot = self._rate_state["Groq"].get("rpm")
        daily_reset_at = self._usage_windows["Groq"].get("daily_reset_at")
        expected_reset_at = _next_daily_reset("Groq", now)
        provider_request_reset_at = self._usage_windows["Groq"].get(
            "provider_requests_reset_at"
        )
        usage_day = self._usage_state["Groq"].get("requests_day", 0)
        legacy_mapping = (
            (
                provider_request_reset_at is not None
                and (
                    snapshot.get("reset_at") == provider_request_reset_at
                    or (
                        rpm_snapshot is None
                        or rpm_snapshot.get("reset_at")
                        != provider_request_reset_at
                    )
                )
            )
            or snapshot.get("source") == "provider"
            or (
                daily_reset_at is None
                and usage_day > 0
            )
            or (
                daily_reset_at is not None
                and now < daily_reset_at < expected_reset_at
            )
        )
        if not legacy_mapping:
            return False

        configured_limit = self._configured_limit(provider, "rpd")
        if configured_limit is not None:
            snapshot["limit"] = configured_limit
            snapshot["remaining"] = max(0, configured_limit - usage_day)
            snapshot["source"] = "configured"
            snapshot["exact"] = True
        snapshot["reset_at"] = expected_reset_at
        self._usage_windows["Groq"]["daily_reset_at"] = expected_reset_at
        self._usage_windows["Groq"]["provider_requests_reset_at"] = None
        return True

    def _persist_state_locked(self):
        saver = self._usage_state_saver
        if saver is None:
            try:
                from core.db import save_ai_service_usage_state
                saver = save_ai_service_usage_state
            except Exception:
                logger.exception("Could not configure AI usage state saving")
                return
        state = {
            "version": 1,
            "usage": self._usage_state,
            "rates": self._rate_state,
            "usage_windows": self._usage_windows,
        }
        try:
            saver(state)
        except Exception:
            logger.exception("Could not persist AI usage state")

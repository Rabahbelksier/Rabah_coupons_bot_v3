import os


def _optional_positive_int(name, default=None):
    raw_value = os.getenv(name)
    if raw_value in (None, ''):
        return default
    try:
        value = int(raw_value)
    except ValueError:
        return default
    return value if value > 0 else default


_ERROR_LOG_MIN_RETENTION_DAYS = 7
ERROR_LOG_RETENTION_DAYS = max(
    _optional_positive_int('ERROR_LOG_RETENTION_DAYS', 30),
    _ERROR_LOG_MIN_RETENTION_DAYS,
)
ERROR_LOG_CLEANUP_INTERVAL_SECONDS = _optional_positive_int(
    'ERROR_LOG_CLEANUP_INTERVAL_SECONDS',
    60 * 60,
)

APP_KEY = os.getenv('APP_KEY')
APP_SECRET = os.getenv('APP_SECRET')
TRACKING_ID = os.getenv('TRACKING_ID')
TOKEN = os.getenv('TELEGRAM_TOKEN')
# Prefer the explicitly configured external database when available. Replit's
# DATABASE_URL is the built-in development database in this workspace.
DATABASE_URL = os.getenv('EXTERNAL_DATABASE_URL') or os.getenv('DATABASE_URL')
PORT = int(os.getenv('PORT', 5000))
RENDER_EXTERNAL_URL = os.getenv('RENDER_EXTERNAL_URL', '')
RAILWAY_PUBLIC_DOMAIN = os.getenv('RAILWAY_PUBLIC_DOMAIN', '')
PUBLIC_URL = (
    os.getenv('PUBLIC_URL')
    or RENDER_EXTERNAL_URL
    or (
        f"https://{RAILWAY_PUBLIC_DOMAIN}"
        if RAILWAY_PUBLIC_DOMAIN
        else ''
    )
).rstrip('/')
_channel_monitor_setting = os.getenv('CHANNEL_MONITOR_ENABLED')
_running_on_replit = bool(os.getenv('REPLIT_ENVIRONMENT') or os.getenv('REPL_ID'))
CHANNEL_MONITOR_ENABLED = (
    _channel_monitor_setting.strip().lower() in {'1', 'true', 'yes', 'on'}
    if _channel_monitor_setting is not None
    else not _running_on_replit
)
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY', '')
GEMINI_MODEL = os.getenv('GEMINI_MODEL', 'gemini-3.5-flash-lite')
GROQ_API_KEY = os.getenv('GROQ_API_KEY', '')
GROQ_MODEL = os.getenv('GROQ_MODEL', 'qwen/qwen3.8-27b')
CLOUDFLARE_API_TOKEN = os.getenv('CLOUDFLARE_API_TOKEN', '')
CLOUDFLARE_ACCOUNT_ID = os.getenv('CLOUDFLARE_ACCOUNT_ID', '')
CLOUDFLARE_DEFAULT_MODEL = '@cf/qwen/qwen3.8-27b'
CLOUDFLARE_MODEL = os.getenv(
    'CLOUDFLARE_MODEL',
    CLOUDFLARE_DEFAULT_MODEL,
)
OPENROUTER_API_KEY = os.getenv('OPENROUTER_API_KEY', '')
OPENROUTER_MODEL = os.getenv(
    'OPENROUTER_MODEL',
    'openrouter/free',
)
DEEPSEEK_API_KEY = os.getenv('DEEPSEEK_API_KEY', '')
DEEPSEEK_MODEL = os.getenv('DEEPSEEK_MODEL', 'deepseek-flash')
AI_RECOVERY_INTERVAL_SECONDS = int(
    os.getenv('AI_RECOVERY_INTERVAL_SECONDS', '60')
)
AI_PROVIDER_TIMEOUT_SECONDS = _optional_positive_int(
    'AI_PROVIDER_TIMEOUT_SECONDS',
    12,
)
GEMINI_KEYWORD_CACHE_TTL_SECONDS = _optional_positive_int(
    'GEMINI_KEYWORD_CACHE_TTL_SECONDS',
    48 * 60 * 60,
)
GEMINI_KEYWORD_CACHE_SYNC_INTERVAL_SECONDS = _optional_positive_int(
    'GEMINI_KEYWORD_CACHE_SYNC_INTERVAL_SECONDS',
    30 * 60,
)
AI_USER_REQUEST_TIMEOUT_SECONDS = _optional_positive_int(
    'AI_USER_REQUEST_TIMEOUT_SECONDS',
    15,
)
CHANNEL_AI_BATCH_SIZE = 5
# Keep enough room for one maximally-sized channel post while leaving a large
# safety margin below the smallest configured provider context window.
CHANNEL_AI_MAX_PROMPT_CHARS = min(
    max(
        _optional_positive_int('CHANNEL_AI_MAX_PROMPT_CHARS', 40_000),
        20_000,
    ),
    40_000,
)
CHANNEL_AI_DRAIN_BATCHES_PER_CYCLE = min(
    max(
        _optional_positive_int('CHANNEL_AI_DRAIN_BATCHES_PER_CYCLE', 10),
        1,
    ),
    50,
)
CHANNEL_AI_DRAIN_DELAY_SECONDS = _optional_positive_int(
    'CHANNEL_AI_DRAIN_DELAY_SECONDS',
    1,
)
AI_RATE_LIMIT_DEFAULTS = {
    provider: {
        metric: _optional_positive_int(f'{provider.upper()}_{metric.upper()}')
        for metric in ('rpm', 'tpm', 'rpd', 'tpd', 'npd')
    }
    for provider in (
        'gemini',
        'groq',
        'cloudflare',
        'openrouter',
        'deepseek',
    )
}
AI_RATE_LIMIT_DEFAULTS['gemini'].update({
    'rpm': AI_RATE_LIMIT_DEFAULTS['gemini']['rpm'] or 4_000,
    'rpd': AI_RATE_LIMIT_DEFAULTS['gemini']['rpd'] or 150_000,
    'tpm': AI_RATE_LIMIT_DEFAULTS['gemini']['tpm'] or 4_000_000,
})
AI_RATE_LIMIT_DEFAULTS['groq'].update({
    'rpm': AI_RATE_LIMIT_DEFAULTS['groq']['rpm'] or 30,
    'rpd': AI_RATE_LIMIT_DEFAULTS['groq']['rpd'] or 1_000,
    'tpm': AI_RATE_LIMIT_DEFAULTS['groq']['tpm'] or 8_000,
    'tpd': AI_RATE_LIMIT_DEFAULTS['groq']['tpd'] or 200_000,
})
AI_RATE_LIMIT_DEFAULTS['cloudflare'].update({
    'npd': AI_RATE_LIMIT_DEFAULTS['cloudflare']['npd'] or 10_000,
})
# OpenRouter documents these conservative free-model request limits publicly.
AI_RATE_LIMIT_DEFAULTS['openrouter']['rpm'] = (
    AI_RATE_LIMIT_DEFAULTS['openrouter']['rpm'] or 20
)
AI_RATE_LIMIT_DEFAULTS['openrouter']['rpd'] = (
    AI_RATE_LIMIT_DEFAULTS['openrouter']['rpd'] or 50
)
TELEGRAM_API_ID = int(os.getenv('TELEGRAM_API_ID', '0') or 0)
TELEGRAM_API_HASH = os.getenv('TELEGRAM_API_HASH', '')
TELEGRAM_SESSION_STRING = os.getenv('TELEGRAM_SESSION_STRING', '')
CHANNEL_TELEGRAM_REPLACEMENT_LINK = os.getenv(
    'CHANNEL_TELEGRAM_REPLACEMENT_LINK',
    'https://t.me/rabahcopons/7366',
)
AUTO_PUBLISH_CHANNEL_LINK = os.getenv(
    'AUTO_PUBLISH_CHANNEL_LINK',
    '',
).strip()
API_URL = "https://api-sg.aliexpress.com/sync"

if not all([APP_KEY, APP_SECRET, TRACKING_ID, TOKEN]):
    raise EnvironmentError("Missing required environment variables")

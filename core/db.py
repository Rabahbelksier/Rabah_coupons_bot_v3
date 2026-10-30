import hashlib
import json
import logging
import re
import unicodedata
from difflib import SequenceMatcher

import psycopg2
from config import DATABASE_URL, ERROR_LOG_RETENTION_DAYS
from core.categories import (
    SAVED_OFFER_CATEGORY_COLUMN_FILTERS,
    category_from_aliexpress_names,
    saved_offer_filter_from_subcategory,
)
from core.ai_service import (
    AI_PROVIDER_FEATURE_KEYS,
    AI_PROVIDER_FEATURE_KEYS_BY_KIND,
)

logger = logging.getLogger(__name__)
_CHANNEL_PROCESSING_LOCK_KEY = 0x4348414E4E454C
_AI_KEYWORD_CACHE_LOCK_KEY = 0x41494B4559434143
_DATABASE_TIMEZONE = "Africa/Algiers"

_PRODUCT_REQUEST_TYPES = {
    "product_best",
    "product_cheapest",
    "product_price_range",
}
_GENERIC_PRODUCT_KEYWORDS = {
    "product",
    "products",
    "phone",
    "phones",
    "smartphone",
    "smartphones",
    "هاتف",
    "هواتف",
    "جوال",
    "جوالات",
    "موبايل",
    "موبايلات",
    "منتج",
    "منتجات",
}
_CATEGORY_ALIASES = {
    "phones": {
        "phone", "phones", "smartphone", "smartphones", "mobile", "mobiles",
        "هاتف", "هواتف", "جوال", "جوالات", "موبايل", "موبايلات",
    },
    "headphones": {
        "headphone", "headphones", "earphone", "earphones", "earbuds",
        "headset", "سماعة", "سماعات", "ايربودز", "إيربودز",
    },
    "tablets": {"tablet", "tablets", "ipad", "تابلت", "لوحي", "أجهزة لوحية"},
    "laptops": {
        "laptop", "laptops", "notebook", "macbook", "chromebook",
        "حاسوب", "لابتوب", "كمبيوتر محمول",
    },
    "watches": {
        "watch", "watches", "smartwatch", "ساعة", "ساعات", "ذكية",
    },
    "cameras": {"camera", "cameras", "كاميرا", "كاميرات", "تصوير"},
    "gaming": {
        "gaming", "game", "console", "playstation", "xbox", "نينتندو",
        "بلايستيشن", "اكس بوكس", "ألعاب",
    },
    "home": {"home", "kitchen", "منزل", "مطبخ", "منزلية"},
    "accessories": {
        "accessory", "accessories", "adapter", "charger", "cable", "case",
        "اكسسوار", "اكسسوارات", "إكسسوار", "إكسسوارات", "ملحق", "ملحقات",
        "شاحن", "شواحن", "كابل", "كوابل", "جراب", "جرابات",
    },
}
_SEARCH_TOKEN_PATTERN = re.compile(r"[\w\u0600-\u06ff]+", re.UNICODE)
_CACHE_PHRASE_SPLIT_PATTERN = re.compile(
    r"[\n;；؛,，،|｜]+|\s+[-–—－]\s+",
    re.UNICODE,
)
_CACHE_PHRASE_SHARED_TAIL_PATTERN = re.compile(
    r"^(?P<left>.+?)\s+(?:et|and|&)\s+(?P<right>.+?)\s+"
    r"(?P<tail>(?:(?:de(?:\s+(?:la|l['’]|le))?|du|des|d['’]|"
    r"the|of|for|to|with|on|in)\s+.+))$",
    re.IGNORECASE | re.UNICODE,
)
_CACHE_PHRASE_STOP_WORDS = {
    "a", "an", "and", "at", "by", "de", "des", "du", "d", "et",
    "for", "in", "la", "le", "les", "of", "on", "or", "the", "to",
    "un", "une", "with", "without", "pour", "avec", "sans", "sur",
    "dans", "و", "في", "من", "على", "مع", "عن", "هذا", "هذه",
}
_FUZZY_TOKEN_THRESHOLD = 0.72
FEATURE_DEFAULTS = {
    "ai_search": True,
    "smart_cart": True,
    "auto_publish": False,
    "ai_provider_gemini": True,
    "ai_provider_groq": True,
    "ai_provider_cloudflare": True,
    "ai_provider_openrouter": True,
    "ai_provider_deepseek": True,
    **{
        feature_key: True
        for scoped_features in AI_PROVIDER_FEATURE_KEYS_BY_KIND.values()
        for feature_key in scoped_features.values()
    },
}
_CATEGORY_BRANDS = {
    "phones": {
        "iphone", "apple",
        "samsung", "galaxy", "xiaomi", "redmi", "poco", "pixel",
        "oneplus", "oppo", "realme", "vivo", "honor", "blackview",
        "tcl", "alcatel", "nokia", "motorola", "infinix", "tecno",
    },
}
_MODEL_FAMILY_TOKENS = {
    "air", "galaxy", "gt", "iphone", "magic", "mate", "mix", "neo",
    "note", "nova", "pixel", "razr", "redmagic", "reno", "rog",
}
_MODEL_VARIANT_TOKENS = {
    "c", "fe", "lite", "max", "mini", "plus", "pro", "se", "ultra",
}
_BRAND_IDENTITY_GROUPS = {
    "apple": {"apple", "iphone"},
    "google": {"google", "pixel"},
    "motorola": {"motorola", "razr"},
    "samsung": {"samsung", "galaxy"},
    "xiaomi": {"xiaomi", "redmi", "poco"},
}
_PRESERVED_PRODUCT_FAMILIES = {"poco", "redmi"}
_MODEL_IDENTITY_STOP_TOKENS = {
    "android", "camera", "cellphone", "display", "dual", "global", "inch",
    "mobile", "network", "new", "nfc", "original", "phone", "ram", "rom",
    "screen", "sim", "smartphone", "unlocked", "version",
}
_COMMON_ACCESSORY_TERMS = {
    "adapter", "charger", "case", "cover", "cable", "cord",
    "usb", "charging", "power", "hub", "سلك", "كابل",
    "شاحن", "شواحن", "جراب", "جرابات", "غطاء", "أغطية",
}
_CATEGORY_CONFLICT_TERMS = {
    category: (set() if category == "accessories" else _COMMON_ACCESSORY_TERMS)
    | {
        alias
        for other_category, aliases in _CATEGORY_ALIASES.items()
        if other_category != category
        for alias in aliases
    }
    for category in _CATEGORY_ALIASES
}


def get_db_connection():
    conn = psycopg2.connect(DATABASE_URL)
    with conn.cursor() as cursor:
        cursor.execute("SET TIME ZONE %s", (_DATABASE_TIMEZONE,))
    return conn


def init_db():
    if not DATABASE_URL:
        logger.warning("DATABASE_URL not set, skipping database initialization")
        return
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS user_bot (
                first_name TEXT,
                last_name TEXT,
                chat_id BIGINT PRIMARY KEY,
                requests_today INTEGER NOT NULL DEFAULT 0,
                requests_this_month INTEGER NOT NULL DEFAULT 0,
                requests_last_month INTEGER NOT NULL DEFAULT 0,
                activity_day DATE,
                activity_month DATE
            )
        """)
        for column_definition in (
            "requests_today INTEGER NOT NULL DEFAULT 0",
            "requests_this_month INTEGER NOT NULL DEFAULT 0",
            "requests_last_month INTEGER NOT NULL DEFAULT 0",
            "activity_day DATE",
            "activity_month DATE",
        ):
            column_name = column_definition.split()[0]
            cur.execute(
                f"ALTER TABLE IF EXISTS user_bot ADD COLUMN IF NOT EXISTS "
                f"{column_name} {column_definition[len(column_name) + 1:]}"
            )
        cur.execute("""
            UPDATE user_bot
            SET requests_today = COALESCE(requests_today, 0),
                requests_this_month = COALESCE(requests_this_month, 0),
                requests_last_month = COALESCE(requests_last_month, 0),
                activity_day = CURRENT_DATE,
                activity_month = date_trunc('month', CURRENT_DATE)::date
            WHERE activity_day IS NULL OR activity_month IS NULL
        """)
        cur.execute("""
            UPDATE user_bot
            SET requests_last_month = COALESCE(requests_this_month, 0),
                requests_this_month = 0,
                requests_today = 0,
                activity_month = date_trunc('month', CURRENT_DATE)::date,
                activity_day = CURRENT_DATE
            WHERE activity_month < date_trunc('month', CURRENT_DATE)::date
        """)
        cur.execute("""
            UPDATE user_bot
            SET requests_today = 0,
                activity_day = CURRENT_DATE
            WHERE activity_day < CURRENT_DATE
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS bot_error_logs (
                id BIGSERIAL PRIMARY KEY,
                level TEXT NOT NULL,
                logger_name TEXT,
                message TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_bot_error_logs_created_at
            ON bot_error_logs (created_at DESC)
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS bot_feature_settings (
                feature_key TEXT PRIMARY KEY,
                enabled BOOLEAN NOT NULL DEFAULT TRUE,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ai_service_usage_state (
                state_key TEXT PRIMARY KEY,
                state JSONB NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ai_keyword_cache_keywords (
                keyword TEXT PRIMARY KEY,
                first_seen_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_seen_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMPTZ NOT NULL
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_ai_keyword_cache_keywords_expires_at
            ON ai_keyword_cache_keywords (expires_at)
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS gemini_keyword_cache_state (
                cache_key TEXT PRIMARY KEY,
                snapshot_hash TEXT NOT NULL,
                gemini_cache_name TEXT NOT NULL,
                gemini_model TEXT NOT NULL,
                synced_at TIMESTAMPTZ NOT NULL,
                expires_at TIMESTAMPTZ
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS auto_publish_deliveries (
                id BIGSERIAL PRIMARY KEY,
                product_identity TEXT NOT NULL,
                occurrence_count INTEGER NOT NULL CHECK (occurrence_count > 0),
                source_post_id BIGINT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                claimed_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                published_at TIMESTAMPTZ,
                telegram_message_id BIGINT,
                UNIQUE (product_identity, occurrence_count)
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_auto_publish_deliveries_published_at
            ON auto_publish_deliveries (published_at DESC)
        """)
        for provider_name, legacy_feature_key in AI_PROVIDER_FEATURE_KEYS.items():
            for scoped_features in AI_PROVIDER_FEATURE_KEYS_BY_KIND.values():
                scoped_feature_key = scoped_features[provider_name]
                cur.execute(
                    """
                    INSERT INTO bot_feature_settings (feature_key, enabled)
                    SELECT %s, enabled
                    FROM bot_feature_settings
                    WHERE feature_key = %s
                    ON CONFLICT (feature_key) DO NOTHING
                    """,
                    (scoped_feature_key, legacy_feature_key),
                )
        for feature_key, enabled in FEATURE_DEFAULTS.items():
            cur.execute(
                """
                INSERT INTO bot_feature_settings (feature_key, enabled)
                VALUES (%s, %s)
                ON CONFLICT (feature_key) DO NOTHING
                """,
                (feature_key, enabled),
            )
        # cart is managed by the administrator. Keep older installations
        # compatible while adding the final seller-coupon price used by AI cart.
        cur.execute("""
            ALTER TABLE IF EXISTS cart
            ADD COLUMN IF NOT EXISTS pricefinalecart NUMERIC(12, 2)
        """)
        cur.execute("""
            ALTER TABLE IF EXISTS cart
            ADD COLUMN IF NOT EXISTS ship NUMERIC(12, 2)
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS telegram_channels (
                id BIGSERIAL PRIMARY KEY,
                link TEXT NOT NULL UNIQUE
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS statu (
                id BIGSERIAL PRIMARY KEY,
                title TEXT,
                price NUMERIC(12, 2),
                content TEXT,
                processing_status TEXT NOT NULL DEFAULT 'pending',
                offer_type TEXT NOT NULL DEFAULT 'normal',
                source_link TEXT,
                published_at TIMESTAMPTZ,
                photo_file_id TEXT,
                link_img TEXT,
                source_channel_id BIGINT,
                source_message_id BIGINT,
                aliexpress_product_id TEXT,
                shipping_country TEXT,
                category TEXT,
                first_level_category_name TEXT,
                second_level_category_name TEXT,
                category_source TEXT,
                source_analize TEXT,
                 processing_started_at TIMESTAMPTZ,
                 processed_started_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (source_channel_id, source_message_id)
            )
        """)
        # Keep imported databases compatible with the new channel-post schema.
        for column_definition in (
            "title TEXT",
            "price NUMERIC(12, 2)",
            "content TEXT",
            "processing_status TEXT NOT NULL DEFAULT 'pending'",
            "offer_type TEXT NOT NULL DEFAULT 'normal'",
            "source_link TEXT",
            "published_at TIMESTAMPTZ",
            "photo_file_id TEXT",
            "link_img TEXT",
            "source_channel_id BIGINT",
            "source_message_id BIGINT",
            "aliexpress_product_id TEXT",
            "shipping_country TEXT",
            "category TEXT",
            "first_level_category_name TEXT",
            "second_level_category_name TEXT",
            "category_source TEXT",
            "source_analize TEXT",
            "created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP",
            "processing_started_at TIMESTAMPTZ",
             "processed_started_at TIMESTAMPTZ",
        ):
            column_name = column_definition.split()[0]
            cur.execute(
                f"ALTER TABLE IF EXISTS statu ADD COLUMN IF NOT EXISTS "
                f"{column_name} {column_definition[len(column_name) + 1:]}"
            )
        cur.execute("""
            UPDATE statu
            SET processing_status = 'pending'
            WHERE processing_status = 'failed'
        """)
        cur.execute("""
            DELETE FROM statu
            WHERE processing_status IS NULL
               OR processing_status NOT IN ('pending', 'processing', 'processed')
        """)
        cur.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'statu_processing_status_check'
                      AND conrelid = 'statu'::regclass
                ) THEN
                    ALTER TABLE statu ADD CONSTRAINT statu_processing_status_check
                    CHECK (processing_status IN ('pending', 'processing', 'processed'));
                END IF;
            END $$;
        """)
        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_statu_source_message
            ON statu (source_channel_id, source_message_id)
        """)
        # The complete phrase cache is rebuilt off the Telegram readiness
        # path after startup. Never expose entries from the previous format
        # while that rebuild is running.
        cur.execute("DELETE FROM ai_keyword_cache_keywords")
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_statu_search_price
            ON statu (processing_status, price)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_statu_search_offer_type
            ON statu (processing_status, offer_type)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_statu_search_category
            ON statu (processing_status, category)
        """)
        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_telegram_channels_link
            ON telegram_channels (link)
        """)
        cur.execute(
            """
            SELECT id, title, content
            FROM statu
            WHERE processing_status = 'processed'
              AND category IS NULL
              AND created_at >= CURRENT_TIMESTAMP - INTERVAL '2 days'
            """
        )
        category_updates = [
            (category, post_id)
            for post_id, title, content in cur.fetchall()
            if (category := _classify_legacy_post_category(title, content))
        ]
        if category_updates:
            cur.executemany(
                "UPDATE statu SET category = %s WHERE id = %s",
                category_updates,
            )
        conn.commit()
        cur.close()
        conn.close()
        logger.info("Database initialized successfully")
    except Exception as e:
        if conn:
            conn.rollback()
            conn.close()
        logger.exception("Database initialization failed")
        raise


def is_feature_enabled(feature_key):
    """Return a feature flag, keeping features enabled if DB is unavailable."""
    default = FEATURE_DEFAULTS.get(feature_key, True)
    if not DATABASE_URL:
        return default

    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT enabled
                FROM bot_feature_settings
                WHERE feature_key = %s
                """,
                (feature_key,),
            )
            row = cursor.fetchone()
            return bool(row[0]) if row is not None else default
    except psycopg2.errors.UndefinedTable:
        if conn:
            conn.rollback()
        logger.warning(
            "Feature settings table is not initialized; using default for %s",
            feature_key,
        )
        return default
    except Exception:
        logger.exception("Could not read feature flag %s", feature_key)
        return default
    finally:
        if conn:
            conn.close()


def delete_channel_post_after_analysis(post_id):
    """Delete a processing row that AI explicitly classified as unusable."""
    if not DATABASE_URL:
        return False
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute(
                """
                DELETE FROM statu
                WHERE id = %s AND processing_status = 'processing'
                """,
                (post_id,),
            )
            deleted = cursor.rowcount > 0
        conn.commit()
        return deleted
    finally:
        if conn:
            conn.close()


def set_feature_enabled(feature_key, enabled):
    """Persist a feature flag and return its new value."""
    if feature_key not in FEATURE_DEFAULTS:
        raise ValueError(f"Unknown feature: {feature_key}")
    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL is required to persist feature settings"
        )

    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO bot_feature_settings (feature_key, enabled)
                VALUES (%s, %s)
                ON CONFLICT (feature_key) DO UPDATE SET
                    enabled = EXCLUDED.enabled,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (feature_key, bool(enabled)),
            )
        conn.commit()
        return bool(enabled)
    except Exception:
        if conn:
            conn.rollback()
        logger.exception("Could not update feature flag %s", feature_key)
        raise
    finally:
        if conn:
            conn.close()

def load_ai_service_usage_state():
    """Load the persisted AI usage snapshot, or return no state safely."""
    if not DATABASE_URL:
        return None
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT state
                FROM ai_service_usage_state
                WHERE state_key = 'global'
                """
            )
            row = cursor.fetchone()
            if not row:
                return None
            state = row[0]
            if isinstance(state, str):
                state = json.loads(state)
            return state if isinstance(state, dict) else None
    except psycopg2.errors.UndefinedTable:
        if conn:
            conn.rollback()
        logger.info("AI usage state table is not initialized yet")
        return None
    except Exception:
        logger.exception("Could not load persisted AI usage state")
        return None
    finally:
        if conn:
            conn.close()


def _keyword_cache_phrases(title):
    """Extract compact, title-grounded product phrases for Gemini matching."""
    source_title = str(title or "")
    phrases = []
    seen = set()

    for segment in _CACHE_PHRASE_SPLIT_PATTERN.split(source_title):
        segment = segment.strip()
        if not segment:
            continue

        shared_tail = _CACHE_PHRASE_SHARED_TAIL_PATTERN.match(segment)
        if shared_tail:
            candidates = [
                f"{shared_tail.group('left')} {shared_tail.group('tail')}",
                f"{shared_tail.group('right')} {shared_tail.group('tail')}",
            ]
        else:
            candidates = [segment]

        for candidate in candidates:
            tokens = _SEARCH_TOKEN_PATTERN.findall(candidate)
            if len(tokens) < 2:
                continue

            normalized_tokens = [
                unicodedata.normalize("NFKC", token)
                for token in tokens
            ]
            meaningful_tokens = [
                token
                for token, normalized_token in zip(tokens, normalized_tokens)
                if normalized_token.casefold() not in _CACHE_PHRASE_STOP_WORDS
                and normalized_token.casefold()
                not in _GENERIC_PRODUCT_KEYWORDS
            ]
            contains_model_number = any(
                re.search(r"\d", token) for token in normalized_tokens
            )
            if len(meaningful_tokens) < 2 and not (
                meaningful_tokens and contains_model_number
            ):
                continue

            phrase = candidate.strip()
            phrase_key = " ".join(
                token.casefold() for token in normalized_tokens
            )
            if phrase_key not in seen:
                seen.add(phrase_key)
                phrases.append(phrase)

    return phrases


def _keyword_cache_terms(title):
    """Compatibility alias for callers using the former token-cache helper."""
    return _keyword_cache_phrases(title)


def refresh_ai_keyword_cache_from_title(title):
    """Upsert title phrases and renew each phrase for 48 hours."""
    phrases = _keyword_cache_phrases(title)
    if not phrases or not DATABASE_URL:
        return len(phrases)

    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                (_AI_KEYWORD_CACHE_LOCK_KEY,),
            )
            cursor.executemany(
                """
                INSERT INTO ai_keyword_cache_keywords (
                    keyword,
                    last_seen_at,
                    expires_at
                )
                VALUES (%s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP + INTERVAL '48 hours')
                ON CONFLICT (keyword) DO UPDATE SET
                    last_seen_at = CURRENT_TIMESTAMP,
                    expires_at = CURRENT_TIMESTAMP + INTERVAL '48 hours'
                """,
                [(phrase,) for phrase in phrases],
            )
        conn.commit()
        return len(phrases)
    except Exception:
        if conn:
            conn.rollback()
        logger.exception("Could not refresh AI keyword cache")
        raise
    finally:
        if conn:
            conn.close()


def rebuild_ai_keyword_cache_from_processed_titles():
    """Atomically replace cached phrases using every currently processed title."""
    if not DATABASE_URL:
        return 0

    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                (_AI_KEYWORD_CACHE_LOCK_KEY,),
            )
            # Live title refreshes use the same transaction lock and upsert
            # after commit, preventing a just-processed offer from being lost.
            cursor.execute(
                """
                SELECT title
                FROM statu
                WHERE processing_status = 'processed'
                  AND title IS NOT NULL
                """
            )
            phrases = {
                phrase
                for (title,) in cursor.fetchall()
                for phrase in _keyword_cache_phrases(title)
            }

            cursor.execute("DELETE FROM ai_keyword_cache_keywords")
            if phrases:
                cursor.executemany(
                    """
                    INSERT INTO ai_keyword_cache_keywords (
                        keyword,
                        first_seen_at,
                        last_seen_at,
                        expires_at
                    )
                    VALUES (
                        %s,
                        CURRENT_TIMESTAMP,
                        CURRENT_TIMESTAMP,
                        CURRENT_TIMESTAMP + INTERVAL '48 hours'
                    )
                    """,
                    [(phrase,) for phrase in sorted(phrases)],
                )
        conn.commit()
        return len(phrases)
    except Exception:
        if conn:
            conn.rollback()
        logger.exception("Could not rebuild AI phrase cache")
        raise
    finally:
        if conn:
            conn.close()


def get_gemini_keyword_cache_snapshot():
    """Return active title phrases, their hash, and the saved Gemini state."""
    empty = {
        "keywords": [],
        "snapshot_hash": "",
        "state": None,
    }
    if not DATABASE_URL:
        return empty

    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute(
                """
                DELETE FROM ai_keyword_cache_keywords
                WHERE expires_at <= CURRENT_TIMESTAMP
                """
            )
            cursor.execute(
                """
                SELECT keyword
                FROM ai_keyword_cache_keywords
                WHERE expires_at > CURRENT_TIMESTAMP
                ORDER BY keyword
                """
            )
            keywords = [row[0] for row in cursor.fetchall()]
            cursor.execute(
                """
                SELECT snapshot_hash, gemini_cache_name, gemini_model,
                       synced_at, expires_at
                FROM gemini_keyword_cache_state
                WHERE cache_key = 'global'
                """
            )
            row = cursor.fetchone()
        conn.commit()
        state = None
        if row:
            state = {
                "snapshot_hash": row[0],
                "gemini_cache_name": row[1],
                "gemini_model": row[2],
                "synced_at": row[3],
                "expires_at": row[4],
            }
        snapshot_hash = (
            hashlib.sha256("\n".join(keywords).encode("utf-8")).hexdigest()
            if keywords
            else ""
        )
        return {
            "keywords": keywords,
            "snapshot_hash": snapshot_hash,
            "state": state,
        }
    except psycopg2.errors.UndefinedTable:
        if conn:
            conn.rollback()
        logger.info("AI keyword cache tables are not initialized yet")
        return empty
    except Exception:
        if conn:
            conn.rollback()
        logger.exception("Could not load Gemini keyword cache snapshot")
        return empty
    finally:
        if conn:
            conn.close()


def save_gemini_keyword_cache_state(
    snapshot_hash,
    gemini_cache_name,
    gemini_model,
    expires_at=None,
):
    """Persist the successfully created Gemini cache as the current snapshot."""
    if not DATABASE_URL:
        return False
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO gemini_keyword_cache_state (
                    cache_key,
                    snapshot_hash,
                    gemini_cache_name,
                    gemini_model,
                    synced_at,
                    expires_at
                )
                VALUES (
                    'global', %s, %s, %s, CURRENT_TIMESTAMP, %s
                )
                ON CONFLICT (cache_key) DO UPDATE SET
                    snapshot_hash = EXCLUDED.snapshot_hash,
                    gemini_cache_name = EXCLUDED.gemini_cache_name,
                    gemini_model = EXCLUDED.gemini_model,
                    synced_at = EXCLUDED.synced_at,
                    expires_at = EXCLUDED.expires_at
                """,
                (
                    snapshot_hash,
                    gemini_cache_name,
                    gemini_model,
                    expires_at,
                ),
            )
        conn.commit()
        return True
    except Exception:
        if conn:
            conn.rollback()
        logger.exception("Could not save Gemini keyword cache state")
        return False
    finally:
        if conn:
            conn.close()


def get_gemini_official_category_cache_state():
    """Return the persisted Gemini cache state for official categories."""
    if not DATABASE_URL:
        return None
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT snapshot_hash, gemini_cache_name, gemini_model,
                       synced_at, expires_at
                FROM gemini_keyword_cache_state
                WHERE cache_key = 'official_categories'
                """
            )
            row = cursor.fetchone()
        if not row:
            return None
        return {
            "snapshot_hash": row[0],
            "gemini_cache_name": row[1],
            "gemini_model": row[2],
            "synced_at": row[3],
            "expires_at": row[4],
        }
    except psycopg2.errors.UndefinedTable:
        if conn:
            conn.rollback()
        logger.info("Gemini cache state table is not initialized yet")
        return None
    except Exception:
        if conn:
            conn.rollback()
        logger.exception("Could not load Gemini official category cache state")
        return None
    finally:
        if conn:
            conn.close()


def save_gemini_official_category_cache_state(
    snapshot_hash,
    gemini_cache_name,
    gemini_model,
    expires_at=None,
):
    """Persist the successfully created Gemini official category cache."""
    if not DATABASE_URL:
        return False
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO gemini_keyword_cache_state (
                    cache_key,
                    snapshot_hash,
                    gemini_cache_name,
                    gemini_model,
                    synced_at,
                    expires_at
                )
                VALUES (
                    'official_categories', %s, %s, %s, CURRENT_TIMESTAMP, %s
                )
                ON CONFLICT (cache_key) DO UPDATE SET
                    snapshot_hash = EXCLUDED.snapshot_hash,
                    gemini_cache_name = EXCLUDED.gemini_cache_name,
                    gemini_model = EXCLUDED.gemini_model,
                    synced_at = EXCLUDED.synced_at,
                    expires_at = EXCLUDED.expires_at
                """,
                (
                    snapshot_hash,
                    gemini_cache_name,
                    gemini_model,
                    expires_at,
                ),
            )
        conn.commit()
        return True
    except Exception:
        if conn:
            conn.rollback()
        logger.exception("Could not save Gemini official category cache state")
        return False
    finally:
        if conn:
            conn.close()


def get_telegram_channel_links():
    """Return configured channel links in database order."""
    if not DATABASE_URL:
        return []
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute("SELECT id, link FROM telegram_channels ORDER BY id")
            return [
                {"id": row[0], "link": row[1]}
                for row in cursor.fetchall()
            ]
    finally:
        if conn:
            conn.close()


def get_pending_channel_posts(limit=None):
    """Return pending source records in queue order."""
    if not DATABASE_URL:
        return []
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            params = []
            limit_clause = ""
            if limit is not None:
                params.append(max(1, int(limit)))
                limit_clause = "LIMIT %s"
            cursor.execute(
                f"""
                SELECT id, source_channel_id, source_message_id, created_at
                       , processing_status
                FROM statu
                WHERE source_channel_id IS NOT NULL
                  AND source_message_id IS NOT NULL
                  AND processing_status = 'pending'
                ORDER BY id
                {limit_clause}
                """,
                params,
            )
            return [
                {
                    "id": row[0],
                    "source_channel_id": row[1],
                    "source_message_id": row[2],
                    "created_at": row[3],
                    "processing_status": row[4],
                }
                for row in cursor.fetchall()
            ]
    finally:
        if conn:
            conn.close()


def get_channel_queue_status():
    """Return counts and oldest ages for the three channel queue states."""
    empty = {
        "pending": 0,
        "processing": 0,
        "processed": 0,
        "oldest_pending_seconds": None,
        "oldest_processing_seconds": None,
    }
    if not DATABASE_URL:
        return empty

    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    COUNT(*) FILTER (WHERE processing_status = 'pending'),
                    COUNT(*) FILTER (WHERE processing_status = 'processing'),
                    COUNT(*) FILTER (WHERE processing_status = 'processed'),
                    EXTRACT(EPOCH FROM (
                        CURRENT_TIMESTAMP
                        - MIN(created_at) FILTER (
                            WHERE processing_status = 'pending'
                        )
                    )),
                    EXTRACT(EPOCH FROM (
                        CURRENT_TIMESTAMP
                        - MIN(COALESCE(processing_started_at, created_at)) FILTER (
                            WHERE processing_status = 'processing'
                        )
                    ))
                FROM statu
                WHERE source_channel_id IS NOT NULL
                  AND source_message_id IS NOT NULL
                """
            )
            row = cursor.fetchone()
            return {
                "pending": int(row[0] or 0),
                "processing": int(row[1] or 0),
                "processed": int(row[2] or 0),
                "oldest_pending_seconds": (
                    max(0, float(row[3])) if row[3] is not None else None
                ),
                "oldest_processing_seconds": (
                    max(0, float(row[4])) if row[4] is not None else None
                ),
            }
    finally:
        if conn:
            conn.close()


def get_processing_channel_posts(limit=5):
    """Return rows already claimed for one resumable AI batch."""
    if not DATABASE_URL:
        return []
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute(
                """SELECT id, source_channel_id, source_message_id, content,
                          source_link, photo_file_id, link_img, processing_status,
                          COALESCE(processing_started_at, created_at)
                              AS processing_started_at
                   FROM statu
                   WHERE processing_status = 'processing'
                   ORDER BY id LIMIT %s""",
                (max(1, int(limit)),),
            )
            columns = [d[0] for d in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]
    finally:
        if conn:
            conn.close()


def acquire_channel_processing_lock():
    """Acquire the cross-process lock that owns processing rows during a batch."""
    if not DATABASE_URL:
        return None
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT pg_try_advisory_lock(%s)",
                (_CHANNEL_PROCESSING_LOCK_KEY,),
            )
            if cursor.fetchone()[0]:
                return conn
    except Exception:
        conn.close()
        raise
    conn.close()
    return None


def release_channel_processing_lock(conn):
    """Release and close a processing lock connection."""
    if not conn:
        return
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_unlock(%s)",
                (_CHANNEL_PROCESSING_LOCK_KEY,),
            )
    finally:
        conn.close()


def create_channel_post(
    source_channel_id,
    source_message_id,
    source_link,
    published_at,
    photo_file_id=None,
):
    """Insert a new channel source in the pending queue."""
    if not DATABASE_URL:
        return None
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO statu (
                    source_channel_id,
                    source_message_id,
                    source_link,
                    published_at,
                    photo_file_id,
                    processing_status,
                    offer_type
                )
                VALUES (%s, %s, %s, %s, %s, 'pending', 'normal')
                ON CONFLICT (source_channel_id, source_message_id)
                DO NOTHING
                RETURNING id
                """,
                (
                    source_channel_id,
                    source_message_id,
                    source_link,
                    published_at,
                    photo_file_id,
                ),
            )
            row = cursor.fetchone()
        conn.commit()
        return row[0] if row else None
    finally:
        if conn:
            conn.close()


def update_channel_post(post_id, expected_status=None, **fields):
    """Update an allow-listed set of processed channel post fields."""
    allowed_fields = {
        "title",
        "price",
        "content",
        "processing_status",
        "offer_type",
        "source_link",
        "published_at",
        "photo_file_id",
        "link_img",
        "aliexpress_product_id",
        "shipping_country",
        "category",
        "first_level_category_name",
        "second_level_category_name",
        "category_source",
        "source_analize",
        "processing_started_at",
        "processed_started_at",
    }
    updates = [(name, value) for name, value in fields.items() if name in allowed_fields]
    if not updates or not DATABASE_URL:
        return

    assignments = ", ".join(
        f"{name} = CURRENT_TIMESTAMP"
        if (
            name in {"processing_started_at", "processed_started_at"}
            and value == "CURRENT_TIMESTAMP"
        )
        else f"{name} = %s"
        for name, value in updates
    )
    values = [
        value
        for name, value in updates
        if not (
            name in {"processing_started_at", "processed_started_at"}
            and value == "CURRENT_TIMESTAMP"
        )
    ]
    values.append(post_id)
    status_condition = ""
    if expected_status is not None:
        status_condition = " AND processing_status = %s"
        values.append(expected_status)
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            try:
                cursor.execute(
                    f"UPDATE statu SET {assignments} "
                    f"WHERE id = %s{status_condition}",
                    values,
                )
            except psycopg2.errors.UndefinedColumn:
                if not any(
                    name in {
                        "link_img",
                        "category_source",
                        "source_analize",
                        "processed_started_at",
                    }
                    for name, _ in updates
                ):
                    raise
                # Keep older external databases usable if initialization could
                # not add the new provenance column. The other category fields
                # are still saved instead of failing the whole channel post.
                conn.rollback()
                compatible_updates = [
                    (name, value)
                    for name, value in updates
                    if name not in {
                        "link_img",
                        "source_analize",
                        "processed_started_at",
                    }
                ]
                try:
                    if not compatible_updates:
                        return
                    compatible_assignments = ", ".join(
                        f"{name} = %s" for name, _ in compatible_updates
                    )
                    compatible_values = [value for _, value in compatible_updates]
                    compatible_values.append(post_id)
                    if expected_status is not None:
                        compatible_values.append(expected_status)
                    cursor.execute(
                        f"UPDATE statu SET {compatible_assignments} "
                        f"WHERE id = %s{status_condition}",
                        compatible_values,
                    )
                except psycopg2.errors.UndefinedColumn:
                    conn.rollback()
                    compatible_updates = [
                        (name, value)
                        for name, value in updates
                        if name not in {
                            "link_img",
                            "category_source",
                            "source_analize",
                            "processed_started_at",
                        }
                    ]
                    if not compatible_updates:
                        return
                    compatible_assignments = ", ".join(
                        f"{name} = %s" for name, _ in compatible_updates
                    )
                    compatible_values = [value for _, value in compatible_updates]
                    compatible_values.append(post_id)
                    if expected_status is not None:
                        compatible_values.append(expected_status)
                    cursor.execute(
                        f"UPDATE statu SET {compatible_assignments} "
                        f"WHERE id = %s{status_condition}",
                        compatible_values,
                    )
            updated = cursor.rowcount > 0
        conn.commit()
        return updated
    finally:
        if conn:
            conn.close()


def mark_channel_post_pending(post_id):
    """Return a claimed row to the only retryable state."""
    if not DATABASE_URL:
        return False
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute(
                """UPDATE statu
                   SET processing_status = 'pending',
                       processing_started_at = NULL,
                       processed_started_at = NULL
                   WHERE id = %s AND processing_status = 'processing'""",
                (post_id,),
            )
            updated = cursor.rowcount > 0
        conn.commit()
        return updated
    finally:
        if conn:
            conn.close()


def delete_channel_post_after_analysis_failure(post_id):
    """Delete only a pending row explicitly proven invalid by preparation."""
    if not DATABASE_URL:
        return False

    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute(
                """
                DELETE FROM statu
                WHERE id = %s
                  AND processing_status IN (
                      'pending'
                  )
                """,
                (post_id,),
            )
            deleted = cursor.rowcount > 0
        conn.commit()
        return deleted
    finally:
        if conn:
            conn.close()




def delete_channel_post_if_status(post_id, expected_status):
    """Delete a channel row only while it is in the expected state."""
    if not DATABASE_URL:
        return False

    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute(
                """
                DELETE FROM statu
                WHERE id = %s
                  AND processing_status = %s
                """,
                (post_id, expected_status),
            )
            deleted = cursor.rowcount > 0
        conn.commit()
        return deleted
    except Exception:
        if conn:
            conn.rollback()
        raise
    finally:
        if conn:
            conn.close()


def requeue_processing_channel_posts(post_ids=None):
    """Return selected processing rows to the retryable pending state."""
    if not DATABASE_URL:
        return 0

    normalized_ids = [
        int(post_id)
        for post_id in (post_ids or [])
        if post_id is not None
    ]
    if post_ids is not None and not normalized_ids:
        return 0

    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            query = """
                UPDATE statu
                SET processing_status = 'pending',
                    processing_started_at = NULL,
                    processed_started_at = NULL
                WHERE processing_status = 'processing'
            """
            params = []
            if post_ids is not None:
                placeholders = ", ".join(["%s"] * len(normalized_ids))
                query += f" AND id IN ({placeholders})"
                params.extend(normalized_ids)
            cursor.execute(query, params)
            updated = cursor.rowcount
        conn.commit()
        return updated
    except Exception:
        if conn:
            conn.rollback()
        raise
    finally:
        if conn:
            conn.close()


def get_channel_post(post_id):
    if not DATABASE_URL:
        return None
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            try:
                cursor.execute(
                    """
                    SELECT id, title, price, content, processing_status,
                           source_link, published_at, photo_file_id, link_img,
                           source_channel_id, source_message_id,
                            aliexpress_product_id, shipping_country, offer_type,
                            category,
                           first_level_category_name, second_level_category_name,
                           category_source, source_analize
                    FROM statu
                    WHERE id = %s AND processing_status = 'processed'
                    """,
                    (post_id,),
                )
            except psycopg2.errors.UndefinedColumn:
                conn.rollback()
                cursor.execute(
                    """
                    SELECT id, title, price, content, processing_status,
                           source_link, published_at, photo_file_id, link_img,
                           source_channel_id, source_message_id,
                           aliexpress_product_id, shipping_country, category,
                           first_level_category_name, second_level_category_name
                    FROM statu
                    WHERE id = %s AND processing_status = 'processed'
                    """,
                    (post_id,),
                )
            row = cursor.fetchone()
            if not row:
                return None
            columns = [description[0] for description in cursor.description]
            return dict(zip(columns, row))
    finally:
        if conn:
            conn.close()


def count_processed_channel_posts():
    """Return the number of searchable, processed offers."""
    if not DATABASE_URL:
        return 0

    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*)
                FROM statu
                WHERE processing_status = 'processed'
                """
            )
            row = cursor.fetchone()
            return int(row[0] or 0) if row else 0
    finally:
        if conn:
            conn.close()


def get_processed_channel_posts_for_auto_publish():
    """Return processed offers needed for product occurrence grouping."""
    if not DATABASE_URL:
        return []

    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, title, price, content, processing_status,
                       source_link, published_at, photo_file_id, link_img,
                       source_channel_id, source_message_id,
                       aliexpress_product_id, shipping_country, offer_type,
                       category, first_level_category_name,
                       second_level_category_name, category_source,
                       source_analize
                FROM statu
                WHERE processing_status = 'processed'
                ORDER BY id
                """
            )
            columns = [description[0] for description in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]
    finally:
        if conn:
            conn.close()


def claim_auto_publish_delivery(product_key, occurrence_count, source_post_id):
    """Atomically claim one threshold, allowing stale claims to be retried."""
    if not DATABASE_URL:
        return None

    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO auto_publish_deliveries (
                    product_identity, occurrence_count, source_post_id, status
                )
                VALUES (%s, %s, %s, 'claimed')
                ON CONFLICT (product_identity, occurrence_count)
                DO UPDATE SET
                    source_post_id = EXCLUDED.source_post_id,
                    status = 'claimed',
                    claimed_at = CURRENT_TIMESTAMP
                WHERE auto_publish_deliveries.status IN ('pending', 'failed')
                   OR (
                       auto_publish_deliveries.status = 'claimed'
                       AND auto_publish_deliveries.claimed_at
                           < CURRENT_TIMESTAMP - INTERVAL '15 minutes'
                   )
                RETURNING id
                """,
                (product_key, int(occurrence_count), int(source_post_id)),
            )
            row = cursor.fetchone()
        conn.commit()
        return row[0] if row else None
    except Exception:
        if conn:
            conn.rollback()
        raise
    finally:
        if conn:
            conn.close()


def mark_auto_publish_delivery_published(delivery_id, telegram_message_id):
    if not DATABASE_URL:
        return False

    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE auto_publish_deliveries
                SET status = 'published',
                    published_at = CURRENT_TIMESTAMP,
                    telegram_message_id = %s
                WHERE id = %s AND status = 'claimed'
                """,
                (telegram_message_id, int(delivery_id)),
            )
            updated = cursor.rowcount > 0
        conn.commit()
        return updated
    except Exception:
        if conn:
            conn.rollback()
        raise
    finally:
        if conn:
            conn.close()


def mark_auto_publish_delivery_failed(delivery_id):
    if not DATABASE_URL:
        return False

    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE auto_publish_deliveries
                SET status = 'failed'
                WHERE id = %s AND status = 'claimed'
                """,
                (int(delivery_id),),
            )
            updated = cursor.rowcount > 0
        conn.commit()
        return updated
    except Exception:
        if conn:
            conn.rollback()
        raise
    finally:
        if conn:
            conn.close()


def get_auto_publish_report():
    """Return automatic delivery totals for recent periods in the database timezone."""
    empty = {
        "published_today": 0,
        "published_yesterday": 0,
        "published_this_month": 0,
        "published_last_month": 0,
        "published_total": 0,
        "failed_today": 0,
        "claimed": 0,
        "thresholds_today": 0,
    }
    if not DATABASE_URL:
        return empty

    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    COUNT(*) FILTER (
                        WHERE status = 'published'
                          AND published_at::date = CURRENT_DATE
                    ),
                    COUNT(*) FILTER (
                        WHERE status = 'published'
                          AND published_at::date = CURRENT_DATE - 1
                    ),
                    COUNT(*) FILTER (
                        WHERE status = 'published'
                          AND published_at >= date_trunc('month', CURRENT_DATE)
                          AND published_at < date_trunc('month', CURRENT_DATE)
                              + INTERVAL '1 month'
                    ),
                    COUNT(*) FILTER (
                        WHERE status = 'published'
                          AND published_at >= date_trunc('month', CURRENT_DATE)
                              - INTERVAL '1 month'
                          AND published_at < date_trunc('month', CURRENT_DATE)
                    ),
                    COUNT(*) FILTER (WHERE status = 'published'),
                    COUNT(*) FILTER (
                        WHERE status = 'failed'
                          AND claimed_at::date = CURRENT_DATE
                    ),
                    COUNT(*) FILTER (WHERE status = 'claimed'),
                    COUNT(*) FILTER (
                        WHERE status = 'published'
                          AND published_at::date = CURRENT_DATE
                    )
                FROM auto_publish_deliveries
                """
            )
            row = cursor.fetchone()
            if not row:
                return empty
            return {
                "published_today": int(row[0] or 0),
                "published_yesterday": int(row[1] or 0),
                "published_this_month": int(row[2] or 0),
                "published_last_month": int(row[3] or 0),
                "published_total": int(row[4] or 0),
                "failed_today": int(row[5] or 0),
                "claimed": int(row[6] or 0),
                "thresholds_today": int(row[7] or 0),
            }
    finally:
        if conn:
            conn.close()


def _normalize_result_limit(limit):
    if limit is None:
        return None
    return max(1, int(limit))


def search_channel_posts(intent, limit=None):
    """Search processed posts with category-aware and typo-tolerant ranking."""
    if not DATABASE_URL:
        return []

    result_limit = _normalize_result_limit(limit)
    request_type = intent.get("request_type")
    keywords = [
        str(keyword).strip()
        for keyword in intent.get("keywords", [])
        if str(keyword).strip()
    ][:6]
    if request_type in _PRODUCT_REQUEST_TYPES:
        specific_keywords = [
            keyword
            for keyword in keywords
            if keyword.casefold() not in _GENERIC_PRODUCT_KEYWORDS
        ]
        keywords = specific_keywords or keywords

    conditions = ["processing_status = 'processed'"]
    params = []

    min_price = intent.get("min_price")
    max_price = intent.get("max_price")
    price_mode = str(intent.get("price_mode") or "").casefold()
    if min_price is not None and price_mode != "near":
        conditions.append("price >= %s")
        params.append(min_price)
    if max_price is not None and price_mode != "near":
        conditions.append("price <= %s")
        params.append(max_price)
    offer_type = str(intent.get("offer_type") or "").casefold().strip()
    if offer_type in {"bundle", "normal"}:
        conditions.append(
            "COALESCE(NULLIF(offer_type, ''), 'normal') = %s"
        )
        params.append(offer_type)

    trending_has_content_filters = (
        request_type == "trending"
        and bool(
            intent.get("category")
            or intent.get("required_specs")
            or intent.get("shipping_country")
            or intent.get("min_price") is not None
            or intent.get("max_price") is not None
            or intent.get("target_price") is not None
            or offer_type
            or intent.get("saved_category")
            or intent.get("saved_subcategory")
            or intent.get("official_category")
            or intent.get("official_categories")
            or _infer_category(None, keywords)
            or any(
                keyword.casefold() not in _GENERIC_PRODUCT_KEYWORDS
                for keyword in keywords
            )
        )
    )

    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            if request_type == "trending" and not trending_has_content_filters:
                # Return one representative offer per product. The product
                # with the most occurrences comes first, and its cheapest
                # processed offer is the representative shown to the user.
                product_key = (
                    "COALESCE("
                    "NULLIF(aliexpress_product_id, ''), "
                    "NULLIF(LOWER(TRIM(title)), ''), "
                    "'post:' || id::text"
                    ")"
                )
                limit_clause = ""
                query_params = list(params)
                if result_limit is not None:
                    limit_clause = "LIMIT %s"
                    query_params.append(result_limit)
                cursor.execute(
                    f"""
                    WITH ranked_posts AS (
                        SELECT id, title, price, content, processing_status,
                               source_link, published_at, photo_file_id, link_img,
                               source_channel_id, source_message_id,
                                 aliexpress_product_id,
                                 shipping_country, offer_type, category,
                                 first_level_category_name,
                                   second_level_category_name,
                               COUNT(*) OVER (
                                   PARTITION BY {product_key}
                               ) AS product_occurrences,
                               ROW_NUMBER() OVER (
                                   PARTITION BY {product_key}
                                   ORDER BY price ASC NULLS LAST,
                                            published_at DESC NULLS LAST,
                                            id DESC
                               ) AS product_rank
                        FROM statu
                        WHERE {' AND '.join(conditions)}
                    )
                    SELECT id, title, price, content, processing_status,
                           source_link, published_at, photo_file_id, link_img,
                           source_channel_id, source_message_id,
                             aliexpress_product_id,
                             shipping_country, offer_type, category,
                             first_level_category_name,
                               second_level_category_name
                    FROM ranked_posts
                    WHERE product_rank = 1
                    ORDER BY product_occurrences DESC,
                             price ASC NULLS LAST,
                             published_at DESC NULLS LAST,
                             id DESC
                    {limit_clause}
                    """,
                    query_params,
                )
            else:
                cursor.execute(
                    f"""
                    SELECT id, title, price, content, processing_status,
                           source_link, published_at, photo_file_id, link_img,
                           source_channel_id, source_message_id,
                             aliexpress_product_id,
                              shipping_country, offer_type, category,
                             first_level_category_name,
                               second_level_category_name
                    FROM statu
                    WHERE {' AND '.join(conditions)}
                    ORDER BY published_at DESC NULLS LAST
                    """,
                    params,
                )
            columns = [description[0] for description in cursor.description]
            posts = [dict(zip(columns, row)) for row in cursor.fetchall()]
    finally:
        if conn:
            conn.close()

    if request_type == "trending":
        if trending_has_content_filters:
            return _rank_filtered_trending_posts(
                posts,
                intent,
                keywords,
                result_limit,
            )
        return (
            list(posts)
            if result_limit is None
            else list(posts)[:result_limit]
        )

    ranked_posts = []
    for position, post in enumerate(posts):
        match_score = _post_match_score(post, intent, keywords)
        if match_score is None:
            continue
        ranked_posts.append((match_score, position, post))

    if request_type in {"category_price_range", "product_price_range"}:
        ranked_posts.sort(
            key=lambda item: _price_order_key(item[2], intent, item[0], item[1])
        )
    elif request_type in {"category_cheapest", "product_cheapest"}:
        ranked_posts.sort(
            key=lambda item: _price_order_key(item[2], intent, item[0], item[1])
        )
    else:
        ranked_posts.sort(
            key=lambda item: _price_order_key(item[2], intent, item[0], item[1])
        )

    ranked = [post for _, _, post in ranked_posts]
    if request_type in {
        "category_cheapest",
        "category_price_range",
        "trending",
    } or (
        request_type in _PRODUCT_REQUEST_TYPES
        and _is_broad_product_request(intent, keywords)
    ):
        return _deduplicate_ranked_posts(ranked, result_limit, intent)
    return ranked if result_limit is None else ranked[:result_limit]


def _normalize_search_text(value):
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = re.sub(r"[\u064b-\u065f\u0670]", "", text)
    text = text.translate(str.maketrans("أإآىة", "ااايت"))
    text = re.sub(r"[_\-/.]+", " ", text)
    return " ".join(_SEARCH_TOKEN_PATTERN.findall(text))


def _search_tokens(value):
    return _normalize_search_text(value).split()


def _similarity_to_title(token, title_tokens):
    if not title_tokens:
        return 0.0
    return max(
        SequenceMatcher(None, token, title_token).ratio()
        for title_token in title_tokens
    )


def _specific_product_score(keywords, title, keyword_groups=None):
    title_normalized = _normalize_search_text(title)
    title_tokens = title_normalized.split()
    if not title_tokens:
        return None

    generic_tokens = set(_GENERIC_PRODUCT_KEYWORDS)
    generic_tokens.update(
        token
        for aliases in _CATEGORY_ALIASES.values()
        for alias in aliases
        for token in _search_tokens(alias)
    )
    specific_keywords = [
        keyword
        for keyword in keywords
        if any(
            token not in generic_tokens
            for token in _search_tokens(keyword)
        )
    ]
    specific_keywords = specific_keywords or keywords
    if not specific_keywords and not keyword_groups:
        return None

    alternatives = []
    seen_terms = set()
    for group in keyword_groups or []:
        group_terms = [
            keyword
            for keyword in group
            if any(
                token not in generic_tokens
                for token in _search_tokens(keyword)
            )
        ]
        if group_terms:
            alternatives.append(group_terms)
            seen_terms.update(
                _normalize_search_text(keyword)
                for keyword in group_terms
            )
    for keyword in specific_keywords:
        normalized_keyword = _normalize_search_text(keyword)
        if normalized_keyword not in seen_terms:
            alternatives.append([keyword])

    if not alternatives:
        return None

    scores = []
    for alternatives_for_term in alternatives:
        best_score = None
        for keyword in alternatives_for_term:
            normalized_keyword = _normalize_search_text(keyword)
            keyword_tokens = [
                token
                for token in normalized_keyword.split()
                if token not in generic_tokens
            ]
            if not keyword_tokens:
                continue
            if normalized_keyword and normalized_keyword in title_normalized:
                best_score = max(best_score or 0.0, 1.0)
                continue
            token_scores = [
                _similarity_to_title(token, title_tokens)
                for token in keyword_tokens
            ]
            compact_title = title_normalized.replace(" ", "")
            if any(
                re.search(r"\d", token)
                and token not in title_tokens
                and token not in compact_title
                for token in keyword_tokens
            ):
                continue
            if token_scores and min(token_scores) >= _FUZZY_TOKEN_THRESHOLD:
                candidate_score = sum(token_scores) / len(token_scores)
                best_score = max(best_score or 0.0, candidate_score)
        if best_score is not None:
            scores.append(best_score)

    if len(scores) != len(alternatives):
        return None
    return sum(scores) / len(scores)


def _unmapped_product_category_matches_title(post, category, intent, keywords):
    """Use title evidence for exact products with unmapped official categories."""
    if intent.get("request_type") not in _PRODUCT_REQUEST_TYPES:
        return False

    second_level = _normalize_search_text(
        post.get("second_level_category_name")
    )
    if second_level in {"", "غير محدد", "undefined", "unknown"}:
        return False
    if category_from_aliexpress_names(
        post.get("first_level_category_name"),
        post.get("second_level_category_name"),
    ) is not None:
        return False

    title = post.get("title")
    normalized_title = _normalize_search_text(title)
    terms = list(keywords or [])
    for group in intent.get("keyword_groups") or []:
        if isinstance(group, (list, tuple)):
            terms.extend(group)
    if not any(
        (normalized_term := _normalize_search_text(term))
        and normalized_term in normalized_title
        for term in terms
    ):
        return False

    return _classify_legacy_post_category(title) == category


def _is_broad_product_request(intent, keywords):
    """Treat a brand-only product request like a category search.

    A brand such as Samsung is a valid request, but it should return one
    representative offer per model. A model or product name remains allowed
    to return its different offers.
    """
    category = _infer_category(intent.get("category"), keywords)
    if not category:
        return False
    category_terms = {
        token
        for alias in _CATEGORY_ALIASES.get(category, set())
        for token in _search_tokens(alias)
    }
    brand_terms = {
        token
        for brand in _CATEGORY_BRANDS.get(category, set())
        for token in _search_tokens(brand)
    }
    keyword_tokens = {
        token
        for keyword in keywords
        for token in _search_tokens(keyword)
    }
    return bool(keyword_tokens) and keyword_tokens <= (category_terms | brand_terms)


def _price_order_key(post, intent, match_score, position):
    price = post.get("price")
    numeric_price = _price_as_float(price)
    missing_price = numeric_price is None
    mode = str(intent.get("price_mode") or "").casefold()
    target = intent.get("target_price")
    numeric_target = _price_as_float(target)

    if mode == "below":
        return (
            missing_price,
            -(numeric_price or 0),
            -match_score,
            position,
        )
    if mode == "above":
        return (missing_price, numeric_price or 0, -match_score, position)
    if mode == "near" and numeric_target is not None:
        return (
            missing_price,
            abs((numeric_price or 0) - numeric_target)
            if numeric_price is not None
            else 0,
            numeric_price or 0,
            -match_score,
            position,
        )
    # Explicit product searches intentionally keep all matching offers in
    # ascending price order; broad searches are also easier to compare this way.
    return (missing_price, numeric_price or 0, -match_score, position)


def _price_as_float(value):
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _stored_or_inferred_category_score(post, category, keywords):
    if not category:
        return 1.0
    first_level = _normalize_search_text(
        post.get("first_level_category_name")
    )
    second_level = _normalize_search_text(
        post.get("second_level_category_name")
    )
    missing_subcategory = {"", "غير محدد", "undefined", "unknown"}
    if second_level not in missing_subcategory:
        official_category = category_from_aliexpress_names(
            post.get("first_level_category_name"),
            post.get("second_level_category_name"),
        )
        # Valid AliExpress subcategories outside our mapped families belong
        # to the catch-all "other" search, not to an impossible category.
        if official_category is None and category == "other":
            return 1.0
        return 1.0 if official_category == category else None
    # A stored first-level value without its second-level value is not enough
    # to classify a product: the same first-level family can contain phones,
    # accessories, tablets, and other unrelated products.
    if first_level not in missing_subcategory:
        return None
    stored_category = str(post.get("category") or "").casefold().strip()
    valid_categories = set(_CATEGORY_ALIASES) | {"other"}
    if stored_category in valid_categories:
        return 1.0 if stored_category == category else None
    legacy_category = _classify_legacy_post_category(
        post.get("title"),
        post.get("content"),
    )
    if legacy_category:
        return 1.0 if legacy_category == category else None
    if category == "other":
        return None
    if _has_category_conflict(category, post.get("title")):
        return None
    return _category_score(category, keywords, post.get("title"))


def _classify_legacy_post_category(title, content=""):
    """Classify retained legacy rows locally without another AI request."""
    normalized = _normalize_search_text(
        " ".join(value for value in (title, content) if value)
    )
    tokens = set(normalized.split())
    if not tokens:
        return None

    if "haylou" in tokens:
        if tokens.intersection(
            {
                "x1", "w1", "gt1", "gt5", "moripods", "s30", "s35",
                "purfree",
            }
        ):
            return "headphones"
        if tokens.intersection({"rs3", "rs4", "solar", "gst"}):
            return "watches"

    category_order = (
        "headphones",
        "watches",
        "tablets",
        "laptops",
        "cameras",
        "gaming",
        "home",
    )
    for candidate in category_order:
        candidate_tokens = {
            token
            for alias in _CATEGORY_ALIASES[candidate]
            for token in _search_tokens(alias)
        }
        if tokens.intersection(candidate_tokens):
            return candidate

    accessory_tokens = {
        token
        for term in _COMMON_ACCESSORY_TERMS
        for token in _search_tokens(term)
    }
    phone_brand_tokens = {
        token
        for brand in _CATEGORY_BRANDS.get("phones", set())
        for token in _search_tokens(brand)
    }
    phone_alias_tokens = {
        token
        for alias in _CATEGORY_ALIASES["phones"]
        for token in _search_tokens(alias)
    }
    title_tokens = _search_tokens(title)
    phone_identity_tokens = phone_brand_tokens | phone_alias_tokens
    first_accessory_position = next(
        (
            position
            for position, token in enumerate(title_tokens)
            if token in accessory_tokens
        ),
        None,
    )
    first_phone_position = next(
        (
            position
            for position, token in enumerate(title_tokens)
            if token in phone_identity_tokens
        ),
        None,
    )
    has_accessory_term = bool(tokens.intersection(accessory_tokens))

    if first_accessory_position is not None and first_phone_position is not None:
        return (
            "accessories"
            if first_accessory_position < first_phone_position
            else "phones"
        )
    if first_phone_position is not None:
        return "phones"
    if has_accessory_term:
        return "accessories"
    return None


def _category_score(category, keywords, title):
    title_tokens = set(_search_tokens(title))
    if not title_tokens:
        return None

    if _has_category_conflict(category, title_tokens):
        return None

    category_terms = _CATEGORY_ALIASES.get(category)
    if category_terms is None:
        category_terms = set(keywords)
    normalized_terms = {
        token
        for term in category_terms
        for token in _search_tokens(term)
    }
    if title_tokens.intersection(normalized_terms):
        return 1.0

    brand_terms = {
        token
        for brand in _CATEGORY_BRANDS.get(category, set())
        for token in _search_tokens(brand)
    }
    if title_tokens.intersection(brand_terms):
        return 0.8
    return None


def _has_category_conflict(category, title_or_tokens):
    """Reject accessories and other product families for every category."""
    if category not in _CATEGORY_CONFLICT_TERMS:
        return False
    title_tokens = (
        title_or_tokens
        if isinstance(title_or_tokens, set)
        else set(_search_tokens(title_or_tokens))
    )
    conflict_tokens = {
        token
        for term in _CATEGORY_CONFLICT_TERMS[category]
        for token in _search_tokens(term)
    }
    return bool(title_tokens.intersection(conflict_tokens))


def _required_specs_match(title, required_specs):
    """Require every explicit RAM/storage condition to appear in the title."""
    if not required_specs:
        return True

    title_text = _normalize_search_text(title)
    compact_title_text = unicodedata.normalize(
        "NFKC",
        str(title or ""),
    ).casefold()
    for spec in required_specs:
        if not isinstance(spec, dict):
            return False
        spec_type = str(spec.get("type") or "").casefold()
        value = str(spec.get("value") or "").casefold()
        match = re.search(r"(\d+(?:\.\d+)?)(gb|tb|g|t)?", value)
        if not match:
            return False
        number = match.group(1)
        unit = match.group(2) or "gb"
        unit_aliases = {
            "gb": r"(?:gb|g|جيجا|جيجابايت)",
            "g": r"(?:gb|g|جيجا|جيجابايت)",
            "tb": r"(?:tb|t|تيرا)",
            "t": r"(?:tb|t|تيرا)",
        }
        unit_pattern = unit_aliases.get(unit, r"(?:gb|g|tb|t)")
        number_pattern = rf"(?<!\d){re.escape(number)}(?:\.0)?"

        if spec_type == "storage":
            if not re.search(
                rf"{number_pattern}\s*{unit_pattern}\b",
                title_text,
            ):
                return False
            continue

        if spec_type != "ram":
            return False

        ram_before = re.search(
            rf"(?:ram|ذاكرة\s*عشوائية|رام)\s*[:+\-/]?\s*"
            rf"{number_pattern}\s*{unit_pattern}?\b",
            title_text,
        )
        ram_after = re.search(
            rf"{number_pattern}\s*{unit_pattern}?\s*"
            rf"(?:ram|ذاكرة\s*عشوائية|رام)\b",
            title_text,
        )
        if not ram_before and not ram_after:
            # Common compact phone-title formats: 12+256GB or 12/256GB.
            compact_ram = re.search(
                rf"{number_pattern}\s*(?:gb|g)?\s*[+/]\s*\d+"
                rf"\s*(?:gb|g)?\b",
                compact_title_text,
            )
            if not compact_ram:
                return False
    return True


def _infer_category(category, keywords):
    if category in _CATEGORY_ALIASES or category == "other":
        return category

    keyword_tokens = {
        token
        for keyword in keywords
        for token in _search_tokens(keyword)
    }
    for candidate, aliases in _CATEGORY_ALIASES.items():
        normalized_aliases = {
            token
            for alias in aliases
            for token in _search_tokens(alias)
        }
        if keyword_tokens.intersection(normalized_aliases):
            return candidate
    for candidate, brands in _CATEGORY_BRANDS.items():
        brand_tokens = {
            token
            for brand in brands
            for token in _search_tokens(brand)
        }
        if keyword_tokens.intersection(brand_tokens):
            return candidate
    return None


def _deduplicate_ranked_posts(posts, limit, intent=None):
    """Keep one cheapest representative for each product in broad searches."""
    unique = {}
    for post in posts:
        key = _broad_product_identity(post)
        current = unique.get(key)
        if current is None:
            unique[key] = post
            continue
        if _price_order_key(
            post,
            intent or {},
            1.0,
            post.get("id") or 0,
        ) < _price_order_key(
            current,
            intent or {},
            1.0,
            current.get("id") or 0,
        ):
            unique[key] = post
    representatives = list(unique.values())
    if intent:
        representatives.sort(
            key=lambda post: _price_order_key(
                post,
                intent,
                1.0,
                post.get("id") or 0,
            )
        )
    return (
        representatives
        if limit is None
        else representatives[:limit]
    )


def _broad_product_identity(post):
    """Group separate AliExpress listings that advertise the same model."""
    title_tokens = _search_tokens(
        str(post.get("title") or "").replace("+", " plus ")
    )
    matched_brands = []
    for position, token in enumerate(title_tokens):
        manufacturer = next(
            (
                canonical
                for canonical, aliases in _BRAND_IDENTITY_GROUPS.items()
                if token in aliases
            ),
            token if any(
                token in {
                    brand_token
                    for brand in brands
                    for brand_token in _search_tokens(brand)
                }
                for brands in _CATEGORY_BRANDS.values()
            ) else None,
        )
        if manufacturer:
            matched_brands.append((position, token, manufacturer))

    if matched_brands:
        anchor_position, _, manufacturer = matched_brands[-1]
        family = next(
            (
                token
                for _, token, _ in reversed(matched_brands)
                if token in _PRESERVED_PRODUCT_FAMILIES
            ),
            None,
        )
        expanded = []
        for token in title_tokens[anchor_position + 1:]:
            if re.fullmatch(r"\d+(?:gb|tb|mah|mp|hz|w)", token):
                if expanded:
                    break
                continue
            expanded.extend(
                re.findall(r"[^\W\d_]+|\d+", token, re.UNICODE)
            )

        model_tokens = []
        found_number = False
        for token in expanded:
            if token in _MODEL_IDENTITY_STOP_TOKENS:
                if model_tokens:
                    break
                continue
            if token.isdigit():
                if found_number:
                    break
                model_tokens.append(token)
                found_number = True
                continue
            if not found_number:
                if len(model_tokens) < 2:
                    model_tokens.append(token)
                continue
            if token in _MODEL_VARIANT_TOKENS or (
                len(token) == 1 and token.isalpha()
            ):
                model_tokens.append(token)
                continue
            break

        if model_tokens:
            identity = [manufacturer]
            if family:
                identity.append(family)
            identity.extend(model_tokens)
            return "model:" + ":".join(dict.fromkeys(identity))
    return (
        post.get("aliexpress_product_id")
        or _normalize_search_text(post.get("title"))
        or f"post:{post.get('id')}"
    )


def product_identity(post):
    """Return the shared product grouping key used by offer search."""
    return _broad_product_identity(post)


def _price_matches_intent(post, intent):
    minimum = intent.get("min_price")
    maximum = intent.get("max_price")
    if minimum is None and maximum is None:
        return True
    try:
        price = float(post.get("price"))
    except (TypeError, ValueError):
        return False
    if minimum is not None and price < float(minimum):
        return False
    if maximum is not None and price > float(maximum):
        return False
    return True


def _shipping_country_matches(stored_country, requested_country, content=""):
    if not requested_country:
        return True
    stored = _normalize_search_text(
        " ".join(
            value
            for value in (stored_country, content)
            if value
        )
    )
    requested_parts = [
        _normalize_search_text(part)
        for part in re.split(r"[,،]|\s+و\s+", str(requested_country))
    ]
    requested_parts = [part for part in requested_parts if part]
    country_aliases = {
        "algeria": {"algeria", "الجزائر", "الجزاير", "dz"},
        "morocco": {"morocco", "المغرب", "maroc", "ma"},
        "tunisia": {"tunisia", "تونس", "tunisie", "tn"},
        "france": {"france", "فرنسا", "fr"},
        "united states": {"united states", "usa", "us", "امريكا", "الولايات المتحدة"},
    }
    normalized_aliases = {
        canonical: {
            _normalize_search_text(alias) for alias in aliases
        }
        for canonical, aliases in country_aliases.items()
    }

    def contains_alias(alias):
        return bool(
            alias
            and re.search(
                rf"(?:^|\s){re.escape(alias)}(?:$|\s)",
                stored,
            )
        )

    return bool(
        stored
        and requested_parts
        and any(
            contains_alias(part)
            or any(
                contains_alias(alias)
                for alias in normalized_aliases.get(part, {part})
            )
            for part in requested_parts
        )
    )


def _offer_type_matches(stored_offer_type, requested_offer_type):
    if not requested_offer_type:
        return True
    requested = str(requested_offer_type).casefold().strip()
    if requested not in {"bundle", "normal"}:
        return True
    stored = str(stored_offer_type or "normal").casefold().strip()
    return stored == requested


def _official_category_matches(post, requested_category):
    if not requested_category:
        return True
    return _normalize_search_text(
        post.get("second_level_category_name")
    ) == _normalize_search_text(requested_category)


def _official_categories_match(post, requested_categories):
    if not requested_categories:
        return True
    stored_category = _normalize_search_text(
        post.get("second_level_category_name")
    )
    return any(
        stored_category == _normalize_search_text(category)
        for category in requested_categories
    )


def _saved_subcategory_matches(post, requested_subcategory):
    if not requested_subcategory:
        return True
    return (
        saved_offer_filter_from_subcategory(
            post.get("second_level_category_name")
        )
        == requested_subcategory
    )


def _saved_category_matches(post, requested_category):
    if not requested_category:
        return True
    if requested_category not in SAVED_OFFER_CATEGORY_COLUMN_FILTERS:
        return False
    return (
        str(post.get("category") or "").casefold().strip()
        == requested_category
    )


def _post_match_score(post, intent, keywords):
    request_type = intent.get("request_type")
    if not _saved_category_matches(
        post,
        intent.get("saved_category"),
    ):
        return None
    if not _saved_subcategory_matches(
        post,
        intent.get("saved_subcategory"),
    ):
        return None
    if not _official_category_matches(
        post,
        intent.get("official_category"),
    ):
        return None
    if not _official_categories_match(
        post,
        intent.get("official_categories"),
    ):
        return None
    if not _price_matches_intent(post, intent):
        return None
    if not _required_specs_match(
        post.get("title"),
        intent.get("required_specs", []),
    ):
        return None
    if not _shipping_country_matches(
        post.get("shipping_country"),
        intent.get("shipping_country"),
        post.get("content"),
    ):
        return None
    if not _offer_type_matches(
        post.get("offer_type"),
        intent.get("offer_type"),
    ):
        return None
    category = (
        None
        if intent.get("official_categories")
        else _infer_category(intent.get("category"), keywords)
    )
    if request_type in _PRODUCT_REQUEST_TYPES:
        product_score = _specific_product_score(
            keywords,
            post.get("title"),
            intent.get("keyword_groups"),
        )
        if product_score is None:
            return None
        if category:
            category_score = _stored_or_inferred_category_score(
                post,
                category,
                keywords,
            )
            if category_score is None and _unmapped_product_category_matches_title(
                post,
                category,
                intent,
                keywords,
            ):
                category_score = 1.0
            if category_score is None:
                return None
            product_score *= category_score
        return product_score
    if request_type in {"category_cheapest", "category_price_range"}:
        if intent.get("official_categories"):
            return 1.0
        return _stored_or_inferred_category_score(
            post,
            category,
            keywords,
        )
    if request_type == "trending":
        score = 1.0
        if category:
            category_score = _stored_or_inferred_category_score(
                post,
                category,
                keywords,
            )
            if category_score is None:
                return None
            score *= category_score

        generic_tokens = set(_GENERIC_PRODUCT_KEYWORDS)
        generic_tokens.update(
            token
            for aliases in _CATEGORY_ALIASES.values()
            for alias in aliases
            for token in _search_tokens(alias)
        )
        specific_keywords = [
            keyword
            for keyword in keywords
            if any(
                token not in generic_tokens
                for token in _search_tokens(keyword)
            )
        ]
        if specific_keywords:
            product_score = _specific_product_score(
                specific_keywords,
                post.get("title"),
                intent.get("keyword_groups"),
            )
            if product_score is None:
                return None
            score *= product_score
        return score
    return 1.0


def _rank_filtered_trending_posts(posts, intent, keywords, limit):
    """Apply semantic filters before grouping trending product offers."""
    groups = {}
    for post in posts:
        if _post_match_score(post, intent, keywords) is None:
            continue
        product_key = _broad_product_identity(post)
        current = groups.get(product_key)
        if current is None:
            groups[product_key] = [1, post]
            continue
        current[0] += 1
        if _price_order_key(
            post,
            intent,
            1.0,
            post.get("id") or 0,
        ) < _price_order_key(
            current[1],
            intent,
            1.0,
            current[1].get("id") or 0,
        ):
            current[1] = post

    representatives = [
        (occurrences, representative)
        for occurrences, representative in groups.values()
    ]

    if intent.get("price_mode") or intent.get("target_price") is not None:
        representatives.sort(
            key=lambda item: _price_order_key(
                item[1],
                intent,
                1.0,
                item[1].get("id") or 0,
            )
        )
    else:
        representatives.sort(
            key=lambda item: (
                -item[0],
                item[1].get("price") is None,
                item[1].get("price")
                if item[1].get("price") is not None
                else 0,
                -(item[1].get("id") or 0),
            )
        )
    ranked_posts = [post for _, post in representatives]
    return ranked_posts if limit is None else ranked_posts[:limit]


def save_channel_photo_file_id(post_id, photo_file_id):
    update_channel_post(post_id, photo_file_id=photo_file_id)


def save_channel_photo_link(post_id, link_img):
    update_channel_post(post_id, link_img=link_img)


def delete_expired_channel_posts():
    """Delete channel posts beyond the retention window in every state."""
    if not DATABASE_URL:
        return 0

    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute(
                """
                DELETE FROM statu
                WHERE created_at < CURRENT_TIMESTAMP - INTERVAL '2 days'
                """
            )
            deleted_rows = cursor.rowcount
        conn.commit()
        return deleted_rows
    except Exception:
        if conn:
            conn.rollback()
        raise
    finally:
        if conn:
            conn.close()


def save_user(chat_id, first_name, last_name):
    if not DATABASE_URL:
        return
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO user_bot (
                chat_id, first_name, last_name,
                requests_today, requests_this_month, requests_last_month
            )
            VALUES (%s, %s, %s, 0, 0, 0)
            ON CONFLICT (chat_id) DO NOTHING
        """, (chat_id, first_name, last_name))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logger.error(f"Error saving user: {e}")


def record_user_activity(chat_id, first_name="", last_name=""):
    """Record one user request and roll daily/monthly counters forward safely."""
    if not DATABASE_URL:
        return
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO user_bot (
                    chat_id, first_name, last_name,
                    requests_today, requests_this_month, requests_last_month
                )
                VALUES (%s, %s, %s, 0, 0, 0)
                ON CONFLICT (chat_id) DO NOTHING
                """,
                (chat_id, first_name, last_name),
            )
            cursor.execute(
                """
                UPDATE user_bot
                SET requests_last_month = CASE
                        WHEN activity_month IS NOT NULL
                         AND activity_month < date_trunc('month', CURRENT_DATE)::date
                        THEN COALESCE(requests_this_month, 0)
                        ELSE COALESCE(requests_last_month, 0)
                    END,
                    requests_this_month = CASE
                        WHEN activity_month IS NULL
                         OR activity_month < date_trunc('month', CURRENT_DATE)::date
                        THEN 0
                        ELSE COALESCE(requests_this_month, 0)
                    END,
                    requests_today = CASE
                        WHEN activity_day IS NULL OR activity_day < CURRENT_DATE
                        THEN 0
                        ELSE COALESCE(requests_today, 0)
                    END,
                    activity_month = date_trunc('month', CURRENT_DATE)::date,
                    activity_day = CURRENT_DATE
                WHERE chat_id = %s
                """,
                (chat_id,),
            )
            cursor.execute(
                """
                UPDATE user_bot
                SET requests_today = requests_today + 1,
                    requests_this_month = requests_this_month + 1
                WHERE chat_id = %s
                """,
                (chat_id,),
            )
        conn.commit()
    except Exception:
        if conn:
            conn.rollback()
        logger.exception("Could not record activity for user %s", chat_id)
    finally:
        if conn:
            conn.close()


def get_activity_stats(period):
    """Return request and active-user totals for today/current/previous month."""
    columns = {
        "today": "requests_today",
        "month": "requests_this_month",
        "previous": "requests_last_month",
    }
    column = columns.get(period)
    if not column or not DATABASE_URL:
        return {"requests": 0, "users": 0}
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT COALESCE(SUM({column}), 0),
                       COUNT(*) FILTER (WHERE COALESCE({column}, 0) > 0)
                FROM user_bot
                """
            )
            requests, users = cursor.fetchone()
            return {"requests": int(requests or 0), "users": int(users or 0)}
    finally:
        if conn:
            conn.close()


def get_user_chat_ids():
    if not DATABASE_URL:
        return []
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute("SELECT chat_id FROM user_bot ORDER BY chat_id")
            return [row[0] for row in cursor.fetchall()]
    finally:
        if conn:
            conn.close()


def get_total_user_count():
    """Return the total number of registered bot users."""
    if not DATABASE_URL:
        return 0
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM user_bot")
            row = cursor.fetchone()
            return int(row[0] or 0)
    finally:
        if conn:
            conn.close()


def delete_user_chat_ids(chat_ids):
    """Delete only the explicitly verified inaccessible users."""
    if not DATABASE_URL or not chat_ids:
        return 0

    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute(
                "DELETE FROM user_bot WHERE chat_id = ANY(%s)",
                (list(chat_ids),),
            )
            deleted_count = cursor.rowcount
        conn.commit()
        return int(deleted_count or 0)
    except Exception:
        if conn:
            conn.rollback()
        logger.exception("Could not delete inaccessible users")
        raise
    finally:
        if conn:
            conn.close()


def add_error_log(level, logger_name, message):
    """Persist a formatted server error without allowing logging to recurse."""
    if not DATABASE_URL:
        return
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO bot_error_logs (level, logger_name, message)
                VALUES (%s, %s, %s)
                """,
                (str(level)[:20], str(logger_name)[:250], str(message)[:12000]),
            )
        conn.commit()
    except Exception:
        if conn:
            conn.rollback()
    finally:
        if conn:
            conn.close()


def delete_expired_error_logs(retention_days=None):
    """Delete error records older than the configured admin-report retention."""
    if not DATABASE_URL:
        return 0

    retention_days = (
        ERROR_LOG_RETENTION_DAYS
        if retention_days is None
        else max(1, int(retention_days))
    )
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute(
                """
                DELETE FROM bot_error_logs
                WHERE created_at < CURRENT_TIMESTAMP
                    - (%s * INTERVAL '1 day')
                """,
                (retention_days,),
            )
            deleted_rows = cursor.rowcount
        conn.commit()
        return deleted_rows
    except Exception:
        if conn:
            conn.rollback()
        raise
    finally:
        if conn:
            conn.close()


def get_error_logs(period):
    """Return persisted errors for the requested period."""
    interval = {
        "today": "1 day",
        "week": "7 days",
        "hour": "1 hour",
        "quarter": "15 minutes",
    }.get(period)
    if not interval or not DATABASE_URL:
        return []
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT level, logger_name, message, created_at
                FROM bot_error_logs
                WHERE created_at >= CURRENT_TIMESTAMP - %s::interval
                ORDER BY created_at DESC
                """,
                (interval,),
            )
            return [
                {
                    "level": row[0],
                    "logger_name": row[1],
                    "message": row[2],
                    "created_at": row[3],
                }
                for row in cursor.fetchall()
            ]
    finally:
        if conn:
            conn.close()


def get_admin_cart_row(row_id):
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, linkcart, pricecart, pricefinalecart, stor, ship
                FROM cart WHERE id = %s
                """,
                (row_id,),
            )
            row = cursor.fetchone()
            if not row:
                return None
            columns = [description[0] for description in cursor.description]
            return dict(zip(columns, row))
    finally:
        if conn:
            conn.close()


def get_admin_cart_rows():
    """Return cart rows in the order and shape used by the admin interface."""
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT id, linkcart, pricecart, pricefinalecart, stor, ship
                FROM cart
                ORDER BY id
            """)
            columns = [description[0] for description in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]
    finally:
        if conn:
            conn.close()


def _run_cart_mutation(query, params):
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute(query, params)
            affected_rows = cursor.rowcount
        conn.commit()
        return affected_rows
    except Exception:
        if conn:
            conn.rollback()
        raise
    finally:
        if conn:
            conn.close()


def update_cart_row(row_id, link, price, final_price, stor, shipping):
    """Update one cart row and fail if the requested id does not exist."""
    affected_rows = _run_cart_mutation(
        """
            UPDATE cart
            SET linkcart = %s,
                pricecart = %s,
                pricefinalecart = %s,
                stor = %s,
                ship = %s
            WHERE id = %s
        """,
        (link, price, final_price, stor, shipping, row_id),
    )
    if affected_rows != 1:
        raise ValueError("Cart row was not found")


def insert_cart_row(row_id, link, price, final_price, stor, shipping):
    """Insert a cart row using the id supplied in the admin template."""
    _run_cart_mutation(
        """
            INSERT INTO cart
                (id, linkcart, pricecart, pricefinalecart, stor, ship)
            VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (row_id, link, price, final_price, stor, shipping),
    )

def save_ai_service_usage_state(state):
    """Atomically replace the persisted AI usage snapshot."""
    if not DATABASE_URL:
        return False
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO ai_service_usage_state (state_key, state)
                VALUES ('global', %s::jsonb)
                ON CONFLICT (state_key) DO UPDATE SET
                    state = EXCLUDED.state,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (json.dumps(state),),
            )
        conn.commit()
        return True
    except Exception:
        if conn:
            conn.rollback()
        logger.exception("Could not persist AI usage state")
        return False
    finally:
        if conn:
            conn.close()

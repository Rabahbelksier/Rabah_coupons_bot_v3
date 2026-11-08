"""PostgreSQL persistence for visit attribution and durable conversion delivery.

Source capture and the outbox insert share a savepoint. Optional tracking
failures must not roll back the basic bot-user insert.
"""

import hashlib
import json
import logging
from contextlib import contextmanager

logger = logging.getLogger(__name__)

SCHEMA = """
ALTER TABLE user_bot ADD COLUMN IF NOT EXISTS meta_source_id TEXT;
CREATE TABLE IF NOT EXISTS meta_bot_visits (
    token_hash TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    pixel_id TEXT NOT NULL,
    user_data JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS meta_bot_visits_expiry
    ON meta_bot_visits (expires_at);
ALTER TABLE meta_bot_visits ADD COLUMN IF NOT EXISTS consumed_by TEXT;
CREATE TABLE IF NOT EXISTS meta_bot_events (
    event_id TEXT PRIMARY KEY,
    user_key TEXT NOT NULL UNIQUE,
    pixel_id TEXT NOT NULL,
    body JSONB,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'sending', 'sent', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    lease_until TIMESTAMPTZ,
    lease_id TEXT,
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS meta_bot_events_due
    ON meta_bot_events (next_attempt_at)
    WHERE status IN ('pending', 'sending');
"""


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _connect():
    # Lazy import keeps schema setup free of circular imports.
    from core.db import get_db_connection
    return get_db_connection(connect_timeout=3)


@contextmanager
def transaction():
    conn = _connect()
    try:
        with conn:
            with conn.cursor() as cursor:
                # Tracking must not monopolize database resources or delay
                # ordinary bot registration on a lock indefinitely.
                cursor.execute("SET LOCAL statement_timeout = '5000ms'")
                cursor.execute("SET LOCAL lock_timeout = '3000ms'")
                yield cursor
    finally:
        conn.close()


def create_visit(token, source_id, pixel_id, user_data, ttl):
    with transaction() as cursor:
        cursor.execute(
            """
            INSERT INTO meta_bot_visits
                (token_hash, source_id, pixel_id, user_data, expires_at)
            VALUES (%s, %s, %s, %s::jsonb,
                    CURRENT_TIMESTAMP + %s * INTERVAL '1 second')
            """,
            (digest(token), source_id, pixel_id, json.dumps(user_data), ttl),
        )


def enrich_visit(token, user_data):
    """Only the unexpired, unconsumed bearer visit may receive browser cookies."""
    with transaction() as cursor:
        cursor.execute(
            """
            UPDATE meta_bot_visits SET user_data = user_data || %s::jsonb
            WHERE token_hash = %s AND consumed_at IS NULL
              AND expires_at > CURRENT_TIMESTAMP
            """,
            (json.dumps(user_data), digest(token)),
        )


def register_start(chat_id, first_name, last_name, attribution):
    """Keep registration even if optional source/outbox work fails."""
    with transaction() as cursor:
        cursor.execute(
            """
            INSERT INTO user_bot (
                chat_id, first_name, last_name,
                requests_today, requests_this_month, requests_last_month
            ) VALUES (%s, %s, %s, 0, 0, 0)
            ON CONFLICT (chat_id) DO NOTHING
            """,
            (chat_id, first_name, last_name),
        )
        cursor.execute("SAVEPOINT meta_source_and_event")
        try:
            queued = _save_source_and_event(cursor, chat_id, attribution)
        except Exception as error:
            cursor.execute("ROLLBACK TO SAVEPOINT meta_source_and_event")
            # Error bodies can contain SQL row values or credentials. Only
            # safe diagnostic identifiers belong in the console.
            logger.warning(
                "Meta attribution failed; basic user registration retained "
                "(error_type=%s, sqlstate=%s)",
                type(error).__name__, getattr(error, "pgcode", None),
            )
            queued = False
        cursor.execute("RELEASE SAVEPOINT meta_source_and_event")
        return queued


def _save_source_and_event(cursor, chat_id, attribution):
    # Lock before reading/updating to serialize concurrent start commands.
    cursor.execute(
        "SELECT meta_source_id FROM user_bot WHERE chat_id = %s FOR UPDATE",
        (chat_id,),
    )
    previous_source = cursor.fetchone()[0]
    source = attribution["source"]
    payload = attribution["start"]
    browser_data = {}
    token = attribution.get("visit_token")
    if token:
        user_key = digest(f"telegram:rabahcoupons1bot:{chat_id}")
        cursor.execute(
            """
            SELECT source_id, pixel_id, user_data FROM meta_bot_visits
            WHERE token_hash = %s AND expires_at > CURRENT_TIMESTAMP
              AND (consumed_at IS NULL OR consumed_by = %s)
            FOR UPDATE
            """,
            (digest(token), user_key),
        )
        visit = cursor.fetchone()
        if visit is None:
            # Unknown, expired and another account's tokens are not sources.
            return False
        payload, visit_pixel, browser_data = visit
        cursor.execute(
            """
            UPDATE meta_bot_visits
            SET consumed_at = COALESCE(consumed_at, CURRENT_TIMESTAMP),
                consumed_by = %s
            WHERE token_hash = %s
            """,
            (user_key, digest(token)),
        )
        attribution = {
            **attribution,
            "eligible": attribution.get("eligible", True)
            and visit_pixel == attribution["pixel"],
        }
    if not payload:
        return False
    cursor.execute(
        "UPDATE user_bot SET meta_source_id = %s WHERE chat_id = %s",
        (payload, chat_id),
    )
    if (
        payload != source or previous_source == source
        or not attribution.get("eligible", True)
    ):
        return False

    # Only an exact, unexpired bearer token may link browser matching data.
    user_key = digest(f"telegram:rabahcoupons1bot:{chat_id}")
    user_data = {
        key: value for key, value in browser_data.items()
        if key in ("fbp", "fbc", "client_ip_address", "client_user_agent")
        and isinstance(value, str)
    }
    user_data["external_id"] = [user_key]
    body = {
        "data": [{
            "event_name": "TelegramBotStart",
            "event_time": attribution["event_time"],
            "event_id": f"tbs_{user_key}",
            "action_source": "chat",
            "user_data": user_data,
        }],
    }
    if attribution["test_code"]:
        body["test_event_code"] = attribution["test_code"]
    cursor.execute(
        """
        INSERT INTO meta_bot_events (event_id, user_key, pixel_id, body)
        VALUES (%s, %s, %s, %s::jsonb)
        ON CONFLICT (user_key) DO NOTHING RETURNING event_id
        """,
        (f"tbs_{user_key}", user_key, attribution["pixel"], json.dumps(body)),
    )
    return cursor.fetchone() is not None


def claim_event(lease_id):
    with transaction() as cursor:
        cursor.execute(
            """
            WITH due AS (
                SELECT event_id FROM meta_bot_events
                WHERE ((status = 'pending' AND next_attempt_at <= CURRENT_TIMESTAMP)
                    OR (status = 'sending' AND lease_until < CURRENT_TIMESTAMP))
                  AND created_at > CURRENT_TIMESTAMP - INTERVAL '6 days'
                  AND attempts < 20
                ORDER BY next_attempt_at
                FOR UPDATE SKIP LOCKED LIMIT 1
            )
            UPDATE meta_bot_events AS event
            SET status = 'sending', attempts = attempts + 1,
                lease_until = CURRENT_TIMESTAMP + INTERVAL '2 minutes',
                lease_id = %s
            FROM due WHERE event.event_id = due.event_id
            RETURNING event.event_id, event.pixel_id, event.body, event.attempts
            """,
            (lease_id,),
        )
        return cursor.fetchone()


def finish_event(event_id, lease_id, status, error=None, delay=0):
    with transaction() as cursor:
        cursor.execute(
            """
            UPDATE meta_bot_events
            SET status = %s, last_error = %s, lease_id = NULL, lease_until = NULL,
                next_attempt_at = CURRENT_TIMESTAMP + %s * INTERVAL '1 second',
                body = CASE WHEN %s IN ('sent', 'failed') THEN NULL ELSE body END
            WHERE event_id = %s AND lease_id = %s AND status = 'sending'
            """,
            (status, error, delay, status, event_id, lease_id),
        )


def cleanup():
    with transaction() as cursor:
        cursor.execute(
            "DELETE FROM meta_bot_visits WHERE expires_at <= CURRENT_TIMESTAMP"
        )
        cursor.execute(
            """
            UPDATE meta_bot_events SET status = 'failed', body = NULL,
                last_error = 'expired_or_attempt_limit', lease_id = NULL,
                lease_until = NULL
            WHERE status IN ('pending', 'sending')
              AND (created_at <= CURRENT_TIMESTAMP - INTERVAL '6 days'
                OR (attempts >= 20 AND
                    (status = 'pending' OR lease_until < CURRENT_TIMESTAMP)))
            """
        )

"""PostgreSQL persistence for visit attribution and durable conversion delivery.

All registration decisions and the outbox insert share one transaction. The
existing user primary key arbitrates concurrent /start deliveries.
"""

import hashlib
import json
from contextlib import contextmanager


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
    """Save any received source; queue at most one matching conversion."""
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
        # Existing users with an empty/different source are eligible too.
        # Lock before reading/updating to serialize concurrent start commands.
        cursor.execute(
            "SELECT meta_source_id FROM user_bot WHERE chat_id = %s FOR UPDATE",
            (chat_id,),
        )
        previous_source = cursor.fetchone()[0]
        source = attribution["source"]
        payload = attribution["start"]
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

        # Names are neither verified nor necessary. Never invent email/phone or
        # send Telegram IDs as Facebook login IDs. Hash a namespaced external ID.
        user_key = digest(f"telegram:rabahcoupons1bot:{chat_id}")
        # A shared campaign payload cannot identify a browser visit. Never
        # attach a stranger's IP/cookies by guessing from timing or IP address.
        user_data = {"external_id": [user_key]}
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
        if cursor.fetchone() is None:
            return False
        return True


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

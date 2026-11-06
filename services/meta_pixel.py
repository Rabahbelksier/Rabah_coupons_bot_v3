import hashlib
import logging
import time

import requests

from config import (
    META_CAPI_ACCESS_TOKEN,
    META_PIXEL_ID,
    META_TEST_EVENT_CODE,
)
from core.db import mark_meta_event_failed, mark_meta_event_sent

logger = logging.getLogger(__name__)
META_GRAPH_API_VERSION = 'v26.0'


def _send_telegram_start_event(event):
    if not META_PIXEL_ID or not META_CAPI_ACCESS_TOKEN:
        raise RuntimeError("Meta Pixel ID or CAPI access token is not configured")

    external_id = hashlib.sha256(
        str(event["chat_id"]).strip().lower().encode("utf-8")
    ).hexdigest()
    user_data = {
        "external_id": [external_id],
    }
    if event.get("fbp"):
        user_data["fbp"] = event["fbp"]
    if event.get("fbc"):
        user_data["fbc"] = event["fbc"]
    if event.get("client_ip_address"):
        user_data["client_ip_address"] = event["client_ip_address"]
    if event.get("client_user_agent"):
        user_data["client_user_agent"] = event["client_user_agent"]

    payload = {
        "data": [
            {
                "event_name": "TelegramBotStart",
                "event_time": int(time.time()),
                "event_id": f"telegram-start-{event['id']}",
                "action_source": "chat",
                "user_data": user_data,
                "custom_data": {
                    "attribution_id": event["attribution_id"],
                },
            }
        ]
    }
    if META_TEST_EVENT_CODE:
        payload["test_event_code"] = META_TEST_EVENT_CODE

    response = requests.post(
        (
            f"https://graph.facebook.com/{META_GRAPH_API_VERSION}/"
            f"{META_PIXEL_ID}/events"
        ),
        params={"access_token": META_CAPI_ACCESS_TOKEN},
        json=payload,
        timeout=10,
    )
    response.raise_for_status()
    try:
        response_body = response.json()
    except ValueError as error:
        raise RuntimeError("Meta returned an unreadable response") from error
    if response_body.get("error") or response_body.get("events_received", 0) < 1:
        raise RuntimeError("Meta rejected the TelegramBotStart event")


def send_pending_telegram_start_events(events):
    for event in events:
        try:
            _send_telegram_start_event(event)
        except Exception as error:
            error_type = type(error).__name__
            logger.warning(
                "Meta TelegramBotStart delivery failed for event %s (%s)",
                event["id"],
                error_type,
            )
            mark_meta_event_failed(
                event["id"],
                f"Meta request failed ({error_type})",
            )
        else:
            mark_meta_event_sent(event["id"])

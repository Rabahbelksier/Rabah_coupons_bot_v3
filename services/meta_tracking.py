"""Configuration, payload validation and a bounded durable CAPI sender."""

import ipaddress
import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass, field

import requests

import config
from core import meta_store

logger = logging.getLogger(__name__)
START_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
VISIT_PATTERN = re.compile(r"mv_[A-Za-z0-9_-]{32}\Z")
COOKIE_PATTERN = re.compile(r"fb\.\d+\.\d{10,16}\.[A-Za-z0-9_.-]{1,500}\Z")
CLICK_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,500}\Z")
BOT_URL = "https://t.me/rabahcoupons1bot"


@dataclass(frozen=True)
class Settings:
    enabled: bool
    pixel: str
    source: str
    token: str = field(repr=False)
    test_code: str
    version: str
    ttl: int
    proxy_hops: int

    @property
    def source_valid(self):
        return bool(START_PATTERN.fullmatch(self.source))

    @property
    def browser_ready(self):
        return bool(self.enabled and re.fullmatch(r"\d{1,30}", self.pixel))

    @property
    def ready(self):
        return bool(
            self.browser_ready and self.source_valid
            and self.token
            and re.fullmatch(r"v\d{1,3}\.0", self.version)
            and config.DATABASE_URL
        )


def settings():
    return Settings(
        config.META_TRACKING_ENABLED, config.META_PIXEL_ID,
        config.META_ATTRIBUTION_ID, config.META_CAPI_ACCESS_TOKEN,
        config.META_TEST_EVENT_CODE, config.META_GRAPH_API_VERSION,
        config.META_VISIT_TTL_SECONDS, config.META_TRUSTED_PROXY_HOPS,
    )


def start_attribution(args, event_time=None):
    current = settings()
    payload = args[0] if isinstance(args, (list, tuple)) and len(args) == 1 else ""
    if not isinstance(payload, str) or not START_PATTERN.fullmatch(payload):
        return None
    attribution = {
        "source": current.source,
        "pixel": current.pixel,
        "start": payload,
        "test_code": current.test_code,
        "event_time": int(event_time if event_time is not None else time.time()),
        # Capturing a source must not depend on Meta credentials/readiness.
        "eligible": current.ready,
    }
    if VISIT_PATTERN.fullmatch(payload):
        attribution["visit_token"] = payload
    return attribution


def browser_cookies(values):
    return {
        name: value for name in ("fbp", "fbc")
        if isinstance(value := values.get(name), str)
        and COOKIE_PATTERN.fullmatch(value)
    }


def request_user_data(req, current):
    values = browser_cookies({
        "fbp": req.cookies.get("_fbp"),
        "fbc": req.cookies.get("_fbc"),
    })
    click_id = req.args.get("fbclid", "")
    if CLICK_PATTERN.fullmatch(click_id):
        # fbc is not hashed; create it only from a real fbclid.
        values["fbc"] = f"fb.1.{int(time.time() * 1000)}.{click_id}"
    address = req.remote_addr or ""
    if current.proxy_hops:
        forwarded = req.headers.get("X-Forwarded-For", "").split(",")
        if len(forwarded) >= current.proxy_hops:
            address = forwarded[-current.proxy_hops].strip()
    try:
        values["client_ip_address"] = str(ipaddress.ip_address(address))
    except ValueError:
        pass
    agent = req.headers.get("User-Agent", "")[:1024]
    if agent:
        values["client_user_agent"] = agent
    return values


def privacy_opt_out(req):
    return (
        req.headers.get("Sec-GPC") == "1"
        or req.headers.get("DNT") == "1"
        or req.cookies.get("meta_consent") == "denied"
    )


class MetaDeliveryWorker:
    def __init__(self):
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = None

    def start(self):
        current = settings()
        if not config.DATABASE_URL:
            return
        if current.enabled and not current.ready:
            logger.warning("Meta tracking enabled but configuration is incomplete")
        # Retention cleanup remains active even after tracking is switched off.
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="meta-conversion-delivery", daemon=True,
            )
            self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def deliver_one(self):
        current = settings()
        if not current.ready:
            return False
        lease_id = uuid.uuid4().hex
        event = meta_store.claim_event(lease_id)
        if not event:
            return False
        event_id, pixel, body, attempts = event
        # Pin the destination and test code at registration, never silently send
        # a previous campaign's events to a newly configured pixel.
        status, error = "pending", "transport"
        try:
            response = requests.post(
                f"https://graph.facebook.com/{current.version}/{pixel}/events",
                headers={"Authorization": f"Bearer {current.token}"},
                json=body, timeout=(3.05, 12), allow_redirects=False,
            )
            try:
                result = response.json()
                result = result if isinstance(result, dict) else {}
            except ValueError:
                result = {}
            meta_error = result.get("error")
            meta_error = meta_error if isinstance(meta_error, dict) else {}
            if (
                200 <= response.status_code < 300
                and result.get("events_received") == 1 and not meta_error
            ):
                status, error = "sent", None
            else:
                code = meta_error.get("code")
                # Do not persist provider messages, response bodies, or request
                # exceptions: they can include tokens or customer information.
                safe_code = code if isinstance(code, int) else "unknown"
                error = f"http_{response.status_code}_code_{safe_code}"
                transient = (
                    response.status_code in (408, 425, 429)
                    or response.status_code >= 500
                    or meta_error.get("is_transient") is True
                    or code in (1, 2, 4, 17, 32, 341, 613)
                    or (200 <= response.status_code < 300 and not meta_error)
                )
                status = "pending" if transient else "failed"
        except requests.RequestException:
            pass
        if status == "pending" and attempts >= 20:
            status = "failed"
        meta_store.finish_event(
            event_id, lease_id, status, error,
            delay=min(6 * 60 * 60, 30 * (2 ** min(attempts, 10))),
        )
        if status != "sent":
            logger.warning("Meta conversion delivery: %s (%s)", status, error)
        return True

    def _run(self):
        next_cleanup = 0
        while not self._stop.is_set():
            try:
                if time.monotonic() >= next_cleanup:
                    meta_store.cleanup()
                    next_cleanup = time.monotonic() + 3600
                sent = self.deliver_one()
            except Exception:
                # Database errors may contain row data. Keep diagnostics generic.
                logger.warning("Meta delivery storage unavailable; retrying later")
                sent = False
            self._stop.wait(0.2 if sent else 5)


meta_delivery_worker = MetaDeliveryWorker()

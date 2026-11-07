"""An invisible tracking/redirect page on the bot's existing Flask server."""

import logging
import secrets
from urllib.parse import urlsplit

from flask import Blueprint, Response, render_template, request

from core import meta_store
from services.meta_tracking import (
    BOT_URL, VISIT_PATTERN, browser_cookies, privacy_opt_out,
    request_user_data, settings,
)

logger = logging.getLogger(__name__)
meta_redirect = Blueprint("meta_redirect", __name__, template_folder="../templates")


@meta_redirect.after_request
def private_response(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@meta_redirect.get("/bot")
def redirect_to_bot():
    current = settings()
    token = ""
    pixel = ""
    target = BOT_URL
    if current.ready and not privacy_opt_out(request):
        pixel = current.pixel
        token = "mv_" + secrets.token_urlsafe(24)
        try:
            meta_store.create_visit(
                token, current.source, current.pixel,
                request_user_data(request, current), current.ttl,
            )
            target += f"?start={token}"
        except Exception:
            logger.warning("Meta visit storage unavailable; using direct campaign link")
            token = ""
            target += f"?start={current.source}"
    elif current.enabled and not current.ready:
        logger.warning("Meta redirect is untracked: configuration is incomplete")
    response = Response(render_template(
        "meta_redirect.html", target=target, pixel=pixel, token=token,
    ), mimetype="text/html")
    return response


@meta_redirect.post("/bot/visit")
def capture_browser_cookies():
    current = settings()
    # A same-origin beacon with JSON body is not a public cross-origin collector.
    # No CORS access, no query-string tokens and no arbitrary identifying fields.
    if (
        not current.ready or privacy_opt_out(request)
        or request.headers.get("Sec-Fetch-Site") == "cross-site"
        or (
            request.headers.get("Origin")
            and urlsplit(request.headers["Origin"]).netloc != request.host
        )
    ):
        return Response(status=204)
    if request.content_length is None or request.content_length > 2048:
        return Response(status=413)
    values = request.get_json(silent=True)
    if not isinstance(values, dict):
        return Response(status=400)
    token = values.get("token", "")
    if not isinstance(token, str) or not VISIT_PATTERN.fullmatch(token):
        return Response(status=400)
    data = browser_cookies(values)
    # Prefer the current click ID's fbc over a stale fbc from a previous visit.
    data.pop("fbc", None)
    if data:
        try:
            meta_store.enrich_visit(token, data)
        except Exception:
            logger.warning("Meta browser cookie enrichment unavailable")
    return Response(status=204)

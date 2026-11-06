import ipaddress
import re
from urllib.parse import urlsplit

from flask import Blueprint, Response, jsonify, request

from config import META_ATTRIBUTION_ID, META_PIXEL_ID
from core.db import create_meta_click_attribution

meta_landing = Blueprint("meta_landing", __name__)
TELEGRAM_BOT_USERNAME = "rabahcoupons1bot"
_TELEGRAM_START_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
)
_META_COOKIE_PATTERN = re.compile(r"^fb\.\d+\.\d+\.[A-Za-z0-9._-]+$")


def _meta_cookie_value(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    if len(value) > 255 or not _META_COOKIE_PATTERN.fullmatch(value):
        return None
    return value


def _client_ip_address():
    forwarded_for = request.headers.get("X-Forwarded-For", "")
    candidates = forwarded_for.split(",") if forwarded_for else []
    if candidates:
        # The hosting ingress appends the connecting client as the rightmost
        # address; earlier values may have been supplied by the browser.
        candidate = candidates[-1].strip()
    else:
        candidate = request.remote_addr or ""
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return None


def _client_user_agent():
    value = request.headers.get("User-Agent", "").strip()
    if not value or len(value) > 1000 or any(ord(char) < 32 for char in value):
        return None
    return value


def _same_origin_request():
    origin = request.headers.get("Origin", "")
    parsed_origin = urlsplit(origin)
    return (
        parsed_origin.scheme in {"http", "https"}
        and bool(parsed_origin.netloc)
        and parsed_origin.netloc.lower() == request.host.lower()
    )


@meta_landing.get("/bot")
def bot_redirect():
    if (
        not META_PIXEL_ID.isdigit()
        or not META_ATTRIBUTION_ID
        or len(META_ATTRIBUTION_ID) > 64
        or any(char not in _TELEGRAM_START_CHARS for char in META_ATTRIBUTION_ID)
    ):
        return Response(
            "Meta Pixel and a valid Telegram attribution ID must be configured.",
            status=503,
            mimetype="text/plain",
        )

    telegram_start_url = f"https://t.me/{TELEGRAM_BOT_USERNAME}?start="
    telegram_url = f"{telegram_start_url}{META_ATTRIBUTION_ID}"
    page = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="robots" content="noindex, nofollow">
  <meta name="viewport" content="width=device-width, initial-scale=1">
</head>
<body>
<script>
!function(f,b,e,v,n,t,s)
{{if(f.fbq)return;n=f.fbq=function(){{n.callMethod?
n.callMethod.apply(n,arguments):n.queue.push(arguments)}};
if(!f._fbq)f._fbq=n;n.push=n;n.loaded=!0;n.version='2.0';
n.queue=[];t=b.createElement(e);t.async=!0;t.src=v;
s=b.getElementsByTagName(e)[0];s.parentNode.insertBefore(t,s)}}
(window,document,'script','https://connect.facebook.net/en_US/fbevents.js');
fbq('init', '{META_PIXEL_ID}');
fbq('track', 'PageView');
fbq('trackCustom', 'TelegramClick');
(function() {{
  const fallbackUrl = {telegram_url!r};
  const telegramStartUrl = {telegram_start_url!r};
  let redirectUrl = fallbackUrl;

  function readMetaCookie(name) {{
    const prefix = name + '=';
    const entry = document.cookie.split(';').map(function(value) {{
      return value.trim();
    }}).find(function(value) {{
      return value.indexOf(prefix) === 0;
    }});
    return entry ? entry.slice(prefix.length) : '';
  }}

  function waitForMetaCookies() {{
    return new Promise(function(resolve) {{
      const deadline = Date.now() + 450;
      function poll() {{
        const values = {{
          fbp: readMetaCookie('_fbp'),
          fbc: readMetaCookie('_fbc')
        }};
        if (values.fbp || values.fbc || Date.now() >= deadline) {{
          resolve(values);
        }} else {{
          window.setTimeout(poll, 50);
        }}
      }}
      poll();
    }});
  }}

  waitForMetaCookies()
    .then(function(values) {{
      return fetch('/bot/attribution', {{
        method: 'POST',
        credentials: 'same-origin',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify(values)
      }});
    }})
    .then(function(response) {{
      if (!response.ok) throw new Error('Attribution request failed');
      return response.json();
    }})
    .then(function(result) {{
      const payload = result && result.start_payload;
      if (typeof payload === 'string'
          && /^[A-Za-z0-9_-]{{1,64}}$/.test(payload)) {{
        redirectUrl = telegramStartUrl + encodeURIComponent(payload);
      }}
    }})
    .catch(function() {{}});

  window.setTimeout(function() {{
    window.location.replace(redirectUrl);
  }}, 800);
}})();
</script>
</body>
</html>"""
    return Response(
        page,
        status=200,
        mimetype="text/html",
        headers={
            "Cache-Control": "no-store",
            "X-Robots-Tag": "noindex, nofollow",
        },
    )


@meta_landing.post("/bot/attribution")
def create_bot_attribution():
    if (
        not META_PIXEL_ID.isdigit()
        or not META_ATTRIBUTION_ID
        or len(META_ATTRIBUTION_ID) > 64
        or any(char not in _TELEGRAM_START_CHARS for char in META_ATTRIBUTION_ID)
    ):
        return Response(
            "Meta Pixel and a valid Telegram attribution ID must be configured.",
            status=503,
            mimetype="text/plain",
        )
    if not _same_origin_request():
        return jsonify({"error": "Invalid origin"}), 403

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "Expected JSON"}), 400

    fbp = _meta_cookie_value(payload.get("fbp"))
    fbc = _meta_cookie_value(payload.get("fbc"))
    start_payload = create_meta_click_attribution(
        META_ATTRIBUTION_ID,
        fbp=fbp,
        fbc=fbc,
        client_ip_address=_client_ip_address(),
        client_user_agent=_client_user_agent(),
    )

    response = jsonify({
        "start_payload": start_payload or META_ATTRIBUTION_ID,
    })
    response.headers["Cache-Control"] = "no-store"
    return response

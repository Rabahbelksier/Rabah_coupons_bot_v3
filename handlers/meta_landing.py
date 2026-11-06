from flask import Blueprint, Response

from config import META_ATTRIBUTION_ID, META_PIXEL_ID

meta_landing = Blueprint("meta_landing", __name__)
TELEGRAM_BOT_USERNAME = "rabahcoupons1bot"
_TELEGRAM_START_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
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

    telegram_url = (
        f"https://t.me/{TELEGRAM_BOT_USERNAME}"
        f"?start={META_ATTRIBUTION_ID}"
    )
    page = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="robots" content="noindex, nofollow">
  <meta name="viewport" content="width=device-width, initial-scale=1">
</head>
<body>
<script>
window.setTimeout(function () {{
  window.location.replace({telegram_url!r});
}}, 800);
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

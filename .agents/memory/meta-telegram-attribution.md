---
name: Meta Telegram attribution
description: Rules for the Telegram bot's campaign-source attribution and Meta events.
---

Only attribute and report TelegramBotStart when the incoming start payload resolves to the configured campaign code. Save the source and queue one event only when it differs from the user's current stored source; a matching existing source must not create a duplicate. The bot-hosted `/bot` landing page retains PageView and TelegramClick and may bridge available `fbp`/`fbc`, client IP, and browser User-Agent through a short-lived opaque start token; never put these values directly in the Telegram URL.

**Why:** the user wants reliable ad-campaign attribution and better browser-to-server matching without counting ordinary starts or repeated starts as new subscriptions.

**How to apply:** keep the campaign code, Pixel ID, and CAPI token in environment configuration/secrets; optionally set a Meta test-event code only during verification, then unset it for normal delivery. Keep matching data short-lived and server-side; send it only when present and permitted by the site's privacy/consent policy. When behind the Railway ingress, use only the rightmost forwarded client IP and validate it; do not trust earlier forwarded values. Do not hardcode these values or alter ad posts/production destinations without a separate request.

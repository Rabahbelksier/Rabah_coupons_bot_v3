---
name: Meta Telegram attribution
description: Rules for the Telegram bot's campaign-source attribution and Meta events.
---

Only attribute and report TelegramBotStart when the incoming start payload exactly matches the configured campaign code. Save the source and queue one event only when it differs from the user's current stored source; a matching existing source must not create a duplicate. The bot-hosted `/bot` landing page retains the website's browser-side PageView and TelegramClick events, while TelegramBotStart is sent server-side.

**Why:** the user wants reliable ad-campaign attribution without counting ordinary bot starts or repeated starts as new subscriptions.

**How to apply:** keep the campaign code, Pixel ID, and CAPI token in environment configuration/secrets; do not hardcode them or alter ad posts/production destinations without a separate request.

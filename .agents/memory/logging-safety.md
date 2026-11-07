---
name: Logging safety
description: Preserve bot behavior and attribution privacy when extending diagnostics.
---

Logging improvements must leave user-facing bot messages, retries, fallbacks, and feature behavior unchanged.

**Why:** the user explicitly requested comprehensive diagnostic logs without breaking or changing any existing function, and previously emphasized keeping bot error messages as they are.

**How to apply:** observe failures rather than handling them differently. Never log raw Telegram updates, private message text, AI prompts, credentials, or the token-bearing webhook path; pattern redaction is defense in depth, not permission to log arbitrary payloads. Telegram can consume handler exceptions internally, so a completed update-processing future alone is not evidence of successful handling or message delivery.

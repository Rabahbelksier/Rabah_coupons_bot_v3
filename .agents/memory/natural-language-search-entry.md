---
name: Natural-language search entry
description: Product decision for routing Telegram search requests independently of process-local session state.
---

Treat the “ask AI” button as the explicit gate for AI search. Natural-language product-search requests must remain on the normal bot path until the user activates that mode.

**Why:** The product requirement is to preserve the older, button-driven interaction and avoid interpreting ordinary messages as AI intent.

**How to apply:** Preserve higher-priority admin, smart-cart, help, and AliExpress-link paths. Call the AI search handler only when `ai_search_active` is true; the reply-keyboard and inline buttons are the only activation paths.
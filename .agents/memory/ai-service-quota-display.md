---
name: AI service quota display
description: Durable rules for presenting provider availability and quota usage in the admin Telegram message.
---

The admin AI-service details message must stay compact: each provider/model starts with 🟢 when configured, enabled, and available, otherwise 🔴; quota limits are shown separately from DeepSeek's observed daily request/token counts.

**Why:** The administrator needs a readable operational snapshot rather than tracebacks or provider capability metadata. DeepSeek's daily counters are usage statistics, not quotas, and must never gate provider calls.

**How to apply:** Keep Gemini, Groq, and OpenRouter request/token fields aligned with the configured display set. Cloudflare Workers AI displays Neurons only. Learned limits are approximate and must be marked with ≈; never add extra Cloudflare quota fields just because the generic rate-limit code supports them. DeepSeek's observed daily counts reset at fixed UTC+1 midnight and stay separate from provider quota windows. Other daily quota windows use UTC except Gemini's Pacific midnight; Groq's return countdown is authoritative only after `x-ratelimit-reset-*` headers arrive, otherwise show it as unknown rather than assuming UTC midnight.
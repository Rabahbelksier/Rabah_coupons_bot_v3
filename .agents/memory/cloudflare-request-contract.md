---
name: Cloudflare Workers AI request contract
description: Verified request details for the Cloudflare Qwen model used by channel analysis.
---

For Cloudflare's current Qwen 3.8 27B model, the model identifier `@cf/qwen/qwen3.8-27b` is valid. When disabling reasoning, send `enable_thinking` inside the nested `chat_template_kwargs` object.

**Why:** Cloudflare documents the reasoning option as part of the chat template configuration. Sending it at the top level can make an otherwise valid OpenAI-compatible request fail or be ignored, leaving channel posts pending after failover.

**How to apply:** Keep the account endpoint at `/accounts/<32-hex-account-id>/ai/v1/chat/completions`, preserve the documented model override, and verify nested request options against the live model schema when Cloudflare changes its catalog.

Cloudflare's OpenAI-compatible API can return its failure details in a top-level `errors` array rather than OpenAI's singular `error` object. Error code `4006` with a daily free allocation message means the 10,000-neuron daily allocation is exhausted and the next reset is midnight UTC.

**Why:** Treating that envelope as an empty error caused the bot to retry a permanently exhausted daily quota after a short recovery interval and obscured the provider's actual diagnosis.

**How to apply:** Normalize both error shapes before classifying responses; map daily neuron allocation failures to `npd`, preserve the configured display limit, persist zero remaining, and quarantine until the next UTC day.
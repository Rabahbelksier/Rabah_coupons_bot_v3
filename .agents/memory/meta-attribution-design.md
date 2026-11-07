---
name: Meta attribution design
description: User-approved distinction between fixed campaign source and per-visit Telegram attribution.
---

The redirect must use exactly `https://t.me/rabahcoupons1bot?start={META_ATTRIBUTION_ID}`, substituting the configured campaign source. Do not replace that source with a per-visit token. Save a received source even when it differs from the configured campaign; matching controls event eligibility, not whether the source may be stored.

**Why:** The user explicitly corrected the earlier per-visit-token approach and repeated that arbitrary start sources must be stored, but only a matching campaign source can generate a conversion.

**How to apply:** Preserve the literal campaign-source deep link in redirect changes. A fixed shared payload cannot reliably identify the originating browser visit; never infer that link from IP or timing. Suppress a conversion when the matching campaign source is already saved for that user, and retain durable event deduplication. Do not change existing bot posts or messages as part of this work.

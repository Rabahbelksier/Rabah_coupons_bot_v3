---
name: Meta attribution design
description: User-required literal campaign links, source saving for existing users, and conversion deduplication.
---

The redirect must use exactly `https://t.me/rabahcoupons1bot?start={META_ATTRIBUTION_ID}`, substituting the configured campaign source. Do not replace that source with a per-visit token. Save a received source even when it differs from the configured campaign; matching controls event eligibility, not whether the source may be stored.

**Why:** The user explicitly corrected the earlier per-visit-token approach and repeated that arbitrary start sources must be stored, but only a matching campaign source can generate a conversion.

Save a received source for existing users too, even if their previous source is empty or different. The user clarified: “نحفظ المصدر ايا كان ونرسل الحدث ان كان مطابقا لMETA_ATTRIBUTION_ID”. This supersedes the earlier restriction to brand-new users.

**Why:** The user explicitly confirmed this rule when asked about existing users with an empty source.

**How to apply:** Preserve the literal campaign-source deep link in redirect changes. A fixed shared payload cannot reliably identify the originating browser visit; never infer that link from IP or timing. Compare the received source with configuration and check the previously saved value before updating it; suppress a conversion if the campaign source was already saved, and retain durable event deduplication. Plain starts without a source must not erase it. Do not change existing bot posts or messages as part of this work.

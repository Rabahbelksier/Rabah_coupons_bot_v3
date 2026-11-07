---
name: Automatic publisher limits
description: User-defined repetition thresholds, offer selection priority, and daily cap for automatic channel publication.
---

Automatic channel publication triggers at cumulative product thresholds 5, 10, 15, and so on. Choose the newest source post regardless of its price. A selected source offer may be published at most twice per database calendar day. If another due threshold is blocked by that cap, record it as skipped so it is not replayed the next day; continue at the next 5-item threshold.

**Why:** The user explicitly requested newest-first selection regardless of price, plus this threshold and daily-cap behavior.

Channel analysis batch sizing is separate from publication thresholds and from
the number of offer links generated for a manually submitted product.

**Why:** The user requested smaller channel-analysis/link-processing batches
while requiring the rest of the bot's existing behavior to remain unchanged.

**How to apply:** Do not treat unrelated five-item publishing thresholds or
manual offer counts as channel-analysis batch settings when changing ingestion.

**How to apply:** Preserve threshold-level idempotency. Enforce the cap using persisted delivery records per selected source offer, count active claims as reservations, and keep skipped thresholds terminal. Persist a sending state before calling Telegram; ambiguous send outcomes must not be retried automatically. The report uses the database timezone for day boundaries.

Automatic publishing must not retry a Telegram send when the request outcome is ambiguous. Persist the send boundary before making the request, and mark uncertain outcomes terminal rather than retryable.

**Why:** Telegram may accept a post even when the client loses the response; retrying such a request can publish an unintended duplicate.

**How to apply:** Keep uncertain results visible to administrators and excluded from automatic retries. Failures known to happen before delivery may remain retryable, while ancillary text failures after a successful photo must not reset the completed delivery.

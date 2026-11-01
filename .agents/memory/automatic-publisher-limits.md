---
name: Automatic publisher limits
description: User-defined repetition thresholds, offer selection priority, and daily cap for automatic channel publication.
---

Automatic channel publication triggers at cumulative product thresholds 5, 10, 15, and so on. Choose the newest source post regardless of its price. A selected source offer may be published at most twice per database calendar day. If another due threshold is blocked by that cap, record it as skipped so it is not replayed the next day; continue at the next 5-item threshold.

**Why:** The user explicitly requested newest-first selection regardless of price, plus this threshold and daily-cap behavior.

**How to apply:** Preserve threshold-level idempotency. Enforce the cap using persisted delivery records per selected source offer, count active claims as reservations, and keep skipped thresholds terminal. The existing report uses the database timezone for day boundaries.

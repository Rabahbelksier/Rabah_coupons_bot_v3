---
name: Telegram source retry state
description: Durable distinction between deleted Telegram messages and recoverable retry failures.
---

A Telegram retry record must leave the retry queue only when the source message is confirmed unavailable. A missing response or an explicit invalid-message error can receive a terminal missing-source status; connection, permission, and other unexpected errors must remain retryable.

**Why:** Treating every fetch failure as deletion can lose valid posts during temporary Telegram or session problems, while leaving confirmed deletions pending blocks later batches indefinitely.

**How to apply:** Keep source-availability classification in the retry worker, and make the database transition conditional on the record still being in a retryable processing state.
---
name: AI request priority
description: Admission policy for user search requests and Telegram channel analysis.
---

User search requests must be admitted ahead of channel analyses that have not started, but an analysis that has already acquired its AI slot must not be cancelled or interrupted.

**Why:** A slow channel post should not make a new user wait, while cancelling in-flight Telegram work can leave database rows and source-post processing inconsistent.

**How to apply:** Keep priority at the async admission boundary, keep external calls outside broad global locks, and use a bounded user timeout with the local parser as the response fallback. Do not send a partial offer just to reduce latency.
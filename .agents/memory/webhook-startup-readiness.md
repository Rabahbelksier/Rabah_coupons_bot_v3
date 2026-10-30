---
name: Webhook startup readiness
description: Startup ordering required to prevent a slow first Telegram response after deployment.
---

The Telegram application must be initialized and started before the web server begins accepting webhook requests. Optional setup such as command-menu configuration and channel monitoring should run asynchronously after core readiness.

**Why:** Deferring initialization until the first webhook makes the first user wait for Telegram setup and potentially slow secondary services after every deployment.

**How to apply:** Keep the readiness guard as a fallback, but invoke it during process startup. Do not add database maintenance, channel synchronization, or optional network setup to the synchronous first-update path.
---
name: AI provider task scoping
description: Boundaries between channel-analysis routing and user-request routing.
---

Separate provider priority and admin enablement by request task, but keep quota exhaustion, recovery, rate-window accounting, and usage totals shared per provider.

**Why:** Provider limits and billing apply to the provider account, not to the feature that made a request; duplicating them per task could exceed real limits or underreport usage.

**How to apply:** Route channel analysis and user requests through their own orders and enabled flags, while keeping provider health/quota state and aggregate usage keyed only by provider. User requests alone may use the local fallback.
---
name: User activity history
description: Historical limits and reporting rules for user activity and first-use dates.
---

Use daily per-user aggregates for activity ranges beyond the existing current-day and rolling month counters. Statistics before daily tracking began cannot be reconstructed from the old counters; show that limitation instead of presenting partial totals as complete history. The user's legacy `first_used_at` values were backfilled to 2025-01-01 by request; treat that date as a placeholder, not a verified first-use time.

**Why:** The previous user table retained only daily/current-month/previous-month counters and no registration timestamp, so older detailed history is unavailable. The user chose a shared fallback date for legacy rows to replace nulls.

**How to apply:** When adding longer time ranges or exposing first-use dates, use the daily activity records for measurable history. Do not interpret 2025-01-01 on legacy rows as their actual first-use date.

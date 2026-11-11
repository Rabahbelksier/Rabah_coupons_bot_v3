---
name: User activity history
description: Historical limits and reporting rules for user activity and first-use dates.
---

Use daily per-user aggregates for activity ranges beyond the existing current-day and rolling month counters. Statistics before daily tracking began cannot be reconstructed from the old counters; show that limitation instead of presenting partial totals as complete history. Existing users also have no recoverable first-use time and should keep a null timestamp rather than receive a fabricated migration date.

**Why:** The previous user table retained only daily/current-month/previous-month counters and no registration timestamp, so older detailed history is unavailable.

**How to apply:** When adding longer time ranges or exposing first-use dates, use the daily activity records for measurable history and leave legacy timestamps unknown unless a trustworthy source is introduced.

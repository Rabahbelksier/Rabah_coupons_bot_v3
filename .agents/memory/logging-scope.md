---
name: Logging improvement boundaries
description: User constraints for Railway diagnostics without changing bot behavior.
---

Server logging improvements must preserve all existing bot function logic and
all user-visible messages, including error messages and administrator reports.
The user runs the bot on Railway.

**Why:** The user repeatedly required no changed or added bot messages and no
broken existing functions while improving diagnostic readability.

**How to apply:** Keep new diagnostic events console-only, separate from the
existing database-backed admin error feed. Do not change retry policies,
exception propagation, return values, or business workflows as part of logging.
Use isolated verification rather than starting another copy against the live
database or Telegram session.

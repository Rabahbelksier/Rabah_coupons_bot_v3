---
name: Telegram reply-keyboard routing
description: Saved-offer reply buttons need to remain distinct from manually typed AI search text.
---

Saved-offer menus must use reply keyboards below the composer. Add an invisible U+2063 suffix to each saved-offer button label and route only marked text to the saved filter; unmarked text that matches a label remains ordinary AI search input while AI mode is active. Keep legacy inline callback routing for menus already sent, and treat marked taps from expired sessions as stale rather than starting a search. For AI-entry and saved-offer reply keyboards, leave both `is_persistent` and `one_time_keyboard` unset: persistence can defeat Telegram's local hide action, while one-time keyboards disappear after a normal selection.

**Why:** The requested interaction is a reply keyboard, while Telegram otherwise sends the same label for a button tap and manually typed text. The hidden suffix preserves the visible label and lets the handler distinguish the two without changing search parsing. Users also need Telegram's own back/icon controls to hide and restore the keyboard without the bot clearing state or forcing the keyboard open.

**How to apply:** Keep saved-offer selection in the reply-keyboard path, omit `is_persistent` and `one_time_keyboard`, and test both values remain unset. Attach the AI entry keyboard to a durable anchor before queued AliExpress work starts; don't reattach it to temporary loading/error messages or after the queue finishes, so a local hide during processing isn't undone.
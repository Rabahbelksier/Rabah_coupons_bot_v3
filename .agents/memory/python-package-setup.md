---
name: Python package setup
description: Workspace side effects to watch for when installing Python dependencies.
---

Package installation may rewrite `requirements.txt` and reconcile workspace files as part of dependency setup. Installing `python-telegram-bot` through the package helper can also install the unrelated `telegram==0.0.1` distribution, which overwrites the shared `telegram/__init__.py`; uninstalling that package can then remove the import initializer needed by python-telegram-bot.

**Why:** Package names and Python import names are not always interchangeable. An automated import-name resolution installed a conflicting Telegram distribution, rewrote the dependency file, and left the runtime import broken even though python-telegram-bot remained installed.

**How to apply:** Prefer completing package setup before editing application code. After any package operation, inspect `git status` and dependency diffs, verify the import used by the app, and confirm the intended source edits are still present. Do not assume uninstalling an import-name package repairs a distribution that shares its files.
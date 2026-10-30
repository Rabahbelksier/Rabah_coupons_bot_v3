---
name: Provenance column compatibility
description: New nullable provenance fields need schema migration and legacy read/write fallbacks.
---

When adding provenance columns to channel-post data, keep the field nullable, add it during startup schema initialization, and let legacy reads and writes continue without that field.

**Why:** Imported databases may be missing newer provenance columns even when the bot must continue saving the existing classification and post data.

**How to apply:** Extend the allow-listed update fields and only populate the new source when the provider identity is available; never make absence of the new column block the existing processing path.
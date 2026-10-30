---
name: AI quota maintenance
description: Boundary between local quota-window maintenance and provider-reported rate-limit metadata.
---

Quota-window maintenance must update local counters and availability without making synthetic provider requests. Provider-specific windows, especially Groq's moving windows, should only be replaced by metadata from real provider responses.

**Why:** A background reset must not consume quota or invent a recovery time that differs from the provider's actual window.

**How to apply:** Keep the background worker behind the existing service lock, let it refresh persisted local state at known timestamps, and leave unknown provider windows unset until an actual response supplies them.
---
name: AI provider failover state
description: Rules for preserving correct provider routing and retry behavior across repeated and concurrent AI requests.
---

Persistently quarantine a provider only for quota exhaustion or configuration failures. Network, malformed-response, and other request-scoped failures may use the next provider for that request, but must remain eligible on the next request.

Treat external failure as retryable for channel processing whenever any provider has a scheduled quota reset, even if other providers failed configuration or transiently. Keep this separate from the stricter “all configured providers exhausted quota” classification.

**Why:** Advancing a global provider index after a transient failure can permanently skip a healthy primary provider. Requiring every provider failure to be quota-related can also mark a channel post permanently failed even though a higher-priority provider will recover shortly.

**How to apply:** Derive the active provider from the first provider not persistently quarantined, serialize routing state transitions, calculate exhaustion type from persisted provider state, and preserve channel work when any finite quota recovery is scheduled.

Channel analysis must also be gated before claiming or sending a `processing` batch when all external providers are locally quarantined with a scheduled recovery. Leave those rows queued until recovery instead of repeatedly re-submitting them.

**Why:** A retry delay alone is insufficient when another entry point can drain the queue immediately; new channel events and the retry loop must share the same admission guard.

**How to apply:** Check the AI availability status before acquiring the channel-processing lock, and only allow the batch once external mode is available again.

Retry-loop backoff must use a provider recovery timestamp only when the aggregate AI mode is `local`; an available external provider must not make channel work wait for another provider's recovery.

**Why:** A scheduled recovery for one provider previously introduced multi-minute channel gaps even while another external provider was successfully serving channel requests.

**How to apply:** Keep the local-mode defer guard, but use the normal retry interval whenever `get_ai_service_status()` reports `external`.
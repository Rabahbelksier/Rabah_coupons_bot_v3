---
name: Gemini keyword cache
description: Durable rules for synchronizing offer-title keywords into Gemini CachedContent.
---

Rebuild the local phrase snapshot from every processed offer title at server startup, off the Telegram readiness path. Attempt Gemini CachedContent sync after that rebuild and when Gemini is selected for a user offer search. Reuse an unchanged, unexpired cache indefinitely; after a snapshot change, wait for the configured sync interval before creating a replacement, but never attach CachedContent whose snapshot differs. Inline the current phrases during that cooldown. Never replace saved cache state until CachedContent creation succeeds; if creation or persistence fails, keep the uncached request constrained to the active phrase list.

**Why:** The phrase list changes independently of request traffic, and creating a cache on every request or timer wastes Gemini quota. Reusing an old CachedContent after a snapshot change makes its vocabulary stale; inline current phrases preserve correctness without bypassing quota throttling.

**How to apply:** Keep snapshot phrases and Gemini cache state separate, atomically replace local phrases on startup and renew them for 48 hours, pass cache context only to Gemini, and leave Groq, OpenRouter, Cloudflare, channel analysis, and administrative requests on their existing paths.

Store newly extracted phrases with the original title wording, spacing, capitalization, and meaningful punctuation; use tokenized text only for validation and deduplication. Keep legacy underscore-joined rows compatible during matching and render a phrase as legacy only when it consists entirely of underscore-joined word tokens. Preserve title-originated punctuation and hyphens. Fingerprint the exact Gemini-facing phrase text with a format-version prefix so a formatting change invalidates old CachedContent.

**Why:** Synthetic separators make the cached phrase differ from the title and can damage model names. Legacy rows still need to work during the transition, while an unexpired Gemini resource may contain an obsolete display format.

**How to apply:** Make single-title upserts and startup rebuilds store source phrase candidates. Use the same legacy-aware formatter for CachedContent and inline fallback, and for the cache fingerprint. Persist the new state only after Gemini successfully creates the replacement.

For user product searches first parsed by Groq, OpenRouter, or Cloudflare, preserve that parser's intent and filters, then use a Gemini-only secondary request to select cached product phrases. Restrict the selection to the exact keyword snapshot used by that Gemini request; on failure, retain only exact cache matches from the first parser.

**Why:** Semantic search must use the closed phrase vocabulary regardless of which provider handles intent parsing, without changing the shared provider order or overwriting price, shipping, category, and other filters.

**How to apply:** Keep this extra request limited to product-term resolution for user searches. Do not route channel or admin analysis through the cache or move the default provider chain.

Official second-level AliExpress category names use a separately versioned Gemini cache state and cache resource. Gemini accepts only one `cachedContent` resource per generation request, so keep phrase-based intent parsing unchanged and resolve explicit category filters in a separate Gemini-only user request. Validate returned names against the canonical local list, pass them through `official_category` or `official_categories`, and leave all other normalized filters intact. Never add official categories for a request that did not explicitly ask for a category filter.

**Why:** Attaching both phrase and category resources to one request is not supported by the Gemini API. A separate category stage allows exact, OR-ed official category filters without displacing the product vocabulary or changing other providers.

**How to apply:** Store the category resource under a distinct cache-state key, refresh it using the same snapshot/model/expiry safeguards, and keep the extra request restricted to explicit user category filters.

When an explicit `official_categories` list is present, treat its names as OR alternatives and do not AND them with a single internal category inferred by the primary parser. Keep price, shipping, specifications, and offer-type filters conjunctive.

**Why:** The primary parser may choose one broad internal family for a request that the category stage correctly resolves to alternatives from different families; applying both silently removes valid results.

**How to apply:** Let the canonical official-category list own the category constraint after resolution, while leaving all non-category filters unchanged.
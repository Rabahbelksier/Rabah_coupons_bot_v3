---
name: Channel shipping-country rule
description: User requirement for assigning shipping destinations from Telegram product posts.
---

If a country is explicitly named anywhere in a Telegram product post, classify the product as shipping to that country, even when the country is not mentioned in shipping context. Never infer a country absent from the source post. If no country is named, keep `shipping_country` null and omit generic shipping lines from the generated basic post.

**Why:** The user explicitly wants country mentions—not shipping-related wording—to be the deciding evidence for shipping-country classification.

**How to apply:** Keep provider instructions and deterministic validation aligned around country mentions in the full source post, including common Arabic prepositions attached to the name. Add every source line naming a validated country to the shipping lines rendered in the template, even without shipping wording; preserve existing country normalization and database persistence.

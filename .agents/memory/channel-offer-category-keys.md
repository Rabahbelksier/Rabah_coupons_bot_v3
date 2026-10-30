---
name: Channel-offer category keys
description: Separation between internal AI category keys and official AliExpress taxonomy on channel offers.
---

For channel-ingested offers, `category` is the bot's internal category label. Dedicated saved-offer filters match the exact internal keys `bluetooth_headphones` and `smart_watches`; preserve those labels when title evidence supports them, even if the provider first returns `other` or AliExpress maps its official subcategory to `other`. Keep AliExpress's first- and second-level names in their separate fields. A conflicting, recognized AliExpress family remains a guard against an obviously inconsistent AI special label.

**Why:** The saved-offer buttons depend on the internal special keys, while the two taxonomy columns retain AliExpress's official hierarchy. An unmapped API category should not erase an explicit Bluetooth-headphone or smart-watch classification.

**How to apply:** Normalize a clear special product title even when the model category is `other`; keep the official API names unchanged, preserve special keys when the API family is compatible or unmapped, and test both saved-offer keys through persistence and retrieval.

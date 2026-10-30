---
name: Telegram media delivery
description: Durable constraints for sending product images and long smart-cart content through Telegram.
---

Telegram photo captions are limited to 1024 characters and text messages to 4096 characters, so long content cannot always remain in one message.

**Why:** A caption that exceeds the Telegram limit causes the photo send to fail even when the image itself is valid; silently falling back to text removes the expected image.

**How to apply:** Prefer a Telegram `file_id` captured from a successfully sent photo over re-fetching a remote URL. For oversized captions, send the image with a short caption and the complete content (including reply markup) as the following text message. For long text reports, split at section boundaries when possible and keep reply markup on the first chunk.
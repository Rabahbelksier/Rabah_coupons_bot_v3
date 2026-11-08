---
name: Meta attribution design
description: User-approved visit-token experiment, source saving for existing users, and conversion deduplication.
---

The user approved implementing the random visit-token approach to try linking browser matching data to a Telegram start. This supersedes their earlier requirement to put the campaign source itself in every redirect URL. The campaign source, not the random token, must still be saved as the user's source; direct source links must continue working.

**Why:** After discussing that browser information alone cannot identify a Telegram account, the user explicitly approved visit-token links and reaffirmed that META_ATTRIBUTION_ID must remain the original stored campaign source.

Save a received source for existing users too, even if their previous source is empty or different. The user clarified: “نحفظ المصدر ايا كان ونرسل الحدث ان كان مطابقا لMETA_ATTRIBUTION_ID”. This supersedes the earlier restriction to brand-new users.

**Why:** The user explicitly confirmed this rule when asked about existing users with an empty source.

Use a visible subscription button on `/bot`, not an automatic redirect. PageView belongs to page display, TelegramClick to actual button clicks, and TelegramBotStart to a qualifying received start command.

**Why:** The user replaced their earlier automatic-redirect preference with an explicit request for a button and this event timing after observing missing start parameters. They subsequently confirmed that this restored start delivery and campaign-source saving.

Basic bot-user registration must remain independent of optional Meta persistence failures. Preserve atomic source/event capture, but allow it to retry later for an existing user whose campaign source was not saved.

**Why:** The user reported a missing user row after manually pressing Start. Independent fault tests confirmed that coupling optional event persistence to the basic insert could discard registration. This does not establish the native Telegram delivery root cause.

**How to apply:** Link browser data only through the exact visit token, never by guessing from IP or timing. Compare the resolved or direct source with configuration and check the previously saved value before updating it; suppress a conversion if the campaign source was already saved, and retain durable event deduplication. Plain starts without a source must not erase it. Do not change existing bot posts or messages as part of this work.

Meta API acceptance and visibility in the Test Events interface are separate checks. An accepted chat-source diagnostic did not appear for the user; after a paired chat/website diagnostic, the user reported seeing TelegramBotStart in the website view, without identifying which diagnostic event ID appeared.

**Why:** Successful API responses alone led to an incomplete explanation of the user's missing test event.

**How to apply:** Verify the displayed event ID before attributing visibility to an action-source change. Keep the source accurate to where the actual conversion occurs; do not relabel a Telegram interaction as a website interaction merely to make it visible in a website test view.

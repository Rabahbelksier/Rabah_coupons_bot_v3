# Meta attribution for Telegram

## What this integration does

The existing Flask server now serves `/bot`. The invisible page schedules a
redirect to `rabahcoupons1bot` after 800 ms, just like the previous site, and
queues only the browser events `PageView` and custom `TelegramClick`. Here
`TelegramClick` means the automatic outbound redirect; it does not prove the
Telegram application opened. Browser blockers, fast navigation and network
failures can prevent browser events from arriving.

Opening Telegram is **not** a subscription event. Only receipt of the user's
first eligible `/start` queues the custom server event `TelegramBotStart`.
Telegram may require the user to press **Start** first.

The redirect page does not change posts, bot replies, existing search, channel
monitoring, publishing or other website features.

## Configuration

All values belong in the deployment's environment/secret settings, not in code.
This feature does not change these settings automatically and does not publish
the application.

| Variable | Purpose |
| --- | --- |
| `META_TRACKING_ENABLED` | Defaults to `false`. Set `true` only after the configuration and privacy requirements below are satisfied. |
| `META_PIXEL_ID` | Numeric pixel/dataset ID used by both the page and the CAPI sender. |
| `META_ATTRIBUTION_ID` | Fixed campaign source, 1–64 characters from `A-Z a-z 0-9 _ -`. The prefix `mv_` is reserved for visit tokens and cannot be used here. |
| `META_CAPI_ACCESS_TOKEN` | Secret, server-only Conversions API token. Never put it in HTML or a URL. |
| `META_TEST_EVENT_CODE` | Optional Events Manager test code. Remove for real conversion delivery. |
| `META_GRAPH_API_VERSION` | Defaults to `v25.0`. Review against Meta's supported Graph API versions when upgrading. |
| `META_VISIT_TTL_SECONDS` | Visit retention and attribution window, defaults to 86,400 seconds; capped at seven days. |
| `META_TRUSTED_PROXY_HOPS` | Defaults to `0`, which ignores `X-Forwarded-For`. Set an exact trusted hop count only after confirming the deployment's proxy sanitizes/appends forwarding headers. |

Existing bot credentials and external PostgreSQL settings remain necessary.
Startup adds a nullable `meta_source_id` to `user_bot` and the separate tracking
tables. It does not replace the existing database or rewrite existing users.

If tracking is disabled or required configuration is invalid/missing, `/bot`
still opens the ordinary bot link, without Pixel calls or attribution.
If visit persistence fails while configuration is valid, the page falls back
to the direct campaign source link. This preserves the redirect but cannot
associate that subscription with the failed browser-visit record.

## Source and visit tokens

The environment source identifies the campaign, not an individual visitor.
Every tracked `/bot` load gets a random 35-character `mv_…` start token.
Only its SHA-256 digest is persisted, and it is a single-use bearer capability.
Do not share a visit's Telegram link between people; a new visitor should open
`/bot` to receive their own token.

The backend stores the source, pixel, expiry and available browser matching
data against that token. Once a new user starts the bot, a transaction:

1. Inserts the user only if absent.
2. Resolves and locks an unexpired, unused visit belonging to the current
   configured source **and** pixel, or accepts a direct start payload exactly
   equal to the configured source.
3. Saves the **fixed source**, not the token, in `user_bot.meta_source_id`.
4. Queues one conversion with a stable event ID and original Telegram message
   timestamp. A unique hashed user key also prevents a previously attributed
   user being counted again if their `user_bot` row was later removed.
5. Clears browser matching data from the consumed visit.

An old user, an organic first start, a different source, an expired/used token,
or a previous campaign's token cannot become a new conversion. A user whose
first start was organic cannot be reattributed by pressing Start later.
An existing user's ordinary `/start` behavior and messages stay unchanged.
If the registration transaction fails, it rolls back completely; it does not
insert an organic row as a fallback and silently lose the first-use attribution.
The bot still replies normally, and a later `/start` can retry registration.

For a direct campaign link use:

`https://t.me/rabahcoupons1bot?start=YOUR_CONFIGURED_SOURCE`

It produces only `TelegramBotStart` for an eligible new user, never page/click
events. Without a visit, no IP, browser or click data is invented.

## Matching data and privacy

The controller must provide an appropriate privacy notice and establish a
lawful basis and any required consent **before** enabling this integration.
An invisible redirect cannot obtain consent by itself. If consent is required
for your visitors, keep tracking disabled until your upstream flow obtains it
for every tracked visitor; do not enable it globally for mixed-consent traffic.
The existing notice must explain Meta sharing and retention.

The page also honors `Sec-GPC: 1`, `DNT: 1`, and the first-party cookie
`meta_consent=denied`: these requests redirect organically without collection,
Pixel initialization or a campaign start payload. Direct Telegram campaign
links do not carry browser consent state; enabling that campaign requires a
lawful basis for the bot event itself as well.

Collected matching data is limited to:

- Valid `_fbp` / `_fbc` browser identifiers when present.
- `fbc` created only when a real, syntactically valid `fbclid` is received.
- The request's IP and user agent. Forwarded IP headers are ignored unless
  trusted proxy hops are explicitly configured. Incorrect hop configuration
  can record an internal proxy address or trust spoofed addresses.
- A SHA-256-hashed, namespaced Telegram user ID as `external_id`. This is not
  sent as a Facebook login ID, and an unlinked Telegram ID by itself does not
  guarantee Meta can match a person.

The page sends same-origin, best-effort beacons before navigation to capture an
`_fbp` created by the asynchronous Pixel. If Pixel is blocked or loads too late,
this field can be absent. Email, telephone, Telegram display names, precise
location and invented demographic fields are not sent. Telegram display names
are not verified Facebook names. IP/user agent/fbp/fbc are not hashed; the
external ID is hashed as required for customer matching.

Unconsumed visits expire according to the configured window and are deleted
by hourly maintenance. A consumed visit immediately loses its browser data.
Delivery payloads are erased upon successful delivery or permanent failure,
and pending payloads expire after six days (before Meta's seven-day event-age
limit). Maintenance continues when tracking is disabled, provided the bot
process and database are available. Data cannot be physically purged while
the process is stopped; cleanup runs when it resumes.

Minimal delivery receipts retain the hashed user key, event ID, pixel ID,
delivery timestamps/status and sanitized numeric error codes to enforce
deduplication; they do not retain the browser payload. They are pseudonymous
data, not anonymous data. Include these receipts and `meta_source_id` in any
applicable data-access/deletion process. Existing bot user names/IDs remain
governed by the bot's existing storage policy.

## Delivery and failures

The sender runs outside the Telegram handler. New events are picked up within
approximately five seconds while the bot process is running. Database leases
and `SKIP LOCKED` permit multiple application workers without parallel sends of
the same leased event. Stale leases recover after two minutes; stale workers
cannot acknowledge another worker's lease.

Transport failures, rate limits, server errors and documented transient error
codes are retried with capped exponential backoff (at most 20 attempts and six
days). Permanent validation/authentication failures are marked failed, with
the customer payload erased. Fix the configuration for future events; this
implementation does not provide an admin replay of terminally failed events.
Do not treat a successful HTTP status alone as acceptance: the response must
report `events_received: 1`.

Retries preserve `event_id` and `event_time`, allowing Meta to deduplicate
repeated delivery. This is at-least-once delivery, not a guarantee of exactly
one request or unlimited-duration Meta deduplication. Destination pixel and
test-event code are pinned when queued; changing them never silently reroutes
old conversions. The current access token must retain permission for any
queued destination.

Database/provider exception text, response bodies, tokens and raw matching data
are not logged by these modules. Ensure infrastructure/access-log retention
also protects query parameters such as `fbclid`.

`TelegramBotStart` uses `action_source: "chat"` because it happens inside a
messaging app. It is not falsely labelled as a website purchase, and this is
not Meta's special Messenger/WhatsApp business-messaging API.

## Safe testing and rollout

1. Configure the existing bot normally and add the Meta variables with tracking
   still disabled. Confirm privacy and the deployment's forwarded-header
   boundary. Never reuse a Meta test token as a live secret.
2. Obtain `META_TEST_EVENT_CODE` from Events Manager → Test Events. Configure it
   before enabling tracking; the code is pinned in newly queued events.
3. Enable tracking and restart the bot. Open the **bot server's** `/bot` URL
   using a real test ad click if `fbclid` matching is being checked. Do not
   fabricate `fbclid`. In the browser, verify `PageView` and `TelegramClick`
   with Meta's browser tools; these browser events do not use the CAPI test code.
4. Use a genuinely new test Telegram account to press Start. Verify the custom
   `TelegramBotStart` appears under Test Events, with `action_source=chat`.
   An existing account is intentionally ineligible. Do not delete real users
   to manufacture first-use eligibility.
5. Repeat Start, test a direct campaign link with another new test account, and
   test organic and mismatched links. Confirm conversion counts do not increase
   for excluded cases. Inspect errors without copying secrets/customer data.
6. Remove the test-event code for future real events and restart. Do not remove
   it hoping already queued test events will turn into production events.
7. Only after successful verification, replace the advertising destination with
   the bot server's `/bot` URL. This task does not modify ads, publish, shut down
   the old website or invent the bot server's production URL.

Automated verification:

```sh
python -m pytest -q tests/test_meta_tracking.py tests/test_help_start.py
```

The tracking tests stub all Meta calls and use an isolated temporary local
PostgreSQL instance for schema idempotency, concurrency, transactions, leases
and retention. They never connect to the application's external database.
Those database cases are skipped if local PostgreSQL binaries are unavailable.
For a full regression run, use `python -m pytest -q` only in a dedicated test
environment: some pre-existing channel-monitor tests use the application's
configured database for advisory-lock checks. The existing taxonomy reference
test also requires its original uploaded reference file to be available.

References checked during implementation:

- [Meta server event parameters](https://developers.facebook.com/documentation/ads-commerce/conversions-api/parameters/server-event)
- [Meta customer information parameters](https://developers.facebook.com/documentation/ads-commerce/conversions-api/parameters/customer-information-parameters)
- [Meta action-source definitions](https://developers.facebook.com/documentation/ads-commerce/gateway-products/signals-gateway/custom-data-destination)
- [Telegram deep linking](https://core.telegram.org/bots/features#deep-linking)

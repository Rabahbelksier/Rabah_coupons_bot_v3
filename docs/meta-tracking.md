# Meta attribution for Telegram

## What this integration does

The existing Flask server serves `/bot` as an Arabic subscription page with a
real Telegram button. It never redirects automatically. `PageView` is queued
only when the page loads; custom `TelegramClick` is queued only when the button
is clicked. A click does not prove the Telegram application opened. Browser
blockers, fast navigation and network failures can prevent browser events
from arriving.

Opening Telegram is **not** a subscription event. Receipt of an eligible
campaign `/start` queues the custom server event `TelegramBotStart`.
Telegram may require the user to press **Start** first.

The subscription page does not change posts, bot replies, existing search, channel
monitoring, publishing or other website features.

## Configuration

All values belong in the deployment's environment/secret settings, not in code.
This feature does not change these settings automatically and does not publish
the application.

| Variable | Purpose |
| --- | --- |
| `META_TRACKING_ENABLED` | Defaults to `false`. Set `true` only after the configuration and privacy requirements below are satisfied. |
| `META_PIXEL_ID` | Numeric pixel/dataset ID used by both the page and the CAPI sender. |
| `META_ATTRIBUTION_ID` | Campaign source, 1–64 characters from `A-Z a-z 0-9 _ -`. Stored with each visit and ultimately in `user_bot.meta_source_id`, not exposed as the visit token. |
| `META_CAPI_ACCESS_TOKEN` | Secret, server-only Conversions API token. Never put it in HTML or a URL. |
| `META_TEST_EVENT_CODE` | Optional Events Manager test code. Remove for real conversion delivery. |
| `META_GRAPH_API_VERSION` | Defaults to `v25.0`. Review against Meta's supported Graph API versions when upgrading. |
| `META_VISIT_TTL_SECONDS` | Visit-token lifetime, default 86400 seconds, capped at seven days. Expired visits cannot attribute a start. |
| `META_TRUSTED_PROXY_HOPS` | Default 0. Trust forwarded IPs only after verifying the deployment's proxy contract; otherwise the request's direct remote address is used. |

Existing bot credentials and external PostgreSQL settings remain necessary.
Startup adds nullable attribution fields to `user_bot` and the separate tracking
tables. It does not replace the existing database or rewrite existing users.

When enabled, `/bot` creates a visit containing the configured source even if
CAPI credentials are unavailable. If visit storage fails, it falls back to the
direct campaign link so onboarding still works without browser matching.
Browser events need only the enabled switch and valid pixel ID; they do not
depend on CAPI readiness. Disabled tracking and privacy opt-outs skip Pixel
initialization and visit collection, but the button still carries the configured
campaign source if valid. Browser collection controls never strip that source.

## Visit links, direct sources and saving

For an eligible tracked visit, the button's URL is:

`https://t.me/rabahcoupons1bot?start=mv_<32 random URL-safe characters>`

Each request gets a fresh 192-bit bearer token. Only its SHA-256 digest is
stored in `meta_bot_visits`, alongside the campaign source, pixel, bounded
matching data and expiration. The raw token contains no browser or Telegram
identity. Treat a copied visit URL as a bearer capability; do not share it.

The initial request captures valid `_fbp`/`_fbc`, a valid `fbclid` converted to
`fbc`, request IP under the configured proxy policy, and bounded user-agent.
The page enriches the visit with Pixel cookies shortly after load and again
on a real button click. Requests use same-origin keepalive fetch. The click
handler never intercepts, delays or replaces native anchor navigation: the
button still opens Telegram when JavaScript, Pixel or enrichment is blocked.

On receipt of a `/start mv_...`, a database transaction:

1. Creates the bot user if absent, or locks the existing user's row.
2. Locks the exact unexpired visit. The first account claims it; repeated starts
   by the same account are idempotent, and another account cannot reuse it.
3. Saves the visit's original campaign source in `meta_source_id`, **not** the
   random token. Unknown, expired or another account's tokens do not overwrite
   an existing source or prevent basic user registration.
4. Creates a conversion only if the resolved source matches the configured
   campaign, tracking/CAPI are ready, and the previous saved value was not
   already that campaign source. The visit pixel must also match the current
   pixel. Only this exact visit's matching data may accompany the event.
5. Uses a unique durable user receipt to prevent a second event, even if the
   source changes away and back, commands arrive concurrently, or the user row
   is subsequently removed.

Direct links such as `?start=fbads` still work: a valid non-visit source is
saved literally, including nonmatching sources, but no browser visit is guessed.
The `mv_` prefix followed by exactly 32 URL-safe characters is reserved for visit
tokens. Browser data is never matched using IP or timing.

### User-row browser fields

Redeeming a valid visit copies these values into the same `user_bot` row as
the Telegram account; all fields are nullable for ordinary/legacy users:

| Column | Value |
| --- | --- |
| `meta_source_id` | Original campaign source captured from `META_ATTRIBUTION_ID`, not the random token. |
| `meta_visit_id` | SHA-256 digest of the exact redeemed visit token; raw bearer tokens are never stored. |
| `meta_pixel_id` | Visit's pixel snapshot. |
| `meta_fbp`, `meta_fbc` | Available validated Meta cookies/click identifier. An initial click-derived `fbc` wins over a stale browser cookie. |
| `meta_client_ip_address` | Request IP under the configured proxy-trust policy. |
| `meta_client_user_agent` | Bounded browser user-agent. |
| `meta_visit_at` | Original visit timestamp. |

An unavailable element stays NULL rather than being invented. A fresh valid
visit updates these browser fields even when the account already has the
matching source and therefore must not queue another event. Direct source-only
starts preserve the last proven visit fields but do not reuse them in a new
event. Expired, unknown and another account's tokens cannot alter these fields.

Existing users with an empty or different source are eligible under the user's
clarified rule. A nonmatching source is saved without an event. A later valid
source replaces the saved value; a plain or invalid `/start` does not erase it.
Source capture does not depend on Meta being enabled or its token being valid.
If the matching source was saved while delivery was disabled, repeating it
after enabling delivery does not retroactively create an event.

Claiming a visit, saving the source and queuing its event are atomic inside a savepoint, separate
from the basic bot-user insert. If optional source/event work fails, only those
changes are rolled back: basic registration remains, and a subsequent start can
retry tracking without a saved campaign value incorrectly suppressing it.
If the tracking connection itself fails, ordinary registration is retried on a
separate connection. An outage affecting the entire database can still prevent
registration; this is not hidden as a successful write.
The bot's existing replies stay unchanged. No historical users are bulk-updated.

Console-only `/start` diagnostics show whether the handler was entered and
whether a Meta event was queued. Tracking persistence failures log only their
exception type and SQLSTATE, not tokens, names, chat IDs or SQL row values.

A direct campaign link follows the same registration rules but generates no
`PageView` or `TelegramClick` because `/bot` was not opened.

## Matching data and privacy

The controller must provide an appropriate privacy notice and establish a
lawful basis and any required consent **before** enabling this integration.
A subscription button is not tracking consent by itself. If consent is required
for your visitors, keep tracking disabled until your upstream flow obtains it
for every tracked visitor; do not enable it globally for mixed-consent traffic.
The existing notice must explain Meta sharing and retention.

The page honors `Sec-GPC: 1`, `DNT: 1`, and the first-party cookie
`meta_consent=denied` for browser collection: no visit is created and Pixel is
not initialized. The button retains a valid configured campaign start payload.
Such direct Telegram campaign links do not carry browser consent state; the
server may process a matching source under the normal bot-event eligibility
rules. Enabling that campaign requires a lawful basis for the bot event itself
as well. Do not rely on a shared start payload to convey browser consent.

The browser Pixel sends its own page/click matching data, subject to browser
restrictions and consent. A redeemed visit token additionally supplies its
validated `fbp`, `fbc`, IP and user-agent to the bot event, together with a
SHA-256-hashed, namespaced Telegram ID as `external_id`. A direct shared campaign
link supplies only that hashed external ID. The integration does
not attach a browser's IP, fbp or fbc by guessing from timing or IP addresses.
This limits matching quality and does not guarantee Meta can match a person.
Email, phone and unverified Telegram display names are not invented or sent.

Visit records retain bounded browser matching data and a hashed account claim
until their expiration, then hourly maintenance deletes them. After a visit is
claimed, browser enrichment cannot change it. Expired tokens cannot create an
attribution even before maintenance physically deletes their records. Browser
snapshots copied into `user_bot` remain with that account; visit expiration and
event delivery do not erase those user-row columns. Include them in the bot's
account retention, access and deletion policy.
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
   Before clicking, only PageView should be queued. Click the Telegram button
   to queue TelegramClick and navigate; waiting alone must never navigate.
   Confirm the Telegram link contains a fresh `start=mv_...` token, and that
   `meta_bot_visits` stores only its hash with the configured source. A privacy
   opt-out retains the direct campaign link but does not create a visit.
4. Use a test Telegram account without a previously saved matching source or
   prior conversion to press Start. An existing account with an empty/different
   source is eligible. Verify `TelegramBotStart` under Test Events with
   `action_source=chat`. Do not delete real users or receipts to manufacture
   eligibility.
5. Repeat Start, test a direct campaign link with another new test account, and
   test organic and mismatched links. Confirm mismatched sources are saved but
   do not create events, and repeat matching starts do not create duplicates.
   Inspect errors without copying secrets/customer data.
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

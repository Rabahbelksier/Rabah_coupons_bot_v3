# Server diagnostics

Console logs are JSON lines. Telegram replies, exception handling, retry policy,
database writes and the existing admin error-report format are unchanged.
No additional logging dependencies or credentials are required.

## Reading Railway logs

Search `trace_id` to group records for an observed operation. Stages currently
include `telegram.webhook`, `telegram.message`, `product.offer`, `channel.batch`,
`ai.generate`, `aliexpress.request`, `offers.search`, and `smart_cart.calculate`.
AliExpress failures also include provider, attempt, HTTP status when available,
and whether the existing retry policy allows another attempt.
Context propagates to asynchronous
tasks and `asyncio.to_thread`. Product jobs and channel batches get their own
trace IDs. These are operation identifiers, not persistent Telegram update IDs.
Callbacks and unrelated background work may have no trace ID.

Fields:
- `timestamp`: UTC timestamp.
- `level`, `logger`, `source`: severity, module, file and line.
- `stage`, `event`: operation and started/completed/failed/interrupted boundary.
- `duration_ms`: elapsed time at that boundary.
- `error_type`, `traceback`: exception details when available.

`completed` means the function returned normally, NOT necessarily that its
business operation succeeded. Existing functions sometimes handle failures
internally; inspect their warning/error records under the same trace ID.
`failed` means an exception escaped the observed function; it is re-raised
unchanged. A lifecycle failure and the caller's existing error record may both
appear; these are not necessarily two failed requests.

## Privacy and behavior

Arguments, return values, user messages and credentials are not added to logs.
Console formatting removes URL paths/query strings, database connection URLs,
Telegram token patterns, bearer credentials and common labelled secret fields.
Pattern-based masking cannot guarantee detection of arbitrary unlabelled secrets;
never add them to log messages. Existing database reports retain their original
format and content; console masking is not a database-log sanitizer.

Lifecycle diagnostics go only to console, never to `bot_error_logs`, so they do
not add entries or change text in the bot's administrator reports.
Logging failures are suppressed; return values and raised exceptions are preserved.
No network calls or database calls are introduced by this diagnostics module.

The Railway deployment needs the updated code before these logs appear.
No production deployment is performed by editing this project.

# Server diagnostics

Console logs are JSON lines. Telegram replies, exception handling and retry
policies are unchanged. Admin log downloads use Railway CLI and do not read or
write error logs in the application database.

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
never add them to log messages. Console masking is not a universal log sanitizer.

The admin menu downloads Railway runtime logs as JSON files for the last 15
minutes, hour, 24 hours, seven days, or current calendar month. The current
month starts at midnight in Africa/Algiers. Downloads are capped by
`RAILWAY_LOGS_MAX_LINES`; the file marks when it may have reached that cap.
Previously persisted `bot_error_logs` rows are not deleted, but new rows are no
longer written and the bot no longer creates or queries that table.

The Railway runtime needs the CLI installed and a project-scoped token in
`RAILWAY_TOKEN`. Configure `RAILWAY_LOGS_PROJECT_ID`,
`RAILWAY_LOGS_ENVIRONMENT_ID`, and `RAILWAY_LOGS_SERVICE_ID` for the service
whose logs are downloaded. Optional CLI path, line-limit, and timeout settings
have defaults in `config.py`.

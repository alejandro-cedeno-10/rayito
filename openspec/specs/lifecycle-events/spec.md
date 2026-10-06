# lifecycle-events Specification

## Purpose
TBD - created by archiving change m15-events-webhooks. Update Purpose after archive.

## Requirements

### Requirement: rayd emits signed lifecycle event lines only when configured
`rayd` SHALL emit one stdout line per lifecycle transition, formatted
`rayito.event.v1 <base64url(event_json)> <base64url(hmac_sha256)>`, only
after a `ConfigureSandbox` call carries a `LifecycleEventsConfig` with a
non-empty `sandbox_key`. Without such a call, `rayd` SHALL emit zero lines,
open no socket beyond what 0.5.x already opens, and spawn no extra process.
`created` SHALL fire exactly once, on the transition from no key to a key
present; `paused`/`resumed` SHALL fire from `/suspend`/`/resume`;
`killed{reason: "request"}` SHALL fire from `/terminate`. The MAC SHALL be
`HMAC-SHA256(k_sbx, event_json_bytes)` where `k_sbx` is the bytes of
`LifecycleEventsConfig.sandbox_key` exactly as received — `rayd` SHALL NOT
derive or store the stack-wide secret.

#### Scenario: no section, no line
- **WHEN** a sandbox runs `create → commands.run → pause → resume → kill` with no `events=`
- **THEN** `rayd`'s stdout contains no `rayito.event.v1` line and no `ConfigureSandbox` call was made

#### Scenario: created fires once
- **WHEN** `ConfigureSandbox` carries a `LifecycleEventsConfig` with a key, twice in a row (a rotation)
- **THEN** exactly one `created` line is emitted, not two

#### Scenario: resume bumps the generation
- **WHEN** a configured sandbox is suspended and resumed
- **THEN** the `resumed` event's `generation` is one more than the `created` event's

### Requirement: paused and killed are flushed before the hook answers, within a bound
On `/suspend` and `/terminate`, a configured `rayd` SHALL wait until the `paused`/`killed` line it queued has been written to stdout and stdout flushed, for at most its `/suspend` share (never more than `SUSPEND_SHARE_MAX`) and at most `PARTICIPANT_TERMINATE_TIMEOUT` respectively, and SHALL NOT wait at all when no line was queued. If the OS random source fails, `rayd` SHALL drop the event and count it with `last_error_class = "random_unavailable"` rather than emit an `event_id` that is not fresh.

#### Scenario: a stuck sink costs the share and no more
- **WHEN** the sink never finishes flushing during `/suspend`
- **THEN** the participant returns after exactly its share, reporting `timed_out`

### Requirement: The forwarder verifies the MAC and the sandbox identity before writing
The forwarder Lambda SHALL take the `sandbox_id` from the CloudWatch Logs stream the line arrived on (everything after the last `]` of `YYYY/MM/DD[<imageVersion>]<microvmId>`, measured in `AWS_API_NOTES.md` Q106), SHALL re-derive `k_sbx = HMAC-SHA256(stack_key, "rayito.events.v1|" + sandbox_id)` from the stack-wide secret, and SHALL verify the line's MAC in constant time **before** parsing its payload. It SHALL reject (count, never write) any line whose MAC does not match, whose payload is not a JSON object of the exact wire shape (string fields of at most 512 characters, integer fields JSON integers in `[0, 2^64)`), whose `sandbox_id` is not the stream's, whose fields are not what `rayd` emits (`event_id` of 32 lowercase hex characters, `kill_reason` exactly `request` on `killed` and absent otherwise, a `microvm-image` ARN), or whose `occurred_at_ms` is more than 24 hours before or more than 5 minutes after the forwarder's own clock. No input SHALL make the decision raise; a line SHALL never fail the invocation. Accepted events SHALL be written idempotently (a repeated `event_id` SHALL NOT create a second row or a Lambda error). If a table write fails, the forwarder SHALL still process every other line of the batch and SHALL then fail the invocation, whose asynchronous retries end in an SQS OnFailure destination. Each forwarder invocation SHALL log one structured line with the accepted count, the failed-write count, the rejected count and the rejected count per closed reason, and SHALL NOT log the line, the `sandbox_id` or the MAC; each reconciler run SHALL log how many events it synthesized.

#### Scenario: a forged line is dropped
- **WHEN** a line's JSON is modified after the MAC was computed
- **THEN** the forwarder does not write a row and counts the line as rejected

#### Scenario: a sandbox cannot claim another sandbox's identity
- **WHEN** a line signed with one sandbox's key arrives on another sandbox's log stream, or a valid line's `sandbox_id` field does not match its own stream
- **THEN** the forwarder rejects it (`mac_invalid` or `sandbox_mismatch`)

#### Scenario: an unauthenticated poison line never drops genuine lines
- **WHEN** a batch holds genuine lines and unsigned payloads such as `[]`, `"x"`, a non-string `sandbox_id`, `generation: 1e400` or 100 000 nested `[`
- **THEN** the genuine lines are written, each poison line is counted as rejected without being parsed, and the invocation succeeds

#### Scenario: a replayed line is refused once its dedupe row expired
- **WHEN** a signed line is written again more than 24 hours after its `occurred_at_ms`
- **THEN** the forwarder counts it as `stale_event` and writes nothing

#### Scenario: rejections are visible in the forwarder's log
- **WHEN** an invocation accepts one line and rejects one forged and one malformed line
- **THEN** its log holds one line `{"failed_writes": 0, "forwarded": 1, "rejected": 2, "rejected_by_reason": {"mac_invalid": 1, "malformed_line": 1}}` and no part of the rejected lines

### Requirement: The deliverer signs deliveries E2B-compatibly and blocks SSRF
The deliverer SHALL sign each webhook request with `e2b-webhook-id`, `e2b-delivery-id`, `e2b-signature-version: v1` and `e2b-signature` (base64 without padding of `sha256(secret + payload)`), SHALL only ever connect over `https://`, SHALL NOT follow redirects, and SHALL resolve the target hostname, classify every candidate address, and connect only to one address already classified as safe — never re-resolving at connect time. It SHALL reject (not deliver, not retry) loopback, private, link-local (including the IMDS address), multicast, reserved and CGNAT (100.64.0.0/10) addresses, and SHALL treat a URL it cannot parse (an invalid port, a malformed IPv6 literal, a host IDNA cannot encode) as a permanent failure of that webhook. It SHALL attempt a delivery at most 3 times with backoff, retrying only a 5xx answer or a transport error, SHALL bound each attempt as a whole (DNS resolution and every send and receive) by the attempt's timeout, and SHALL read at most 64 KiB of any response. Any other unexpected error while delivering to one webhook SHALL fail only that webhook, never the other webhooks of the record or the batch. It SHALL record each (sandbox, event, webhook) triple as `attempting` before the first attempt and `delivered` or `failed` after, keyed by `DELIVERY#<sandbox_id>#<event_id>`; only a `delivered` entry SHALL be skipped when its at-least-once trigger hands it the same event again, so a delivery is never lost to a crash or timeout and never repeated once delivered. It SHALL fit every attempt and backoff into the invocation's remaining time and, when that runs out, report the unfinished record as a batch item failure.

#### Scenario: an SSRF target is never dialed
- **WHEN** a webhook URL resolves to `169.254.169.254` or any other blocked address
- **THEN** no connection is attempted and the delivery is not retried

#### Scenario: a failed delivery is attempted again on redelivery
- **WHEN** a webhook answered 500 to every attempt and the stream redelivers the same record
- **THEN** the deliverer attempts that webhook again, and a pair already `delivered` is not attempted

#### Scenario: a signature a verifier can check
- **WHEN** a webhook receives a delivery
- **THEN** `base64_decode(e2b-signature)` equals `sha256(secret + raw_body_bytes)`

#### Scenario: one broken webhook never blocks the others
- **WHEN** one webhook's URL is `https://h:99999/` and another webhook subscribes to the same type
- **THEN** the first is recorded `failed` without a retry, the second is delivered, and the batch reports no failure

#### Scenario: a trickling receiver cannot outlast the attempt
- **WHEN** a receiver answers one byte at a time, each well within a per-read timeout
- **THEN** the attempt ends with a timeout once its deadline passes

### Requirement: The reconciler synthesizes killed events for sandboxes that vanished
A scheduled reconciler SHALL compare `ListMicrovms` against the sandboxes the events table still considers open (no `killed` event recorded) and SHALL write a `killed{reason: "unknown"}` event, with a deterministic id and the generation and image of the sandbox's last recorded event, for each one missing from the live set (or listed only in a terminal state) — re-running the reconciler over the same gap SHALL NOT create a second event. It SHALL find the open sandboxes by querying a sparse index that holds only open sandboxes' state rows, never by scanning the table, so its cost does not grow with the number of event or delivery rows. A sandbox's recorded state SHALL only move forward, and SHALL stay `killed` once a `killed` event is recorded, so a late or duplicate line can never reopen it.

#### Scenario: a vanished sandbox gets a killed event
- **WHEN** a sandbox the table considers open does not appear in `ListMicrovms`
- **THEN** exactly one synthesized `killed{reason: "unknown"}` event is written for it, even if the reconciler runs again before the next real event

#### Scenario: a line after killed does not reopen the sandbox
- **WHEN** a `resumed` line arrives after the sandbox's `killed` was recorded
- **THEN** the sandbox stays closed and the reconciler synthesizes nothing for it

#### Scenario: the reconciler never scans the table
- **WHEN** the reconciler lists open sandboxes
- **THEN** it queries the `open` index and the table fake has no scan operation at all

### Requirement: events= and LifecycleEvents are off by default and never implicit
`events=`/`events` on `Sandbox.create()` SHALL default to `None`/`undefined`; without it, the SDK SHALL build no DynamoDB, Secrets Manager or CloudFormation client and SHALL send no `ConfigureSandbox` call. Passing to `Sandbox.create()`/`AsyncSandbox.create()` an `events=` that is not a `LifecycleEvents`/`AsyncLifecycleEvents`, or with a `logging` that does not reach CloudWatch, SHALL raise `InvalidArgumentException`. A valid `events=` SHALL be sent, after `run-microvm` and the first `Health`, as a `LifecycleEventsConfig` section of the same single `ConfigureSandbox` call that carries every other 0.6 section, with `sandbox_key = HMAC-SHA256(stack_key, "rayito.events.v1|" + sandbox_id)` derived by the SDK from the stack's secret (read at most once per `LifecycleEvents` instance) and the `sandbox_id`/`image_arn`/`image_version` of that launch; if the stack is not deployed, its key cannot be read, the agent does not report `lifecycle_events` support or the section is not applied, `create()` SHALL terminate the sandbox (unless `keep_on_failure`) and raise. Every AWS error from `LifecycleEvents` SHALL surface as `WebhookException`/`WebhookError` carrying only the AWS error code, never an ARN or account id. Constructing `LifecycleEvents`/`AsyncLifecycleEvents` SHALL make no AWS call; `deploy`/`status`/`destroy`/`register_webhook`/`list_webhooks`/`delete_webhook`/`get_events` SHALL each be explicit calls, usable today without `Sandbox.create(events=...)`.

#### Scenario: the zero-cost golden trace is unaffected
- **WHEN** the existing `create → commands.run → files.write → pause → resume → commands.run → kill → list` scripted session runs with no 0.6 option set
- **THEN** its boto3 operations, `runHookPayload` and gRPC method sequence are unchanged from `fixtures/zero_cost_0_5_trace.json`

#### Scenario: events= sends the sandbox key in the single Configure
- **WHEN** `Sandbox.create(events=LifecycleEvents(), logging="cloudwatch", execution_role_arn=...)` launches against a 0.6 agent reporting `lifecycle_events`
- **THEN** exactly one `ConfigureSandbox` call carries a `LifecycleEventsConfig` whose `sandbox_key` is the derived `k_sbx` for that `sandbox_id`, never the stack key

#### Scenario: events= without a deployed stack terminates the sandbox
- **WHEN** `Sandbox.create(events=LifecycleEvents(), logging="cloudwatch")` runs and the `events-webhooks` stack does not exist
- **THEN** `WebhookException` is raised and the MicroVM is terminated

#### Scenario: events= without CloudWatch logging is invalid
- **WHEN** `Sandbox.create(events=LifecycleEvents(), logging="disabled")` is called
- **THEN** `InvalidArgumentException` is raised before any AWS call

#### Scenario: building LifecycleEvents makes no AWS call
- **WHEN** `LifecycleEvents()` is constructed
- **THEN** no `boto3.session.Session.client` call is made for `dynamodb`, `secretsmanager` or `cloudformation`

### Requirement: register_webhook only accepts URLs the deliverer can reach
`LifecycleEvents.register_webhook`/`registerWebhook` SHALL reject with `InvalidArgumentException`/`InvalidArgumentError`, before any AWS call and without echoing the URL, any URL that is not `https://`, has no host, has a port outside 1–65535, has a DNS host whose ASCII form has a label outside 1–63 characters of `[A-Za-z0-9_-]` or exceeds 253 characters, or contains whitespace, control characters or backslashes. Both SDKs SHALL agree on `testdata/lifecycle-events/webhook-url-vectors.json`.

#### Scenario: an unreachable URL is never stored
- **WHEN** `register_webhook("https://h:99999/", ...)` is called
- **THEN** `InvalidArgumentException` is raised, its message does not contain the URL, and no row is written

### Requirement: The forwarder admits only events that move a sandbox forward, at a bounded rate
After the MAC and shape checks, the forwarder SHALL admit an event only if it moves its sandbox's recorded lifecycle strictly forward: `created` only as the sandbox's first event, `paused` only after `created` or `resumed` of the same generation, `resumed` only with a generation higher than the last admitted event's, and nothing after `killed`; a gap SHALL be allowed. `paused` and `resumed` SHALL additionally spend from a per-sandbox token bucket kept on the sandbox's state row (a burst of 20, then one every 30 seconds, measured on the forwarder's clock). An event refused by either rule SHALL be counted under `invalid_transition` or `rate_limited` and SHALL NOT be stored or delivered. The state row SHALL be written with an optimistic-concurrency condition, and a retry of the last admitted event SHALL still store it.

#### Scenario: a guest-driven suspend/resume loop is bounded
- **WHEN** one batch holds a `created` followed by 20 authentic `paused`/`resumed` pairs of the same sandbox
- **THEN** 20 of those 40 events are stored and the other 20 are counted as `rate_limited`

#### Scenario: an older position is refused
- **WHEN** a `paused` of generation 0 arrives after a `resumed` of generation 1 was admitted
- **THEN** it is counted as `invalid_transition` and not stored

### Requirement: Only new event rows invoke the deliverer
The deliverer's event source mapping SHALL carry a filter that passes only `INSERT` records whose new image has a partition key starting with `EVENT#`, so delivery-status, state and webhook writes never invoke it.

#### Scenario: the filter is pinned
- **WHEN** `scripts/tests/test_events_webhooks_template.py` reads `DelivererEventSourceMapping`
- **THEN** its `FilterCriteria` holds exactly that pattern

### Requirement: Every delivery carries a timestamped HMAC and rotated secrets take effect in bounded time
Besides the E2B headers, every delivery attempt SHALL carry `rayito-signature: t=<unix seconds>,v1=<hex HMAC-SHA256(secret, "<t>.<webhook_id>." + body)>`. The deliverer SHALL cache a webhook secret for at most 300 seconds and, when a receiver answers 401 or 403, SHALL re-read the secret once past the cache and, if it changed, retry immediately with the new value.

#### Scenario: a rotated secret is used after a 401
- **WHEN** a warm deliverer holds the old secret and the receiver answers 401, then 200
- **THEN** the second attempt is signed with the new secret and the pair is recorded `delivered`

### Requirement: The stack emits one caller policy per job
`infra/events-webhooks.yaml` SHALL emit `EventsLauncherPolicy` (read the stack key, `DescribeStacks`), `EventsReaderPolicy` (`Query` on `EVENT#*` rows and the `gsi1` index, `DescribeStacks`) and `EventsWebhookAdminPolicy` (`PutItem`/`DeleteItem`/`Query` on `WEBHOOK` rows only, `DescribeStacks`), each as an output. `EventsOperatorPolicy` SHALL remain, as their deprecated union, for one release. Each Lambda SHALL log only to its own log group, created by the stack.

#### Scenario: a reader cannot register a webhook
- **WHEN** the template test reads `EventsReaderPolicy`
- **THEN** it grants no `PutItem`, no `DeleteItem` and no `GetSecretValue`

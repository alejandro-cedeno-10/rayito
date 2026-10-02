## ADDED Requirements

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
The forwarder Lambda SHALL re-derive `k_sbx = HMAC-SHA256(stack_key, "rayito.events.v1|" + sandbox_id)` from the stack-wide secret, using the `sandbox_id` carried inside the event JSON, and SHALL reject (count, never write) any line whose MAC does not match, in constant time, or whose `sandbox_id` is not the microVM id that ends the CloudWatch Logs stream the line arrived on (`YYYY/MM/DD[<imageVersion>]<microvmId>`, measured in `AWS_API_NOTES.md` Q106). Accepted events SHALL be written idempotently (a repeated `event_id` SHALL NOT create a second row or a Lambda error). Each forwarder invocation SHALL log one structured line with the accepted count, the rejected count and the rejected count per closed reason, and SHALL NOT log the line, the `sandbox_id` or the MAC; each reconciler run SHALL log how many events it synthesized.

#### Scenario: a forged line is dropped
- **WHEN** a line's JSON is modified after the MAC was computed
- **THEN** the forwarder does not write a row and counts the line as rejected

#### Scenario: a sandbox cannot claim another sandbox's identity
- **WHEN** a line's own derivable MAC is valid for its key but its `sandbox_id` field does not match the log stream it arrived on
- **THEN** the forwarder rejects it

#### Scenario: rejections are visible in the forwarder's log
- **WHEN** an invocation accepts one line and rejects one forged and one malformed line
- **THEN** its log holds one line `{"forwarded": 1, "rejected": 2, "rejected_by_reason": {"mac_invalid": 1, "malformed_line": 1}}` and no part of the rejected lines

### Requirement: The deliverer signs deliveries E2B-compatibly and blocks SSRF
The deliverer SHALL sign each webhook request with `e2b-webhook-id`, `e2b-delivery-id`, `e2b-signature-version: v1` and `e2b-signature` (base64 without padding of `sha256(secret + payload)`), SHALL only ever connect over `https://`, SHALL NOT follow redirects, and SHALL resolve the target hostname, classify every candidate address, and connect only to one address already classified as safe — never re-resolving at connect time. It SHALL reject (not deliver, not retry) loopback, private, link-local (including the IMDS address), multicast, reserved and CGNAT (100.64.0.0/10) addresses. It SHALL attempt a delivery at most 3 times with backoff, retrying only a 5xx answer or a transport error, and SHALL read at most 64 KiB of any response. It SHALL record each (event, webhook) pair as `attempting` before the first attempt and `delivered` or `failed` after; only a `delivered` pair SHALL be skipped when its at-least-once trigger hands it the same event again, so a delivery is never lost to a crash or timeout and never repeated once delivered. It SHALL fit every attempt and backoff into the invocation's remaining time and, when that runs out, report the unfinished record as a batch item failure.

#### Scenario: an SSRF target is never dialed
- **WHEN** a webhook URL resolves to `169.254.169.254` or any other blocked address
- **THEN** no connection is attempted and the delivery is not retried

#### Scenario: a failed delivery is attempted again on redelivery
- **WHEN** a webhook answered 500 to every attempt and the stream redelivers the same record
- **THEN** the deliverer attempts that webhook again, and a pair already `delivered` is not attempted

#### Scenario: a signature a verifier can check
- **WHEN** a webhook receives a delivery
- **THEN** `base64_decode(e2b-signature)` equals `sha256(secret + raw_body_bytes)`

### Requirement: The reconciler synthesizes killed events for sandboxes that vanished
A scheduled reconciler SHALL compare `ListMicrovms` against the sandboxes the events table still considers open (no `killed` event recorded) and SHALL write a `killed{reason: "unknown"}` event, with a deterministic id and the generation and image of the sandbox's last recorded event, for each one missing from the live set (or listed only in a terminal state) — re-running the reconciler over the same gap SHALL NOT create a second event. A sandbox's recorded state SHALL only move forward in time, and SHALL stay `killed` once a `killed` event is recorded, so a late or duplicate line can never reopen it.

#### Scenario: a vanished sandbox gets a killed event
- **WHEN** a sandbox the table considers open does not appear in `ListMicrovms`
- **THEN** exactly one synthesized `killed{reason: "unknown"}` event is written for it, even if the reconciler runs again before the next real event

#### Scenario: a line after killed does not reopen the sandbox
- **WHEN** a `resumed` line arrives after the sandbox's `killed` was recorded
- **THEN** the sandbox stays closed and the reconciler synthesizes nothing for it

### Requirement: events= and LifecycleEvents are off by default and never implicit
`events=`/`events` on `Sandbox.create()` SHALL default to `None`/`undefined`; without it, the SDK SHALL build no DynamoDB, Secrets Manager or CloudFormation client and SHALL send no `ConfigureSandbox` call. Passing to `Sandbox.create()`/`AsyncSandbox.create()` an `events=` that is not a `LifecycleEvents`/`AsyncLifecycleEvents`, or with a `logging` that does not reach CloudWatch, SHALL raise `InvalidArgumentException`; a valid `events=` SHALL raise `UnimplementedError` naming this change and the missing `ConfigureSandbox` send (D5: `create()` has nowhere yet to dispatch the section — see `ADR-020`), until the shared "send `FeaturePlan.configure_sections` after `run-microvm`" wiring lands. Every AWS error from `LifecycleEvents` SHALL surface as `WebhookException`/`WebhookError` carrying only the AWS error code, never an ARN or account id. Constructing `LifecycleEvents`/`AsyncLifecycleEvents` SHALL make no AWS call; `deploy`/`status`/`destroy`/`register_webhook`/`list_webhooks`/`delete_webhook`/`get_events` SHALL each be explicit calls, usable today without `Sandbox.create(events=...)`.

#### Scenario: the zero-cost golden trace is unaffected
- **WHEN** the existing `create → commands.run → files.write → pause → resume → commands.run → kill → list` scripted session runs with no 0.6 option set
- **THEN** its boto3 operations, `runHookPayload` and gRPC method sequence are unchanged from `fixtures/zero_cost_0_5_trace.json`

#### Scenario: events= is rejected before launch
- **WHEN** `Sandbox.create(events=LifecycleEvents(), logging="cloudwatch")` is called
- **THEN** `UnimplementedError` is raised and no `RunMicrovm` call is made

#### Scenario: events= without CloudWatch logging is invalid
- **WHEN** `Sandbox.create(events=LifecycleEvents(), logging="disabled")` is called
- **THEN** `InvalidArgumentException` is raised before any AWS call

#### Scenario: building LifecycleEvents makes no AWS call
- **WHEN** `LifecycleEvents()` is constructed
- **THEN** no `boto3.session.Session.client` call is made for `dynamodb`, `secretsmanager` or `cloudformation`

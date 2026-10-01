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

### Requirement: The forwarder verifies the MAC and the sandbox identity before writing
The forwarder Lambda SHALL re-derive `k_sbx = HMAC-SHA256(stack_key, "rayito.events.v1|" + sandbox_id)` from the stack-wide secret, using the `sandbox_id` carried inside the event JSON, and SHALL reject (count, never write) any line whose MAC does not match, in constant time, or whose `sandbox_id` does not appear in the CloudWatch Logs stream the line arrived on. Accepted events SHALL be written idempotently (a repeated `event_id` SHALL NOT create a second row or a Lambda error).

#### Scenario: a forged line is dropped
- **WHEN** a line's JSON is modified after the MAC was computed
- **THEN** the forwarder does not write a row and counts the line as rejected

#### Scenario: a sandbox cannot claim another sandbox's identity
- **WHEN** a line's own derivable MAC is valid for its key but its `sandbox_id` field does not match the log stream it arrived on
- **THEN** the forwarder rejects it

### Requirement: The deliverer signs deliveries E2B-compatibly and blocks SSRF
The deliverer SHALL sign each webhook request with `e2b-webhook-id`, `e2b-delivery-id`, `e2b-signature-version: v1` and `e2b-signature` (base64 without padding of `sha256(secret + payload)`), SHALL only ever connect over `https://`, SHALL NOT follow redirects, and SHALL resolve the target hostname, classify every candidate address, and connect only to one address already classified as safe — never re-resolving at connect time. It SHALL reject (not deliver, not retry) loopback, private, link-local (including the IMDS address), multicast, reserved and CGNAT (100.64.0.0/10) addresses. It SHALL retry a failed delivery at most 3 times with backoff and SHALL deduplicate against its own at-least-once trigger so one event is never delivered twice to the same webhook for the same `event_id`.

#### Scenario: an SSRF target is never dialed
- **WHEN** a webhook URL resolves to `169.254.169.254` or any other blocked address
- **THEN** no connection is attempted and the delivery is not retried

#### Scenario: a signature a verifier can check
- **WHEN** a webhook receives a delivery
- **THEN** `base64_decode(e2b-signature)` equals `sha256(secret + raw_body_bytes)`

### Requirement: The reconciler synthesizes killed events for sandboxes that vanished
A scheduled reconciler SHALL compare `ListMicrovms` against the sandboxes the events table still considers open (no `killed` event recorded) and SHALL write a `killed{reason: "unknown"}` event, with a deterministic id, for each one missing from the live set — re-running the reconciler over the same gap SHALL NOT create a second event.

#### Scenario: a vanished sandbox gets a killed event
- **WHEN** a sandbox the table considers open does not appear in `ListMicrovms`
- **THEN** exactly one synthesized `killed{reason: "unknown"}` event is written for it, even if the reconciler runs again before the next real event

### Requirement: events= and LifecycleEvents are off by default and never implicit
`events=`/`events` on `Sandbox.create()` SHALL default to `None`/`undefined`; without it, the SDK SHALL build no DynamoDB, Secrets Manager or CloudFormation client and SHALL send no `ConfigureSandbox` call. `events=` SHALL require `logging="cloudwatch"` (Python) / `logging: "cloudwatch"` (TypeScript) and SHALL raise `InvalidArgumentException`/`InvalidArgumentError` before any AWS call otherwise. Constructing `LifecycleEvents`/`AsyncLifecycleEvents` SHALL make no AWS call; `deploy`/`status`/`destroy`/`register_webhook`/`list_webhooks`/`delete_webhook`/`get_events` SHALL each be explicit calls.

#### Scenario: the zero-cost golden trace is unaffected
- **WHEN** the existing `create → commands.run → files.write → pause → resume → commands.run → kill → list` scripted session runs with no 0.6 option set
- **THEN** its boto3 operations, `runHookPayload` and gRPC method sequence are unchanged from `fixtures/zero_cost_0_5_trace.json`

#### Scenario: events= without cloudwatch logging is rejected before launch
- **WHEN** `Sandbox.create(events=ev)` is called without `logging="cloudwatch"`
- **THEN** `InvalidArgumentException` is raised and no `RunMicrovm` call is made

#### Scenario: building LifecycleEvents makes no AWS call
- **WHEN** `LifecycleEvents()` is constructed
- **THEN** no `boto3.session.Session.client` call is made for `dynamodb`, `secretsmanager` or `cloudformation`

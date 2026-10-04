## MODIFIED Requirements

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

## ADDED Requirements

### Requirement: register_webhook only accepts URLs the deliverer can reach
`LifecycleEvents.register_webhook`/`registerWebhook` SHALL reject with `InvalidArgumentException`/`InvalidArgumentError`, before any AWS call and without echoing the URL, any URL that is not `https://`, has no host, has a port outside 1–65535, has a DNS host whose ASCII form has a label outside 1–63 characters of `[A-Za-z0-9_-]` or exceeds 253 characters, or contains whitespace, control characters or backslashes. Both SDKs SHALL agree on `testdata/lifecycle-events/webhook-url-vectors.json`.

#### Scenario: an unreachable URL is never stored
- **WHEN** `register_webhook("https://h:99999/", ...)` is called
- **THEN** `InvalidArgumentException` is raised, its message does not contain the URL, and no row is written

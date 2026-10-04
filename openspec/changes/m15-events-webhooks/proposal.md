## Why

Rayito 0.5.x has no way to learn that a sandbox was created, paused,
resumed or killed except by polling `Health`/`list-microvms` yourself.
Agent platforms that bill by sandbox-hour, clean up external resources on
kill, or audit lifecycle events for compliance all need a push-based
signal. M15's architecture (§7.4) assigns this to `m15-events-webhooks`:
`rayd` emits signed lifecycle events on stdout; an optional,
customer-account stack (forwarder/deliverer/reconciler Lambdas, a DynamoDB
table) turns them into verified rows and E2B-compatible webhook
deliveries, off by default and billed only when deployed and used.

## What Changes

- **`rayd` emits signed lifecycle events** (`crates/rayd-core/src/lifecycle_events/`,
  `crates/rayd/src/features/lifecycle_events.rs`, ADR-020): `created` (first
  `ConfigureSandbox` section with a key), `paused`/`resumed`
  (`/suspend`/`/resume`, via `LifecycleParticipant`), `killed{reason:
  request}` (`/terminate`). One stdout line,
  `rayito.event.v1 <b64url(event)> <b64url(hmac-sha256)>`; the key (`k_sbx`)
  is derived by the SDK and pushed through `ConfigureSandbox`, never
  computed by `rayd`. A bounded, non-blocking queue; zero lines without the
  section. `paused`/`killed` wait (bounded by `/suspend`'s share and by
  `/terminate`'s participant cap) for the sink to write and flush the line
  before the VM freezes or the process exits. `features::lifecycle_events`
  is the first feature slot with a real adapter (always `supported()`, but
  inert without a key); its state lives in the process's one `FeatureSet`,
  shared by `ConfigureService` and the hooks through the foundations
  wiring (`v06-foundations` §15, PR #87), never in a singleton.
- **`infra/events-webhooks.yaml`** (`OptionalStack`): a stack-wide HMAC
  secret, an on-demand DynamoDB table (streams on), three Lambdas
  (forwarder/deliverer/reconciler, Python 3.12, `infra/lambdas/events_webhooks/`),
  a CloudWatch Logs subscription filter, an SQS queue (the deliverer's
  on-failure destination) and an EventBridge Scheduler rule.
  `scripts/gen_stack_assets.py` injects `docs/aws-api/service-2.json` into
  the zip under `models/lambda-microvms/<apiVersion>/` (decision 8); the
  reconciler builds its client from a dedicated botocore session pointed
  there, and the template also sets `AWS_DATA_PATH`.
- **Forwarder**: re-derives `k_sbx` from the stack secret, checks the event's
  `sandbox_id` against its own log stream, verifies the MAC
  (constant-time), writes idempotently. Only a newly written event moves the
  sandbox's `STATE#` row, only forward in time; `killed` leaves a tombstone.
- **Deliverer**: E2B-compatible signature (`e2b-signature` = base64 of
  `sha256(secret + payload)`, no padding), an SSRF guard (resolve, classify,
  connect to the checked address — never re-resolve), https-only, no
  redirects, ≤ 3 attempts and only for 5xx or transport errors, a 64 KiB
  response cap. Each (event, webhook) pair is claimed (`attempting`) before
  its first attempt and finished `delivered`/`failed`; only `delivered` is
  skipped on redelivery, so nothing is lost. Attempts fit the invocation's
  remaining time; an unfinished record is reported in `batchItemFailures`.
- **Reconciler**: every `ReconcilerIntervalMinutes` (default 5, minimum 2),
  compares `ListMicrovms` against sandboxes the table still considers open,
  synthesizes `killed{unknown}` with the generation and image of the
  sandbox's last event (not yet `timeout`: that needs per-sandbox
  max-duration tracking this iteration does not add), deterministic ids
  over a window equal to the schedule's own interval.
- **SDK**: `LifecycleEvents`/`AsyncLifecycleEvents` (deploy/status/destroy
  over `OptionalStacks`; `register_webhook`/`list_webhooks`/`delete_webhook`/
  `get_events` direct on DynamoDB, usable today; AWS errors surface as
  `WebhookException`/`WebhookError` carrying only the AWS error code).
  `events=` on `Sandbox.create()` validates its type and that `logging`
  reaches CloudWatch, then raises `UnimplementedError` naming the missing
  post-`run-microvm` `ConfigureSandbox` send (D5):
  `LifecycleEvents._build_section` is implemented and unit-tested, but
  `create()` has nowhere yet to send the section. CLI `rayito events
  deploy|status|destroy|list` and `webhook add|list|remove`, reusing
  `rayito stack`'s cost/confirmation helpers.
- **TypeScript mirror**: `src/lifecycle-events/{domain,keys,section,dynamodb,service}.ts`,
  `LifecycleEvents` (one async class).

## Impact

- **Rust**: `crates/rayd-core/src/lifecycle_events/{mod,event,mac,emit}.rs`,
  `crates/rayd/src/{features/lifecycle_events.rs,adapters/stdout_event_sink.rs}`,
  `grpc/configure.rs` (the section is cloned out of the request), the
  slot's own tests in `features/mod.rs`, `crates/rayd-core/Cargo.toml`
  (`hmac`), `proto/rayito/v1/lifecycle_events.proto`. The shared wiring
  (`main.rs`, `grpc/mod.rs`, `grpc/health.rs`, `hooks/mod.rs`,
  `tests/common`, the workspace `hmac` pin) is foundations' (PR #87).
- **Python**: `_lifecycle_events/` (new package), `_feature_options.py`
  (events branch: validation, then `FeaturePlan.events`, sent after
  `run-microvm` by `LifecycleEventsSectionFactory` — D6),
  `_stacks/components/events_webhooks.py`, `cli/events.py`, `cli/stack.py`
  (`confirm_deploy`/`confirm_destroy` extracted for reuse), `__init__.py`
  exports. `create()`'s `logging=` reaches `plan_features` through
  foundations' seam (PR #87); no other edit to `sandbox_{sync,async}/main.py`.
- **TypeScript**: `src/lifecycle-events/`, `feature-options.ts`,
  `stacks/components/events-webhooks.ts`, `index.ts` exports.
- **Infra**: `infra/events-webhooks.yaml`, `infra/lambdas/events_webhooks/`.
- **Scripts**: `scripts/gen_stack_assets.py` (TS generator line-width fix;
  the artifact is an allowlist of `.py` sources outside `tests/`,
  `__pycache__` and hidden directories, plus the injected service model),
  and its drift check in CI.
- **No change** to 0.5.x behaviour without `events=`/`LifecycleEvents`: no
  new AWS client, no `ConfigureSandbox` call, no stdout line.

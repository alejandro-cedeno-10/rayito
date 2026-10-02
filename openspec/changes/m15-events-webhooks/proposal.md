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
  section. `features::lifecycle_events` is the first feature slot with a
  real adapter (always `supported()`, but inert without a key) — it keeps
  its live state behind a process-wide singleton rather than widen
  `grpc::Services`/`hooks::HookServices` across eight other milestones'
  test files; see `ADR-020`'s "Known integration gap" for the one piece
  (`create()`'s post-`run-microvm` dispatch) this change does not wire.
- **`infra/events-webhooks.yaml`** (`OptionalStack`): a stack-wide HMAC
  secret, an on-demand DynamoDB table (streams on), three Lambdas
  (forwarder/deliverer/reconciler, Python 3.12, `infra/lambdas/events_webhooks/`),
  a CloudWatch Logs subscription filter and an EventBridge Scheduler rule.
  The reconciler bundles `docs/aws-api/service-2.json` under `models/` and
  sets `AWS_DATA_PATH` (decision 8) to call `lambda-microvms:ListMicrovms`
  from a Lambda runtime that has never heard of that service.
- **Forwarder**: re-derives `k_sbx` from the stack secret, checks the event's
  `sandbox_id` against its own log stream, verifies the MAC
  (constant-time), writes idempotently.
- **Deliverer**: E2B-compatible signature (`e2b-signature` = base64 of
  `sha256(secret + payload)`, no padding), an SSRF guard (resolve, classify,
  connect to the checked address — never re-resolve), https-only, no
  redirects, ≤ 3 retries, deduplicated against DynamoDB Streams' at-least-
  once delivery.
- **Reconciler**: `rate(5 min)`, compares `ListMicrovms` against sandboxes
  the table still considers open, synthesizes `killed{unknown}` (not yet
  `timeout`: that needs per-sandbox max-duration tracking this iteration
  does not add), deterministic ids so re-running never duplicates.
- **SDK**: `LifecycleEvents`/`AsyncLifecycleEvents` (deploy/status/destroy
  over `OptionalStacks`; `register_webhook`/`list_webhooks`/`delete_webhook`/
  `get_events` direct on DynamoDB, usable today). `events=` on
  `Sandbox.create()` raises `UnimplementedError` naming this change, the
  same stub behaviour as the other six pending 0.6 options — see the gap
  above (D5): `LifecycleEvents._build_section` is implemented and
  unit-tested in isolation, but `create()` has nowhere yet to send the
  `ConfigureSandbox` section it builds. CLI `rayito events
  deploy|status|destroy|list` and `webhook add|list|remove`.
- **TypeScript mirror**: `src/lifecycle-events/{domain,keys,section,dynamodb,service}.ts`,
  `LifecycleEvents` (one async class).

## Impact

- **Rust**: `crates/rayd-core/src/lifecycle_events/{mod,event,mac,emit}.rs`,
  `crates/rayd/src/{features/lifecycle_events.rs,adapters/stdout_event_sink.rs}`,
  minimal additive edits to `crates/rayd/src/{features/mod.rs,grpc/health.rs,
  grpc/configure.rs,hooks/mod.rs}` and `Cargo.toml`/`crates/rayd-core/Cargo.toml`
  (`hmac`), `proto/rayito/v1/lifecycle_events.proto`.
- **Python**: `_lifecycle_events/` (new package), `_feature_options.py`
  (events branch, still `UnimplementedError`), `_stacks/components/events_webhooks.py`,
  `cli/events.py`, `__init__.py`. No edit to `sandbox_{sync,async}/main.py`
  (D5): `events=` stops at `plan_features`, same as the other six pending
  0.6 options.
- **TypeScript**: `src/lifecycle-events/`, `feature-options.ts`,
  `stacks/components/events-webhooks.ts`, `index.ts`. No edit to
  `sandbox/sandbox.ts`, for the same reason.
- **Infra**: `infra/events-webhooks.yaml`, `infra/lambdas/events_webhooks/`.
- **Scripts**: `scripts/gen_stack_assets.py` (TS generator line-width fix,
  needed for any Lambda-bearing component, not only this one).
- **No change** to 0.5.x behaviour without `events=`/`LifecycleEvents`: no
  new AWS client, no `ConfigureSandbox` call, no stdout line.

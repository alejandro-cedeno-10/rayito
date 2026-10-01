## Context

M15 foundations (`v06-foundations`) built `ConfigureSandbox`, `AgentFeatures`
and the `LifecycleParticipant`/`FeatureSet` scaffolding for eight parallel
features, but every slot shipped as `slot::Unsupported`, and the wiring
between `ConfigureService` (gRPC) and the hooks listener (`/suspend`,
`/resume`, `/ready`, `/terminate`) was left as two *independent* `FeatureSet`
instances — workable only because every slot was stateless. This is the
first feature with real, mutable, per-process state (a pushed key), so it is
the first to hit that seam.

## Decisions

- **D1 — A process-wide singleton, not a widened `Services`/`HookServices`.**
  `features::lifecycle_events::shared_inner()` is a `OnceLock<Arc<Inner>>`:
  every call to `build()` (one inside `grpc::router_with_transfers`, one
  inside `main.rs` for `HookServices.participants`, one inside the
  integration test harness) hands back a thin wrapper over the *same*
  `Inner`. There is exactly one sandbox per `rayd` process, so this is a
  singleton-per-agent, not global mutable state in the general sense.
  **Rejected alternative**: add `features: Arc<FeatureSet>` to
  `grpc::Services`. This looked cleaner but has 9 construction call sites
  across `main.rs` and eight unrelated milestones' integration test files
  (`m1_hello.rs` .. `m9_network.rs`) — a blast radius far out of proportion
  to what this feature needs, and exactly the kind of cross-feature
  collision the M15 plan's "shared-file protocol" exists to avoid. The
  singleton gets the same observable behaviour (one shared state) with a
  change confined to this feature's own file.
- **D2 — `rayd` never derives `k_sbx`.** The SDK computes
  `HMAC-SHA256(stack_key, "rayito.events.v1|" + sandbox_id)` and pushes only
  the result through `ConfigureSandbox`; the stack-wide secret never reaches
  the guest. A compromised sandbox can therefore only forge or replay its
  own events, never another sandbox's — enforced a second time by the
  forwarder checking the event's `sandbox_id` against its own log stream.
- **D3 — `created`/`paused`/`resumed`/`killed` only; `timeout` is reconciler-
  only.** `rayd` only ever reports `killed{reason: request}` (it knows this
  happened because `/terminate` was called); `timeout`/`unknown` only come
  from the reconciler comparing `ListMicrovms` against the table. This
  iteration's reconciler always reports `unknown`: distinguishing `timeout`
  needs per-sandbox max-duration tracking this change does not add (listed
  as a follow-up, not silently done wrong).
- **D4 — Single DynamoDB table, sparse GSI for "every sandbox".** One table
  (`EVENT#<sandbox_id>`/`STATE#<sandbox_id>`/`WEBHOOK`/`DELIVERY#<event_id>`
  partitions) keeps the stack to one resource instead of three, and a sparse
  GSI (`gsi1pk="EVENT"` only on event rows) makes `get_events(sandbox_id=None)`
  a single index query instead of a table scan.
- **D5 — Known integration gap, documented rather than forced.**
  `create()`'s actual dispatch of `FeaturePlan.configure_sections` into a
  `Configure` RPC call does not exist yet anywhere in the SDK (not only for
  this feature): `plan_features()` runs before `run-microvm`, so it cannot
  build a section that needs `sandbox_id`. The missing piece — in
  `sandbox_{sync,async}/main.py` and `sandbox/sandbox.ts`, both files this
  change does not otherwise touch beyond one kwarg each — is named exactly
  in `_feature_options.plan_features`'s docstring and in `ADR-020`, so
  wiring it is a plumbing change, not a design one, whenever that lands
  (here or in a later feature that also needs it).

## Risks

- **Log-stream-naming assumption (CP-5).** The forwarder's "sandbox_id
  appears in the log stream name" check is not measured against a real
  Lambda MicroVM's CloudWatch log stream yet — flagged in `AWS_API_NOTES.md`
  §25 as the first thing the AWS acceptance stage should confirm.
- **Reconciler only ever reports `unknown`, never `timeout`.** A consumer
  distinguishing the two in `get_events()`/webhook payloads will see fewer
  `timeout` events than the architecture originally described; documented
  in the proposal and `docs-delta.md`, not hidden.

## Migration

None: no existing behaviour changes, and every new surface is reached only
through `events=`/`LifecycleEvents()`, never implicitly.

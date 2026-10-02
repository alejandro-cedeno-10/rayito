## Context

M15 foundations (`v06-foundations`) built `ConfigureSandbox`, `AgentFeatures`
and the `LifecycleParticipant`/`FeatureSet` scaffolding for eight parallel
features, with every slot shipped as `slot::Unsupported`. This is the first
feature with real, mutable, per-process state (a pushed key), so it is the
first that needs `ConfigureService` and the hooks listener to act on the
same `FeatureSet`, and the first participant with work on `/resume` and
`/terminate`.

## Decisions

- **D1 — One `FeatureSet` per process, wired by foundations.** `main` builds
  one `Arc<FeatureSet>` and shares it between `ConfigureService`, `Health`
  and `HookServices.participants`; the hooks call `on_suspend`/`on_resume`/
  `on_terminate` once per accepted transition, each under its own cap
  (`v06-foundations` §15, PR #87). This feature only fills its own slot.
  **Rejected alternative** (the first iteration of this change): a
  process-wide `OnceLock` singleton behind every `build()`. It avoided the
  shared files, but every `#[tokio::test]` in one integration binary shared
  one state and one drain task spawned on whichever test's runtime ran
  first, so later tests silently lost events and leaked keys.
- **D1b — Flush before the VM freezes or the process exits.** A queued line
  is only on stdout once the drain task runs; nothing guarantees that before
  `/suspend` answers (the VM freezes) or `/terminate` answers (the process
  exits). `LifecycleEventSink::flush` puts a barrier on the same FIFO
  channel, acknowledged after the lines ahead of it are written and stdout
  is flushed. `on_suspend` waits for it up to its share, `on_terminate` up
  to `hooks::PARTICIPANT_TERMINATE_TIMEOUT`; with no key configured nothing
  is queued and nothing waits.
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
- **D4b — State only moves forward; deliveries are never lost.** The
  forwarder updates `STATE#` only for a newly written event and only when it
  is not older than the recorded one (a conditional write on
  `last_seen_ms`); `killed` stays as a tombstone with the events' TTL, so a
  late or duplicate line cannot reopen a sandbox and make the reconciler
  synthesize a second `killed`. The deliverer records a `delivery_status`
  per (event, webhook): `attempting` before the first attempt,
  `delivered`/`failed` after; only `delivered` is skipped, so a crash, a
  timeout or an unreadable secret never loses a delivery. Attempts and
  backoff are fitted into `context.get_remaining_time_in_millis()`; when
  time runs out the record is reported in `batchItemFailures` and the stream
  retries from it (records that exhaust the stream's retries go to an SQS
  queue). Only 5xx and transport errors are retried.
- **D5 — Known integration gap: `events=` validates, then stays
  `UnimplementedError`.** `create()`'s dispatch of
  `FeaturePlan.configure_sections` into a `Configure` RPC does not exist yet
  anywhere in the SDK: `plan_features()` runs before `run-microvm`, so it
  cannot build a section that needs `sandbox_id`, and the send needs the
  gRPC stub that only exists after the first `Health` inside `_open`.
  Accepting `events=` and never sending its key would leave the caller
  paying for the stack and believing events flow. So `events=` checks what
  it can before launch (a `LifecycleEvents`/`AsyncLifecycleEvents`, and a
  `logging` the shared resolver maps to `cloudWatch`) and then raises
  `UnimplementedError` naming the missing send. `create()` passes its
  `logging` to `plan_features` through foundations' seam (PR #87).
  `register_webhook`/`get_events`/`deploy`/`destroy` are unaffected and
  usable today.

## Risks

- **Log-stream naming (CP-5), measured.** The stream is
  `YYYY/MM/DD[<imageVersion>]<microvmId>` (`AWS_API_NOTES.md` Q106); the
  forwarder now requires it to end in `]<sandbox_id>` instead of the
  original substring check.
- **A sandbox killed while suspended gets no `/terminate`** (Q105): its
  `killed{unknown}` comes only from the reconciler, up to one interval
  late. A run-time `maximumDurationInSeconds` expiry *does* reach
  `/terminate`, reported as `killed{request}` (the hook carries no reason).
- **Reconciler only ever reports `unknown`, never `timeout`.** A consumer
  distinguishing the two in `get_events()`/webhook payloads will see fewer
  `timeout` events than the architecture originally described; documented
  in the proposal and `docs-delta.md`, not hidden.

## Migration

None: no existing behaviour changes, and every new surface is reached only
through `events=`/`LifecycleEvents()`, never implicitly.

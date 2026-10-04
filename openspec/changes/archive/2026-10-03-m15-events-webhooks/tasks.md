## 1. rayd: domain and emission

- [x] 1.1 `crates/rayd-core/src/lifecycle_events/{mod,event,mac,emit}.rs`: `LifecycleEvent`/`EventKind`/`KillReason`, `compute_mac` (HMAC-SHA256, panic-free per the workspace's `expect_used`/`unwrap_used` deny), `format_event_line`, `LifecycleEventSink` port, shared constants (`SUSPEND_SHARE_MAX`, `EVENT_QUEUE_CAPACITY`, `DOMAIN_SEPARATOR`, `PARTICIPANT_NAME`). `hmac` added to the workspace and `rayd-core`'s `Cargo.toml` (foundations' own comment said it was already wired; it was not — added here).
- [x] 1.2 `proto/rayito/v1/lifecycle_events.proto`: `sandbox_key`, `sandbox_id`, `image_arn`, `image_version` on the config; `emitted`/`dropped`/`last_error_class` on the status. Regenerated with `buf generate` (touches only this feature's `*_pb2*`/`*_pb.ts`).
- [x] 1.3 `crates/rayd/src/adapters/stdout_event_sink.rs`: bounded `tokio::mpsc` channel, one background task, one `writeln!` per line under the stdout lock; `flush()` (port `LifecycleEventSink::flush`) is a barrier on the same channel, acknowledged after the lines ahead of it are written and stdout is flushed.
- [x] 1.4 `crates/rayd/src/features/lifecycle_events.rs`: real adapter, always `supported()`; `apply`/`status`/`participant` (`on_suspend`/`on_resume`/`on_terminate` emit `paused`/`resumed`/`killed{request}`); `on_suspend` waits for the flush up to its share and `on_terminate` up to `hooks::PARTICIPANT_TERMINATE_TIMEOUT`; a failed random source drops the event (`random_unavailable`) instead of reusing an id. State lives in the slot `build()` returns (no singleton).
- [x] 1.5 `Health.features.lifecycle_events` comes from the slot's own `supported()` (`FeatureSet::agent_features`, foundations PR #87); no edit to `grpc/health.rs` here.
- [x] 1.6 `/resume`/`/terminate` participant dispatch, one `FeatureSet` per process and the per-participant caps live in foundations (`v06-foundations` §15, PR #87); this change merges that branch and adds no edit of its own to `main.rs`, `grpc/mod.rs`, `hooks/mod.rs` or `tests/common`.
- [x] 1.7 Unit tests: MAC/emit vectors against `testdata/lifecycle-events/mac-vectors.json` (shared with Python/TS), created-once semantics, invalid section, cleared section stops events, resume bumps generation, terminate emits `killed`, full queue counts as dropped not an error, participant demand matches the domain constant, suspend/terminate wait for the flush up to their bound and no longer (stuck fake sink, paused clock), an unconfigured slot never flushes, a failed random source drops the event, two built slots never share state. `cargo test --workspace` and `cargo clippy --workspace --all-targets -- -W clippy::pedantic` clean in the Lima VM.

## 2. Infra and Lambdas

- [x] 2.1 `infra/events-webhooks.yaml`: HMAC secret, DynamoDB table (streams, GSI1), forwarder/deliverer/reconciler Lambdas + roles, Logs subscription, EventBridge Scheduler, operator policy; `scripts/gen_stack_assets.py` discovers it automatically (no edit needed there beyond the TS line-width fix in 2.4).
- [x] 2.2 `infra/lambdas/events_webhooks/`: `domain/{event,mac,signature,ssrf,dedupe,schema,forwarding,delivery}.py` (pure), `ports.py`, `adapters/{dynamodb,secrets,http_client,microvms}.py`, `handlers/{forwarder,deliverer,reconciler}.py`. The `lambda-microvms` model is injected into the zip at generation time from `docs/aws-api/service-2.json` (decision 8), never vendored; the reconciler builds its client from a dedicated botocore session pointed at it, and the template sets `AWS_DATA_PATH`.
- [x] 2.2a Deliverer: `delivery_status` claim (`attempting` → `delivered`/`failed`, only `delivered` skipped), retries only for 5xx/transport errors, 64 KiB response cap, attempts fitted into the remaining invocation time with `batchItemFailures` (`ReportBatchItemFailures`), `BisectBatchOnFunctionError` and an SQS on-failure destination. It reads no stack key.
- [x] 2.2b Forwarder/reconciler: `STATE#` only moves forward and only for a newly written event; `killed` is a tombstone with TTL; the synthesized `killed` copies generation and image from `STATE#` (no `ReconcilerImage*` parameters); its dedupe window is `RECONCILER_INTERVAL_MINUTES`.
- [x] 2.2c Template: `ReconcilerIntervalMinutes` minimum 2 (`rate(N minutes)`), default equal to the SDKs' constant; `EventsOperatorPolicy` grants `PutItem`/`Query`/`DeleteItem` on the table and `gsi1` and `DescribeStacks` on the stack; IAM action `lambda:ListMicrovms` (`AWS_API_NOTES.md` §10).
- [x] 2.3 `clients/python/src/rayito/_stacks/components/events_webhooks.py`: real `StackComponent` (parameters, artifact, cost statement). TS mirror `stacks/components/events-webhooks.ts`.
- [x] 2.4 `scripts/gen_stack_assets.py`: `render_typescript` now measures the first physical line against biome's line width before deciding whether `TEMPLATE_BODY`/`ARTIFACT_BASE64` go on the same line or their own — the first component with real Lambda code (this one) was the first to hit the single-line-too-long case; `--check` and `pnpm lint` both pass.
- [x] 2.5 Lambda unit tests (`infra/lambdas/events_webhooks/tests/`): MAC verification, sandbox/log-stream mismatch, SSRF classification (loopback/private/link-local/CGNAT/IMDS), E2B signature vectors, dedupe window math, adapter conditions (state forward-only, tombstone, delivery claims), handler tests for forwarder/deliverer/reconciler with fakes in exactly the template's environment, a template test that every `*_ENV` a handler reads is declared, the http client's single `Host` header and body cap, the bundled-model client from a fresh session. Run in CI next to `scripts/tests`.
- [x] 2.6 `scripts/gen_stack_assets.py`: the artifact is an allowlist (`.py` outside `tests/`, `__pycache__` and hidden directories) plus injected service models; `--check` runs in CI and passes from a clean checkout.

## 3. SDK (Python)

- [x] 3.1 `_lifecycle_events/{__init__,_domain,_keys,_section,_dynamodb,_service,_service_async}.py`: `LifecycleEvents`/`AsyncLifecycleEvents` (`deploy`/`status`/`destroy` over `OptionalStacks`; `register_webhook`/`list_webhooks`/`delete_webhook`/`get_events` direct on DynamoDB); `_build_section` implemented and unit-tested, not yet called by `create()`.
- [x] 3.2 `_feature_options.py`: `events=` validates (a `LifecycleEvents`/`AsyncLifecycleEvents`; `logging` that the shared resolver maps to `cloudWatch`) and then raises `UnimplementedError` naming the missing post-`run-microvm` `ConfigureSandbox` send (D5).
- [x] 3.2a `_lifecycle_events/_aws.py`: `EventsGateway` port and its boto3 adapter; every AWS error becomes `WebhookException(aws_code=...)` with a message-free sanitized cause. `get_events` validates `1 <= limit <= 100` and filters `types` in DynamoDB with pagination.
- [x] 3.3 `cli/events.py`: `deploy`/`status`/`destroy`/`list`, `webhook add`/`list`/`remove`; cost statement and what `destroy` removes printed through `cli/stack.py`'s `confirm_deploy`/`confirm_destroy`.
- [x] 3.4 `__init__.py` exports (`LifecycleEvents`, `AsyncLifecycleEvents`, `EventRecord`, `WebhookInfo`).
- [x] 3.5 Unit tests: `test_m15_events_webhooks_domain.py` (MAC/key derivation against the shared vectors), `test_m15_events_webhooks_service.py` (fakes for `OptionalStacks`/DynamoDB/Secrets Manager: deploy/status/destroy, register/list/delete webhook, get_events filtering, pagination and limits, sanitized AWS errors, binary stack key, `_build_section`), `test_m15_events_webhooks_feature_options.py` (type and logging validation, then `UnimplementedError` before any control plane).
- [x] 3.6 Off-by-default: `events=None`/no `LifecycleEvents()` constructed builds no DynamoDB/Secrets Manager/CloudFormation client and sends no `ConfigureSandbox` call (extends `test_m15_zero_cost.py`'s existing guarantee; no edit to that shared file needed since the golden trace already has no 0.6 option set).

## 4. SDK (TypeScript)

- [x] 4.1 `src/lifecycle-events/{domain,keys,section,dynamodb,service}.ts`: `LifecycleEvents` (one async class).
- [x] 4.2 `feature-options.ts` + `lifecycle-events/options.ts`: same validation, then `UnimplementedError` (D5), mirroring the Python side. `listWebhooks` paginates; `awsCall` turns every AWS SDK error into `WebhookError` with only its code; `getEvents` validates `1..100`.
- [x] 4.3 `stacks/components/events-webhooks.ts`, `index.ts` exports.
- [x] 4.4 Unit tests: `m15-events-webhooks-domain.test.ts` (shared MAC vectors), `m15-events-webhooks-service.test.ts` (fake DynamoDB/Secrets Manager/stacks clients, pagination, limits, sanitized errors), `m15-events-webhooks-feature-options.test.ts` (validation, then `UnimplementedError`).
- [x] 4.5 `pnpm typecheck` and `pnpm lint` clean.

## 5. OpenSpec, docs and changelogs

- [x] 5.1 This change (`openspec/changes/m15-events-webhooks/`), capability `lifecycle-events`; `npx -y @fission-ai/openspec@1.10.0 validate --strict` passes.
- [x] 5.2 `ARCHITECTURE.md` ADR-020 filled (including the "Known integration gap" note); `AWS_API_NOTES.md` §25 filled; `MILESTONES.md`'s events-webhooks bullet expanded.
- [x] 5.3 CHANGELOG anchors filled in all three `CHANGELOG.md` (`crates/rayd`, `clients/python`, `clients/typescript`).
- [x] 5.4 `docs/site/docs/funciones-opcionales/eventos-y-webhooks.md` filled (Coste y activación, Python+TS tabs).
- [x] 5.5 `docs-delta.md` written (exact replacement rows for e2b-parity, optional-features, cost, security, errores, variables-de-entorno) — applied by `m15-docs-integration`, not here.

## 6. AWS acceptance (separate, serialized stage — not run by this agent)

- [x] 6.1 CP-4 (Q105: RUNNING yes, also on max-duration expiry as `killed{request}`; SUSPENDED no, reconciler covers it): does `/terminate` arrive for a VM in RUNNING and in SUSPENDED (max duration 180 s)?
- [x] 6.2 CP-5 (Q106: `paused` ingested 226 ms after emission, before the freeze; stream `YYYY/MM/DD[<imageVersion>]<microvmId>`, forwarder check tightened): do lines written inside `/suspend` reach CloudWatch, and at what latency to the Lambda? Also confirms/corrects the forwarder's log-stream-naming assumption (§25).
- [x] 6.3 End-to-end (2026-10-02; the platform timeout came through `/terminate`, the reconciler path was exercised by a kill while suspended; forwarder/reconciler summary log lines added, Q107; `--tag` for tag-policy accounts, Q108): deploy the stack, a Function URL receiver, create → pause → resume → kill, 4 signed deliveries, a forged unsigned line dropped, a platform timeout synthesized by the reconciler, `get_events` ordering, destroy (secret force-deleted).
- [x] 6.4 Cleanup: remove the receiver and the stack; budget cap $0.50 (§8 of the architecture). (Done in the 2026-10-02 acceptance: stack, receiver Lambda and its URL/role, webhook secrets, image and log groups deleted, before/after inventory identical; estimated cost under $0.20.)

## 7. `create(events=...)` wiring (D6)

- [x] 7.1 Python: `FeaturePlan.events`, `LaunchFacts.sandbox_id`, `planned_sections` appends `LifecycleEventsSectionFactory` (`_lifecycle_events/_section.py`); `LifecycleEventsSection` is an `ImmediateSection` with `sandbox_key` out of `repr`; `validate_events_option` returns the sync `LifecycleEvents` (`AsyncLifecycleEvents._sync_events`); `EVENTS_PENDING_REASON` removed.
- [x] 7.2 TypeScript: same (`FeaturePlan.events`, `LaunchFacts`, `LifecycleEventsSection` class with `requiredFlag = "lifecycleEvents"` and `k_sbx` in an ES private field, `LifecycleEventsSectionFactory`); `SandboxCreateOptions.events` typed `LifecycleEvents`.
- [x] 7.3 Unit tests: `test_m15_events_create_wiring.py` / `m15-events-create-wiring.test.ts` (planning, derived key in the single `Configure`, stack key read once per instance, terminate on no flag / pre-0.6 agent / undeployed stack / `INVALID`); the feature-options tests now assert the option is planned.
- [x] 7.4 Docs: `eventos-y-webhooks.md` (gap warning removed, Python+TS `create(events=...)` example, error rows), ADR-020, `MILESTONES.md`, `RELEASE_NOTES_0.6.0.md`, `docs-delta.md` row, CHANGELOGs.

## 1. rayd: domain and emission

- [x] 1.1 `crates/rayd-core/src/lifecycle_events/{mod,event,mac,emit}.rs`: `LifecycleEvent`/`EventKind`/`KillReason`, `compute_mac` (HMAC-SHA256, panic-free per the workspace's `expect_used`/`unwrap_used` deny), `format_event_line`, `LifecycleEventSink` port, shared constants (`SUSPEND_SHARE_MAX`, `EVENT_QUEUE_CAPACITY`, `DOMAIN_SEPARATOR`, `PARTICIPANT_NAME`). `hmac` added to the workspace and `rayd-core`'s `Cargo.toml` (foundations' own comment said it was already wired; it was not — added here).
- [x] 1.2 `proto/rayito/v1/lifecycle_events.proto`: `sandbox_key`, `sandbox_id`, `image_arn`, `image_version` on the config; `emitted`/`dropped`/`last_error_class` on the status. Regenerated with `buf generate` (touches only this feature's `*_pb2*`/`*_pb.ts`).
- [x] 1.3 `crates/rayd/src/adapters/stdout_event_sink.rs`: bounded `tokio::mpsc` channel, one background task, one `writeln!` per line under the stdout lock.
- [x] 1.4 `crates/rayd/src/features/lifecycle_events.rs`: real adapter, always `supported()`; `apply`/`status`/`participant` (`on_suspend`/`on_resume`/`on_terminate` emit `paused`/`resumed`/`killed{request}`); a process-wide singleton (`shared_inner`) so the gRPC-side `FeatureSet` (`ConfigureGrpc.apply`) and the hooks-side one (`HookServices.participants`) share state without widening `grpc::Services`/`hooks::HookServices` across eight other milestones' test files.
- [x] 1.5 `grpc::health`'s one hard-coded `AgentFeatures::foundations_only()` call site updated to also report `lifecycle_events: true` (the slot foundations' own comment named as the place to do this).
- [x] 1.6 `hooks::mod`: `on_resume`/`on_terminate` now run every participant (`run_participants_on_resume`/`_on_terminate`), mirroring the existing `on_suspend` wiring — additive, empty-participants-by-default, no change to any other milestone's test.
- [x] 1.7 Unit tests: MAC/emit vectors against `testdata/lifecycle-events/mac-vectors.json` (shared with Python/TS), created-once semantics, invalid section, cleared section stops events, resume bumps generation, terminate emits `killed`, full queue counts as dropped not an error, participant demand matches the domain constant. `cargo test --workspace` and `cargo clippy --workspace --all-targets -- -W clippy::pedantic` clean in the Lima VM.

## 2. Infra and Lambdas

- [x] 2.1 `infra/events-webhooks.yaml`: HMAC secret, DynamoDB table (streams, GSI1), forwarder/deliverer/reconciler Lambdas + roles, Logs subscription, EventBridge Scheduler, operator policy; `scripts/gen_stack_assets.py` discovers it automatically (no edit needed there beyond the TS line-width fix in 2.4).
- [x] 2.2 `infra/lambdas/events_webhooks/`: `domain/{event,mac,signature,ssrf,dedupe,schema,forwarding}.py` (pure), `ports.py`, `adapters/{dynamodb,secrets,http_client,microvms}.py`, `handlers/{forwarder,deliverer,reconciler}.py`. `models/lambda-microvms/2025-09-09/service-2.json` bundled for the reconciler's `AWS_DATA_PATH` (decision 8).
- [x] 2.3 `clients/python/src/rayito/_stacks/components/events_webhooks.py`: real `StackComponent` (parameters, artifact, cost statement). TS mirror `stacks/components/events-webhooks.ts`.
- [x] 2.4 `scripts/gen_stack_assets.py`: `render_typescript` now measures the first physical line against biome's line width before deciding whether `TEMPLATE_BODY`/`ARTIFACT_BASE64` go on the same line or their own — the first component with real Lambda code (this one) was the first to hit the single-line-too-long case; `--check` and `pnpm lint` both pass.
- [x] 2.5 Lambda domain unit tests (`infra/lambdas/events_webhooks/tests/`): MAC verification, sandbox/log-stream mismatch, SSRF classification (loopback/private/link-local/CGNAT/IMDS), E2B signature vectors, dedupe window math, deliverer handler against only the env vars the template sets, the http client's single-`Host`-header request. Wired into `scripts/tests`' `test-scripts` Makefile target.

## 3. SDK (Python)

- [x] 3.1 `_lifecycle_events/{__init__,_domain,_keys,_section,_dynamodb,_service,_service_async}.py`: `LifecycleEvents`/`AsyncLifecycleEvents` (`deploy`/`status`/`destroy` over `OptionalStacks`; `register_webhook`/`list_webhooks`/`delete_webhook`/`get_events` direct on DynamoDB); `_build_section` implemented and unit-tested, not yet called by `create()`.
- [x] 3.2 `_feature_options.py`: `events=` stays `UnimplementedError` (D5 — no edit to `sandbox_{sync,async}/main.py`, same stub shape as the other six 0.6 options until the shared post-`run-microvm` `Configure` dispatch lands).
- [x] 3.3 `cli/events.py`: `deploy`/`status`/`destroy`/`list`, `webhook add`/`list`/`remove`.
- [x] 3.4 `__init__.py` exports (`LifecycleEvents`, `AsyncLifecycleEvents`, `EventRecord`, `WebhookInfo`).
- [x] 3.5 Unit tests: `test_m15_events_webhooks_domain.py` (MAC/key derivation against the shared vectors), `test_m15_events_webhooks_service.py` (fakes for `OptionalStacks`/DynamoDB/Secrets Manager: deploy/status/destroy, register/list/delete webhook, get_events filtering and ordering, `_build_section`); `events=`'s stub behaviour is covered by the shared `test_m15_feature_options.py`/`test_m15_create_kwargs.py` tables, parity with the other six options.
- [x] 3.6 Off-by-default: `events=None`/no `LifecycleEvents()` constructed builds no DynamoDB/Secrets Manager/CloudFormation client and sends no `ConfigureSandbox` call (extends `test_m15_zero_cost.py`'s existing guarantee; no edit to that shared file needed since the golden trace already has no 0.6 option set).

## 4. SDK (TypeScript)

- [x] 4.1 `src/lifecycle-events/{domain,keys,section,dynamodb,service}.ts`: `LifecycleEvents` (one async class).
- [x] 4.2 `feature-options.ts`: `events` stays `UnimplementedError` (D5 — no edit to `sandbox/sandbox.ts`), mirroring the Python side.
- [x] 4.3 `stacks/components/events-webhooks.ts`, `index.ts` exports.
- [x] 4.4 Unit tests: `m15-events-webhooks-domain.test.ts` (shared MAC vectors), `m15-events-webhooks-service.test.ts` (fake DynamoDB/Secrets Manager/stacks clients); `events`'s stub behaviour is covered by the shared `m15-feature-options.test.ts` table, parity with the other six options.
- [x] 4.5 `pnpm typecheck` and `pnpm lint` clean.

## 5. OpenSpec, docs and changelogs

- [x] 5.1 This change (`openspec/changes/m15-events-webhooks/`), capability `lifecycle-events`; `npx -y @fission-ai/openspec@1.10.0 validate --strict` passes.
- [x] 5.2 `ARCHITECTURE.md` ADR-020 filled (including the "Known integration gap" note); `AWS_API_NOTES.md` §25 filled; `MILESTONES.md`'s events-webhooks bullet expanded.
- [x] 5.3 CHANGELOG anchors filled in all three `CHANGELOG.md` (`crates/rayd`, `clients/python`, `clients/typescript`).
- [x] 5.4 `docs/site/docs/funciones-opcionales/eventos-y-webhooks.md` filled (Coste y activación, Python+TS tabs).
- [x] 5.5 `docs-delta.md` written (exact replacement rows for e2b-parity, optional-features, cost, security, errores, variables-de-entorno) — applied by `m15-docs-integration`, not here.

## 6. AWS acceptance (separate, serialized stage — not run by this agent)

- [ ] 6.1 CP-4: does `/terminate` arrive for a VM in RUNNING and in SUSPENDED (max duration 180 s)?
- [ ] 6.2 CP-5: do lines written inside `/suspend` reach CloudWatch, and at what latency to the Lambda? Also confirms/corrects the forwarder's log-stream-naming assumption (§25).
- [ ] 6.3 End-to-end: deploy the stack, a Function URL receiver, create → pause → resume → kill, 4 signed deliveries, a forged unsigned line dropped, a platform timeout synthesized by the reconciler, `get_events` ordering, destroy (secret force-deleted).
- [ ] 6.4 Cleanup: remove the receiver and the stack; budget cap $0.50 (§8 of the architecture).

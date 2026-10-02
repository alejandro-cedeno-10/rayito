## 1. Proto (owned fields inside the foundations-created stub file)

- [x] 1.1 `proto/rayito/v1/telemetry_export.proto`: real `TelemetryExportConfig`
      (interval_s, service_name, names, image_arn, image_version,
      image_memory_mib, oneof auth { execution_role, bearer }),
      `TelemetryExportExecutionRoleAuth`, `TelemetryExportBearerAuth`,
      `TelemetryExportStatus`, `TelemetryExportNameStyle` -- every message
      and enum carries the feature's own prefix (architecture §3), fixed
      after review from the initial unprefixed `ExecutionRoleAuth`/
      `BearerAuth`/`NameStyle`.
- [x] 1.2 `buf generate` regenerates Python/TypeScript gencode for this file
      only (verified: `git status` showed no other generated file changed).

## 2. Vendored OTLP proto (the one exception the shared-file protocol allows)

- [x] 2.1 `crates/rayito-proto/vendor/opentelemetry/proto/{common,resource,metrics,collector/metrics}/v1/*.proto`:
      minimal subset, real upstream field numbers (design.md D6).
- [x] 2.2 `crates/rayito-proto/src/lib.rs`: `pub mod otlp` exposing the
      vendored types, nested to mirror the real package hierarchy.

## 3. rayd-core domain

- [x] 3.1 `telemetry/config.rs`: `TelemetryConfig::validate`, `TelemetryAuth`,
      `MIN_INTERVAL`/`MAX_INTERVAL` (15-300 s), `TelemetryConfigError`.
- [x] 3.2 `telemetry/model.rs`: `GaugeKind` (7), `AttrKey` (4), `MetricPoint`,
      `ResourceAttrs`, `NameStyle`, `points_from_sample`.
- [x] 3.3 `telemetry/batcher.rs`: `Batcher` (bounded, drop-oldest),
      `jittered_backoff` (over the existing `code::ports::RandomSource`
      port), `plan_suspend_flush`, `TelemetrySink`/`OtlpEncoder` ports.
- [x] 3.4 `metrics_history.rs`: `MetricsHistory::latest()`/`MetricsRing::latest()`.
- [x] 3.5 `lib.rs`: `pub mod telemetry;`.

## 4. rayd adapters

- [x] 4.1 `adapters/aws_sigv4.rs`: SigV4 signer, RFC 4231 + AWS worked-example
      tests (design.md D5).
- [x] 4.2 `adapters/otlp_codec.rs`: `OtlpEncoder` impl over the vendored types.
- [x] 4.3 `adapters/cloudwatch_otlp_sink.rs`: `TelemetrySink` impl, dedicated
      `hyper-rustls` client, gzip, `FilteringResolver` reuse.

## 5. rayd wiring

- [x] 5.1 `features/telemetry_export.rs`: real `ConfigurableFeature` +
      `LifecycleParticipant` (≤ 2 s `/suspend` flush), `Unsupported` without
      a known region.
- [x] 5.2 `features/mod.rs`: `FeatureContext` fields (design.md D3),
      `FeatureSet::{agent_features, root_egress, participants}`.
- [x] 5.3 `grpc/health.rs`: `HealthGrpc::with_features` (design.md D2).
- [x] 5.4 `grpc/mod.rs`: `router_with_features` (design.md D1); the three
      existing `router*` functions unchanged in behavior.
- [x] 5.5 `grpc/configure.rs`: clone (not move) `TelemetryExportConfig` out
      of the shared request (it is no longer `Copy`, unlike its five
      sibling stub messages).
- [x] 5.6 `main.rs`: `build_feature_set` (extracted to stay under
      `clippy::too_many_lines`), threads the real `FeatureSet` into
      `router_with_features` and `HookServices.participants`.
- [x] 5.7 `Cargo.toml`: `prost` added to `rayd`'s own dependencies (already a
      workspace dependency via `rayito-proto`, not a new crate).

## 6. Gates: Rust

- [x] 6.1 `cargo fmt` clean.
- [x] 6.2 `cargo clippy --workspace --all-targets -- -D warnings` clean.
- [x] 6.3 `cargo test --workspace` green inside the Lima VM (587 `rayd-core`
      + 254 `rayd` unit tests + all integration suites, 0 failures).

## 7. Python

- [x] 7.1 `_telemetry_export/{__init__,_domain,_section,_propagation}.py`.
- [x] 7.2 `_feature_options.py`: real `telemetry` branch, `FeaturePlan.telemetry`.
- [x] 7.3 `_transport.py`: `CallMetadataProvider` seam (additive, default
      `()`, zero behavior change — see proposal.md's non-blocking follow-up).
- [x] 7.4 `sandbox_sync/main.py` / `sandbox_async/main.py`: `_apply_telemetry`
      (mirrors `_apply_initial_network`'s terminate-on-failure gate),
      `get_telemetry_status()`, `Sandbox.create()`'s "Coste y activación" block.
- [x] 7.5 `__init__.py`: exports `TelemetryExport`, `OtlpAuth`, `TelemetryHealth`.
- [x] 7.6 `_stacks/components/otlp_export.py`: real `StackComponent`.
- [x] 7.7 `infra/otlp-export.yaml` + `scripts/gen_stack_assets.py` regenerated
      (`--check` clean).

## 8. Gates: Python

- [x] 8.1 `uv run ruff check` clean.
- [x] 8.2 `uv run ruff format --check` clean.
- [x] 8.3 `uv run mypy` clean (new/changed files).
- [x] 8.4 `uv run pytest` green (2564 passed, 0 failed, 116 deselected e2e).

## 9. TypeScript

- [x] 9.1 `configure/{base,rpc}.ts` (foundations' Python-only seam, built
      here for TypeScript too).
- [x] 9.2 `telemetry-export/{domain,section}.ts`.
- [x] 9.3 `feature-options.ts`: real `telemetry` branch, `FeaturePlan.telemetry`.
- [x] 9.4 `sandbox/sandbox.ts`: `#applyTelemetry`, `getTelemetryStatus()`,
      `SandboxCreateOptions.telemetry`'s TSDoc "Coste y activación" block.
- [x] 9.5 `index.ts`: exports.
- [x] 9.6 `stacks/components/otlp-export.ts`: real component.
- [x] 9.7 `scripts/check-dts-cost-blocks.mjs`: two new registered
      declarations (design.md D9).

## 10. Gates: TypeScript

- [x] 10.1 `pnpm lint` clean.
- [x] 10.2 `pnpm typecheck` clean.
- [x] 10.3 `pnpm test` green (1131 passed, 0 failed).
- [x] 10.4 `pnpm pack:check` clean (7 cost declarations found).

## 11. Docs and OpenSpec

- [x] 11.1 `docs/site/docs/funciones-opcionales/exportacion-otlp.md`: full guide.
- [x] 11.2 `docs/site/docs/referencia/python/opcionales.md`: new section.
- [x] 11.3 `ARCHITECTURE.md` ADR-021, `AWS_API_NOTES.md` §26, `MILESTONES.md`.
- [x] 11.4 Three CHANGELOGs' `<!-- m15-rayd-otlp -->` anchors replaced.
- [x] 11.5 `docs/RELEASE_NOTES_0.6.0.md` section.
- [x] 11.6 `docs-delta.md` (this change's directory) for the shared tables.
- [x] 11.7 `cd clients/python && uv run --group docs mkdocs build -f ../../docs/site/mkdocs.yml --strict` clean.
- [x] 11.8 `npx -y @fission-ai/openspec@1.10.0 validate --strict` clean.

## 12. Not done in this change (by design)

- [ ] `traceparent` live wiring into the gRPC channel, in **both** SDKs
      (proposal.md's non-blocking follow-up, design.md D8) -- narrowed by
      §13 below: `rayd`'s own half (reading an inbound `traceparent`) is
      now done, and TypeScript now has its own tested, disconnected
      provider too (parity with Python); what remains deferred in both
      SDKs is specifically connecting the provider to the live channel's
      auth plugin/interceptor, plus a `CallMetadataProvider`-equivalent
      seam on the TypeScript transport (which still has none).
- [ ] AWS acceptance (OT1-OT12 Q-numbers): this change does not touch AWS;
      the serialized acceptance stage runs separately (see `aws_plan` in
      the delivery report).

## 13. Post-review fixes

A critical/high/medium/low review of the first version of this change
(PR #81) found the items below; all are fixed here except the one
explicitly deferred further (noted).

- [x] 13.1 `adapters/cloudwatch_otlp_sink.rs`: `send_with` now actually
      sets `x-amz-date`/`x-amz-content-sha256`/`x-amz-security-token` on
      the request for `ExecutionRole` auth -- the signature `sign()`
      computed covered those headers, but they were never attached to the
      HTTP request, so CloudWatch rejected every execution-role export
      with 403. New `build_request` test asserts every signed header is
      present; a new test pins the official SigV4 test suite's
      `post-vanilla` known-answer vector (`aws_sigv4.rs`).
- [x] 13.2 `features/telemetry_export.rs`: `apply()` now probes
      `ExecutionRoleProbe` before starting anything, rejecting
      `role_not_permitted` (no execution role at all) or
      `credentials_unavailable` (any other broker failure) instead of
      accepting the section and letting every export fail silently
      (`credential_broker.rs`'s `classify` tells the two apart).
- [x] 13.3 `crates/rayd/src/grpc/request_context.rs` (new): a tower layer
      that parses an inbound W3C `traceparent` (never `grpc-trace-bin` nor
      `baggage`) and opens a `trace_id`/`span_id` `tracing` span for that
      one RPC, wired as the outermost layer in `grpc::router_with_features`.
      `clients/typescript/src/telemetry-export/propagation.ts` (new):
      `TraceparentProvider`, mirroring Python's one to one -- TypeScript
      had none before. Both remain disconnected from the live channel
      (§12's narrowed scope).
- [x] 13.4 `image_memory_mib`: both SDKs now divide the guest's
      4x-multiplied `memory_total_bytes`/`memoryTotalBytes` by
      `GUEST_MEMORY_MULTIPLIER` (`image_memory_mib_from_guest_bytes`/
      `imageMemoryMibFromGuestBytes`, new pure helpers) instead of passing
      the raw guest view through under a name that says image memory; the
      repeated `1024 * 1024` is now a named `BYTES_PER_MIB` constant.
- [x] 13.5 `features/telemetry_export.rs`: `spawn_exporter`/`flush` now
      share one `export_pending` helper, generic over the `TelemetrySink`
      port (driven by a fake sink in tests) instead of each calling
      `CloudWatchOtlpSink::send_with` directly; `FixedCredentialsSink` is
      used for real; dead `PushedCredentials` bearer-token bookkeeping
      (written, never read -- the sink already holds its own copy) is
      removed.
- [x] 13.6 `TelemetryParticipant::on_resume` now tears the running
      exporter down and respawns it with a fresh `CloudWatchOtlpSink`
      (fresh connection pool), instead of relying on hyper's 30 s
      pool-idle timer, which runs on the monotonic clock and does not
      advance while the `MicroVM` is suspended (OT5).
- [x] 13.7 `proto/rayito/v1/telemetry_export.proto`: `NameStyle`,
      `ExecutionRoleAuth`, `BearerAuth` renamed to `TelemetryExportNameStyle`,
      `TelemetryExportExecutionRoleAuth`, `TelemetryExportBearerAuth`
      (architecture §3's feature-prefix rule); gencode regenerated.
- [x] 13.8 `_apply_telemetry`/`#applyTelemetry` now honour
      `keep_on_failure`/`keepOnFailure` (`terminate_on_failure`/
      `terminateOnFailure`, as `persist=`/`#bindAndRestore` already do)
      instead of always terminating regardless -- the docs page already
      promised this; the docstrings didn't match the code.
- [x] 13.9 Python: the gate (`require_telemetry_support`) and section
      assembly are now a shared pure helper in `_section.py` instead of
      being copy-pasted between `Sandbox._apply_telemetry` and
      `AsyncSandbox._apply_telemetry`; the async variant now runs
      `build_section` (which may do a blocking `GetSecretValue`) through
      `asyncio.to_thread` instead of blocking the event loop directly.
      `_propagation.py`'s `TraceparentProvider` gets real unit tests
      (`test_m15_rayd_otlp_propagation.py`) -- this change's own
      `proposal.md` claimed it was already "unit-tested in isolation",
      which was not true before this fix.
- [x] 13.10 `crates/rayd-core/src/metrics_history.rs`: `MetricsRing::latest`'s
      own doc comment was wedged between `oldest_unix_ms`'s doc comment and
      its fn; moved back.
- [x] 13.11 `aws_sigv4.rs`/`cloudwatch_otlp_sink.rs` doc fixes: `aws-sigv4`
      is in fact a public crate already in `Cargo.lock` (the comment
      claimed otherwise); every timeout constant now names its source or
      rationale; `REQUEST_TIMEOUT` (6 s) is now below
      `EXPORT_ATTEMPT_TIMEOUT` (8 s) so it can actually fire first, instead
      of being dead code nested inside a shorter outer timeout.
- [x] 13.12 Docs: `exportacion-otlp.md` said exports happen "cada pocos
      segundos" (now names `interval_s`, 60 s default) and that
      `image_memory_mib` is "la vista del guest" (now correctly describes
      the declared-memory value §13.4 produces); added a note on why
      `get_telemetry_status()` is a separate method rather than
      `get_health().telemetry` (design.md D7 already explained the
      decision; the public docs page didn't mention it).

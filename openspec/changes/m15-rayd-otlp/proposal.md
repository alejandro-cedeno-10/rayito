## Why

Rayito has no way to see a sandbox's CPU, memory or disk from outside the
process that created it, except by calling `get_metrics_history()`
in-process. Teams that already watch their infrastructure in CloudWatch
have no way to put sandboxes on the same dashboards without building and
running their own forwarder. Research
(`docs/research/2026-10-e2b-out-of-scope.md` §6, row 108) evaluated five
designs; option B1 ("`rayd` → CloudWatch OTLP, SigV4-signed") is the only
one that exports without Rayito hosting any plane of its own, and the
critic's B1' variant (a bearer token instead of the execution role) avoids
handing `rayito-base` sandboxes IMDS-readable role credentials (T1) just to
export metrics.

This change implements B1/B1' end to end: a bounded, backoff-and-jitter
exporter inside `rayd` that reuses the existing 5 s metrics sampler, signed
either with SigV4 over the execution role (caps-only) or a pushed bearer
token (works on `rayito-base`), opt-in only through `ConfigureSandbox`'s
`telemetry_export` section (ADR-015, M15 foundations).

## What Changes

- **rayd-core domain** (`crates/rayd-core/src/telemetry/`): `TelemetryConfig`
  (validated interval 15-300 s, service name, auth), the closed `GaugeKind`
  (7 metrics)/`AttrKey` (4 resource attributes) vocabulary so a path, a
  command or an `envs`/`metadata` value can never become an exported
  attribute by construction, the bounded `Batcher` (drop-oldest, jittered
  backoff reusing the existing `code::ports::RandomSource` port — no new
  crate), and the `TelemetrySink`/`OtlpEncoder` ports.
- **rayd adapters**: `adapters::aws_sigv4` (a from-scratch SigV4 signer over
  `sha2` — no new crate per the shared-file protocol — verified against the
  RFC 4231 HMAC-SHA256 test vector and AWS's own worked SigV4 example),
  `adapters::otlp_codec` (builds an `ExportMetricsServiceRequest` from a
  minimal vendored subset of `opentelemetry-proto`,
  `crates/rayito-proto/vendor/opentelemetry/`, Apache-2.0), and
  `adapters::cloudwatch_otlp_sink` (a dedicated `hyper-rustls` client,
  gzip, `FilteringResolver` reused from ADR-010).
- **rayd wiring**: `features::telemetry_export` replaces its `Unsupported`
  stub with a real `ConfigurableFeature` + `LifecycleParticipant` (a
  bounded `/suspend` flush, ≤ 2 s). `FeatureContext` grows the fields this
  feature needs (session, credential broker, pushed-credentials holder,
  metrics-history handle, region); `main.rs` now builds one `FeatureSet` for
  the whole process (`router_with_features`, a new function alongside the
  three existing `router*` ones, which keep building their own default,
  all-`Unsupported` set — zero behavior change for every existing caller
  and test) shared by `ConfigureGrpc`, `HealthGrpc` (`Health.features`
  now reflects real capability) and the hooks' participants.
- **Python/TypeScript**: `_telemetry_export`/`telemetry-export` packages
  (`TelemetryExport`, `OtlpAuth`, `TelemetryHealth`, the `ConfigureSection`
  adapter, bearer-secret resolution). `_feature_options.plan_features`/
  `planFeatures` wires the `telemetry` branch to real validation instead of
  an unconditional stub raise (the first of the seven 0.6 options to do
  so). `sandbox_{sync,async}/main.py` and `sandbox/sandbox.ts` send the
  section after `/run` is confirmed ready and check
  `Health.features.telemetry_export`, terminating the sandbox (unless
  `keep_on_failure`) and raising `UnimplementedError` when unsupported —
  the same gate `_apply_initial_network`/`#applyInitialNetwork` already
  uses for network policy. `get_telemetry_status()`/`getTelemetryStatus()`
  is a new, explicit method (never folded into `get_health()`/`getHealth()`,
  so the path without `telemetry=` never pays an extra RPC).
- **`configure/` seam** (both SDKs): `_configure_base.py`/`configure/base.ts`
  and `sandbox_{sync,async}/configure.py`/`configure/rpc.ts` existed as
  foundations' seam in Python only, with "no consumer yet" written into
  their own docstrings; this change is that first consumer and also builds
  the equivalent TypeScript seam, which foundations had not created.
- **`traceparent` propagation** (both SDKs, design.md D8): with
  `tracer_provider=`/`tracerProvider`, every RPC on the handle's channel
  carries W3C `traceparent` (`TraceparentProvider`, installed through the
  `CallMetadataProvider` seam: `ProxyAuthPlugin.providers` in Python,
  `ProxyAuthOptions.callMetadata` in TypeScript); `rayd`'s
  `grpc::request_context` records it as `trace_id`/`span_id`. Without the
  option no header is added and no `opentelemetry` module is imported.
- **`infra/otlp-export.yaml`**: one IAM managed policy
  (`RayitoOtlpExport`, `cloudwatch:PutMetricData` on the account's default
  OTLP dataset — OT9 found this cannot be scoped by namespace). Fills the
  `otlp-export` `OptionalStack` slot foundations stubbed.
- **Docs**: `exportacion-otlp.md` (full guide), `referencia/python/opcionales.md`
  (new section), ADR-021, `AWS_API_NOTES.md` §26, `MILESTONES.md`, three
  CHANGELOGs, `docs/RELEASE_NOTES_0.6.0.md`, `docs-delta.md` for the shared
  tables `m15-docs-integration` applies.
- **Tests**: rayd-core/rayd unit tests (254 `rayd` + 587 `rayd-core`, all
  green inside the Lima VM, `cargo clippy --workspace --all-targets --
  -D warnings` clean), `clients/python/tests/unit/test_m15_rayd_otlp.py`
  (30 cases) plus updates to the three foundations tests that assumed every
  0.6 option was still an unconditional stub
  (`test_m15_feature_options.py`, `test_m15_create_kwargs.py`), their
  TypeScript mirrors (`m15-rayd-otlp.test.ts`, `m15-feature-options.test.ts`,
  `m15-create-options.test.ts`), the full Python suite (2564 passed, 0
  failed) and the full TypeScript suite (1131 passed, 0 failed).

## Second review (PR #81)

A second review found the `/resume` pool rebuild was never called, a
cancelled `/suspend` flush could lose drained points, the bearer bypassed
`SecretCache` (and its `rayito/` prefix), the SDK half of `traceparent`
was unwired, SigV4 was hand-rolled while a direct crate was added anyway,
the IMDS provider was not actually shared, and a few smaller items. All
are fixed in this change; design.md D5, D8, D10-D12 record the decisions
and tasks.md §14 lists each fix.

## Impact

- **Rust**: `crates/rayd-core/src/telemetry/` (new), `crates/rayd-core/src/metrics_history.rs`
  (+`latest()`), `crates/rayd-core/src/lib.rs` (+1 line), `crates/rayd-core/src/root_egress.rs`
  (doc-comment backtick fix only), `crates/rayd/src/adapters/{aws_sigv4,otlp_codec,cloudwatch_otlp_sink}.rs`
  (new), `crates/rayd/src/adapters/mod.rs`, `crates/rayd/src/features/{mod,telemetry_export}.rs`,
  `crates/rayd/src/grpc/{mod,health,configure}.rs`, `crates/rayd/src/main.rs`,
  `crates/rayd/Cargo.toml` (+`prost`, already a workspace dependency),
  `crates/rayito-proto/src/lib.rs`, `crates/rayito-proto/vendor/opentelemetry/**` (new),
  `proto/rayito/v1/telemetry_export.proto`.
- **Python**: `_telemetry_export/` (new package), `_feature_options.py`,
  `_transport.py` (+`CallMetadataProvider` seam), `sandbox_{sync,async}/main.py`,
  `__init__.py`, `_stacks/components/otlp_export.py`,
  `tests/unit/test_m15_rayd_otlp.py` (new), `tests/unit/test_m15_feature_options.py`,
  `tests/unit/test_m15_create_kwargs.py`.
- **TypeScript**: `telemetry-export/` (new), `configure/` (new: `base.ts`, `rpc.ts`),
  `feature-options.ts`, `sandbox/sandbox.ts`, `index.ts`,
  `stacks/components/otlp-export.ts`, `scripts/check-dts-cost-blocks.mjs`
  (+2 registered declarations), `tests/unit/m15-rayd-otlp.test.ts` (new),
  `tests/unit/m15-feature-options.test.ts`, `tests/unit/m15-create-options.test.ts`.
- **Infra**: `infra/otlp-export.yaml` (new; generated templates in both
  SDKs via `scripts/gen_stack_assets.py`).
- **Docs**: see "What Changes" above; `docs-delta.md` in this change
  directory for the shared tables.
- **No changes** to any 0.5.x or 0.6.0-foundations public behaviour without
  `telemetry=`/`telemetry`: no new RPC, no new AWS client, no new outbound
  connection.

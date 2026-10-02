## Context

`v06-foundations` built `ConfigureSandbox`, the `OptionalStack` convention
and six per-feature stub slots, explicitly leaving "the first feature that
needs real shared context" to promote `ConfigureGrpc`'s locally-built
`FeatureSet` into something `main.rs` builds once and shares (E1 of that
change's `design.md`). This change is that first feature. It also found
several seams the M15 architecture plan named but foundations did not
actually create (the TypeScript `configure/` adapter, the
`CallMetadataProvider` seam, the `check-dts-cost-blocks.mjs`/
`test_optional_features_docs.py` drop-in registries) and had to decide,
case by case, whether to build them now or defer them.

## Decisions (this change)

### D1. `main.rs` builds one real `FeatureSet`; the three existing `router*` functions keep their old behavior unchanged

`router_with_transfers` (and the `router`/`router_with_settings` it backs)
built a disposable `FeatureSet` with `FeatureContext::default()` inside
itself, per foundations' own comment: "every slot is still
`features::slot::Unsupported`... threads `Arc<FeatureSet>` through
`Services` in its own PR instead of building it here." Rather than widen
`Services` (touched by four integration test files and `main.rs`), this
change adds a new `router_with_features(services, settings, transfers,
features)` function that the other three now delegate to, each still
building its own default `FeatureSet` internally. `main.rs` calls the new
function directly with a real, process-wide `FeatureSet` built by a small
extracted helper, `build_feature_set` (kept as its own function rather than
inlined, to stay under `clippy::too_many_lines` on `main()`). Every
existing test, including the four that construct `Services` literally,
needs zero changes.

### D2. `HealthGrpc` gets an additive `with_features` builder, not a required constructor argument

Mirroring the existing `.with_persistence(...)`/`.with_transfers(...)`
builder pattern already on `FilesystemGrpc`, `HealthGrpc::new(...)` keeps
its five positional arguments and a new `features: Option<Arc<FeatureSet>>`
field defaults to `None` (reporting `AgentFeatures::foundations_only()` and
empty `root_egress`, exactly as before this change). Only `main.rs` calls
`.with_features(...)`. `FeatureSet` gains `agent_features()` and
`root_egress()` methods (capability-based, never "currently configured" —
matching `AgentFeatures`'s and `RootEgressClass`'s own doc comments) so
`to_response` has one real implementation to call instead of a second
hard-coded shape.

### D3. `FeatureContext` grows fields instead of becoming feature-specific

Foundations' own doc comment on `FeatureContext` invited this: "a feature
that needs credentials, a bucket name or other shared context adds a field
here... never by widening `FeatureSet` itself." This change adds `session`,
`credentials` (`Arc<ImdsCredentialBroker>`), `pushed`
(`Arc<PushedCredentials>`), `history` (`Arc<MetricsHistory>`) and `region`.
`FeatureContext::default()` (used only by tests and by the two remaining
bare-construction call sites, now `FeatureContext::default()`) builds a
harmless default — `region: None` in particular keeps `telemetry_export`
`Unsupported` by default, so `every_slot_starts_unsupported` in
`features/mod.rs`'s own test module needed no change. The other five
stub `build()` functions already take `_ctx: &FeatureContext` and ignore
it, so widening the struct's fields does not touch them.

### D4. Image memory comes from the guest's own `Health`, not the image's declared baseline

The architecture sketch for `AttrKey::ImageMemoryMib` implied the image's
*declared* baseline (what `sizes-catalog`'s `ConventionCatalog` reads via
`GetMicrovmImageVersion`). That catalog does not exist yet (sizes-catalog
is a sibling, parallel change), and adding a `GetMicrovmImageVersion` call
from this feature would mean an extra AWS call every time `telemetry=` is
used, coupled to a module this change does not own. Instead, the SDK
passes `info.memory_mb`/`readinessHealth.memoryTotalBytes` — the guest's
own observed `MemTotal`, already available for free right after `/run`'s
readiness poll. This is honestly a *different* number (Q68: the guest sees
4x the image's declared memory), documented as such in the proto comment
and the AWS_API_NOTES §26 entry, not silently presented as the baseline.

### D5. SigV4 signing uses `aws-sigv4`; direct dependencies only on crates already locked

*Superseded (second review of PR #81):* the first version hand-rolled
HMAC-SHA256 and SigV4 over `sha2` on the reading that the M15 shared-file
protocol (§5) forbids new direct crates, while the same PR added
`aws-credential-types` as a direct dependency — two rules at once, and
hand-written crypto that only added review and security surface. The rule
this change now follows, stated once in the workspace `Cargo.toml`: a
direct dependency is allowed only on a crate `aws-config`/`aws-sdk-s3`/
`hyper` already pull in transitively, pinned to the exact version
`Cargo.lock` already resolves, so the dependency tree gains no crate.
Under it, `cloudwatch_otlp_sink` signs with `aws-sigv4` 1.6.0 (the signer
`aws-sdk-s3` itself uses), `aws-credential-types` 1.3.0 provides the
`Credentials` it signs with (its secret is zeroized internally) plus the
`CredentialsError` `credential_broker` classifies, and `httpdate` 1.0.3
parses the `Date` header D11 measures skew from. `adapters::aws_sigv4` is
deleted. The signed headers are every header the request carries except
`aws-sigv4`'s default exclusions (`user-agent`, ...), with the default
`PayloadChecksumKind::NoHeader` — the payload hash still binds the body
through the canonical request, as in the OT1 measurement (Q91).

### D6. The vendored OTLP proto is a hand-authored minimal subset, with real upstream field numbers

`crates/rayito-proto/vendor/opentelemetry/` declares only the messages and
fields `adapters::otlp_codec` actually sets (no `Sum`/`Histogram`, no
`ArrayValue`/`KeyValueList`, no exemplars), but every field number matches
the real `opentelemetry-proto` schema (verified from memory against a
well-known, long-stable spec and cross-checked by round-tripping
`ExportMetricsServiceRequest::decode` in `otlp_codec`'s own tests), so the
bytes `rayd` sends decode identically to what a full `opentelemetry-proto`
client would produce for the same gauges. The module nesting
(`opentelemetry::proto::common::v1`, etc.) mirrors the real package
hierarchy exactly, because prost's generated code cross-references a
sibling package with a relative `super::super::...` path that assumes it.

### D7. `get_telemetry_status()`/`getTelemetryStatus()` is a new explicit method, not a field on `get_health()`/`getHealth()`

The architecture sketch showed `sbx.get_health().telemetry`. Implementing
that literally would mean `get_health()` — called by every sandbox,
`telemetry=` or not — conditionally calls `ConfigureStatus` too, or always
does and eats the extra RPC unconditionally. Either risks the "zero calls
without the option" guarantee the golden test enforces. A new, explicit
method keeps that guarantee trivially true (it is never called unless the
caller asks) at the cost of a small API deviation from the sketch,
documented in the feature's own docs page and this file.

### D8. `traceparent` propagation is live, and only with `tracer_provider=`

*Second review of PR #81:* the first version shipped the SDK half of §7.5
as a tested but disconnected seam, citing the risk of reordering when a
handle's auth plugin is built relative to when its instrumentation is
known. That risk turned out not to need any reordering: Python's
`ProxyAuthPlugin.providers` is already a public, mutable attribute read on
every call, and every place that assigns a handle's instrumentation after
construction now goes through one setter (`_use_instrumentation` in
`Sandbox`/`AsyncSandbox`, `#useInstrumentation` in TypeScript) that also
sets the channel's call-metadata providers from
`call_metadata_providers(instrumentation)` /
`callMetadataProvidersFor(tracerProvider)`. TypeScript gets the missing
seam: `ProxyAuthOptions.callMetadata`, read per request by
`proxyAuthInterceptor` from `SandboxCore.callMetadataProviders`.

Without `tracer_provider=`/`tracerProvider` the provider list is empty, no
`opentelemetry` module is imported and no header is added: the zero-cost
golden test (Python) and `otel.integration.test.ts` (TypeScript, with a
global propagator registered) assert that no request `rayd` receives
carries `traceparent`/`tracestate`. With it, `rayd`'s
`grpc::request_context` layer (unchanged) records the `trace_id`/`span_id`.
One-shot channels with no handle (`Sandbox.kill`/`pause`/`resume` static
methods, `get_info`'s probe) do not propagate: there is no handle
instrumentation to read, and their spans already cover the call itself.

### D9. `check-dts-cost-blocks.mjs`'s `COST_DECLARATIONS` gets two more hard-coded entries, not a glob conversion

`v06-foundations`'s own `design.md`/`proposal.md` left this array
hard-coded, explicitly noting "converting it... is left for
`m15-docs-integration` or whichever feature first adds a 'disponible'
row" — this change is that feature. Converting the whole mechanism to glob
a `cost-declarations/` directory (as the architecture plan's aspirational
shape names) is a larger, shared-infrastructure change better done once,
deliberately, by `m15-docs-integration` after seeing how many sibling
features also need an entry, rather than each parallel feature attempting
the same registry migration independently and conflicting. This change
adds its two entries (`class TelemetryExport`, `opción telemetry`) to the
existing array, each with a full "Coste y activación" TSDoc block that
passes the existing check unchanged.

### D10. `/resume` rebuilds the exporter without waiting, and no exit path loses drained points

`hooks::mod`'s `/resume` handler now calls every `LifecycleParticipant`'s
`on_resume` (`run_participants_on_resume`, also `on_terminate`), taken
verbatim from `m15-events-webhooks`' identical hunk so whichever of the two
PRs merges second merges cleanly. Before, nothing called `on_resume`, so
the pool rebuild §13.6 described never ran. The rebuild itself
(`SharedState::rebuild_sink_on_resume`) aborts the old exporter task
instead of awaiting it — a send started before the suspension could
otherwise hold `/resume` for up to `EXPORT_ATTEMPT_TIMEOUT` (8 s) — and
keeps the current exporter running if a fresh sink cannot be built. Every
stop is now `JoinHandle::abort`, which is only safe because
`export_pending` wraps the drained points in an `InFlight` guard that
re-enqueues them on drop: the same guard covers `/suspend`'s flush being
dropped by `hooks::run_participants`' outer `timeout(share, ..)`, which is
created first and so normally fires before the inner one.

### D11. SigV4 signing corrects the guest clock from AWS's `Date` header

`clock_offset_ms` (`Health`) measures wall-clock drift against the
monotonic clock across a pause, not skew against AWS, and applying it
would double-correct once the guest resyncs. The sink instead does what
the AWS SDKs do: every response's `Date` header is compared with the guest
clock; a difference above `SKEW_CORRECTION_THRESHOLD` (60 s, well inside
SigV4's 5-minute window) is stored and applied to the next signature, and
it returns to zero once the clock agrees again. A refused export whose
`Date` changed the stored skew is reported as `network` (retried with the
queued points) rather than `rejected`. The `/resume` rebuild carries the
measured skew over (`CloudWatchOtlpSink::rebuilt`). Whether CloudWatch's
OTLP 403 actually carries `Date` is on the OT5 acceptance checklist.

### D12. The OTLP bearer is read through `SecretCache`, and one IMDS provider is shared

`OtlpAuth.bearer(secret_name)` first used its own boto3 / SDK v3 client,
looked the name up raw and skipped error translation, so `"otlp-key"`
behaved differently here than in `secrets=`. Both SDKs now read it with
the handle's shared `SecretCache` (`resolve_bearer_token`/
`aresolve_bearer_token`, TypeScript `resolveBearerToken`), inheriting the
`rayito/` prefix rule, `translate_error` and the TTL; `build_section` is
pure. In `rayd`, `main` builds one `imds_execution_role_provider()` and
hands it to both `S3ObjectStore` (persistence) and
`ImdsCredentialBroker::sharing` (`FeatureContext`), so there is one IMDS
cache and one refresh cycle, as §1(c) asks.

## Needs the maintainer

None beyond the four (D1-D4) already recorded in `v06-foundations`'s
`design.md`, which this change does not touch.

## Migration

None: every new surface (the `telemetry_export` proto section, the
`telemetry=`/`telemetry` kwarg, `get_telemetry_status()`/
`getTelemetryStatus()`, the `otlp-export` stack component) is additive and
inert without an explicit caller.

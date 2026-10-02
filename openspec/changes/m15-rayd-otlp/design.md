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

### D5. SigV4 signing is hand-written over `sha2`, not a new crate

The M15 shared-file protocol (§5) allows this feature to add vendored
protos but says "no crates." `aws-config`/`aws-sdk-s3` sign their own S3
calls internally and expose no reusable public signer. `adapters::aws_sigv4`
implements HMAC-SHA256 (RFC 2104) directly over the `sha2` dependency
already in the workspace, verified against the RFC 4231 test vector and a
hand-checked AWS worked SigV4 example (2015-08-30T12:36:00Z, the exact
timestamp from AWS's own canonical-request documentation page), rather than
asserting against a single hard-to-independently-verify end-to-end
signature. The surrounding canonical-request/string-to-sign assembly is
plain, well-documented string formatting that unit tests check component by
component (payload hash, signed-header set, credential scope, session-token
handling).

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

### D8. `traceparent` propagation ships as a tested, disconnected seam (see proposal.md's "Non-blocking follow-up")

Covered in `proposal.md`; recorded here because it is the one place this
change's scope was deliberately narrowed after starting on it, following
the same "stop when something doesn't fit instead of improvising" rule
`v06-foundations` applied to the zombie reaper.

**Post-review update (tasks.md §13.3):** a review of the first version
flagged two gaps in this narrowing that were not actually part of the
"reorder the live channel" risk D8 is about, and so are fixed directly
rather than deferred further:

- **`rayd`'s own side was entirely missing.** The architecture (§7.5)
  always meant propagation to have two ends: the SDK injecting
  `traceparent`, and `rayd` reading it back to correlate its own log
  lines. Only the SDK side was ever planned as "disconnected" (because
  wiring the *injection* into a live channel is the risky, shared-file
  reorder); `rayd`'s *reading* side carries no such risk -- it is a new,
  additive tower layer (`grpc::request_context`) that only ever reads one
  header and, when present, opens a span for that RPC. It did not need
  deferring and is implemented now.
- **TypeScript had no `TraceparentProvider` at all**, let alone a
  disconnected one -- a real parity gap, not a narrower version of
  Python's. `telemetry-export/propagation.ts` now mirrors Python's
  `_propagation.py` exactly (same scope: tested in isolation via a fake
  `@opentelemetry/api` peer, not wired into the transport).

**Still deferred, in both SDKs**, for exactly the reason D8 already gives:
connecting the (now real, tested) provider to the live channel's auth
plugin/interceptor requires reordering when that plugin is built relative
to when the OTel instrumentation is known, in a file every other 0.6
feature also touches (`sandbox_sync/main.py` and its mirrors). TypeScript
additionally has no `CallMetadataProvider`-equivalent seam on its
transport yet (Python's existed from foundations; TypeScript's does not),
so TypeScript's live wiring needs that seam built first, same as this
change already had to build TypeScript's `configure/` seam from scratch
(D1-area work, §5 of `proposal.md`).

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

## Needs the maintainer

None beyond the four (D1-D4) already recorded in `v06-foundations`'s
`design.md`, which this change does not touch.

## Migration

None: every new surface (the `telemetry_export` proto section, the
`telemetry=`/`telemetry` kwarg, `get_telemetry_status()`/
`getTelemetryStatus()`, the `otlp-export` stack component) is additive and
inert without an explicit caller.

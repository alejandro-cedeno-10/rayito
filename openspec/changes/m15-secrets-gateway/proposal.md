## Why

An agent calling an external API (Anthropic, OpenAI, a customer's own
backend) needs that API's credential, but today the only way to get it into
the sandbox is `secrets=` injecting it as a process environment variable —
visible to the agent's own code, to anything it shells out to, and to
`cmdline`/`environ` for as long as the process lives. `docs/research/
2026-10-e2b-out-of-scope.md` (ADR-014, secrets-gateway's design section)
proposes the alternative E2B itself does not have: a loopback HTTP listener
inside `rayd` that holds the real credential, injects it into requests the
sandbox makes to one declared upstream, and never lets the sandbox read it
back. `m15-foundations` already built the shared `ConfigureSandbox` channel,
the `SecretGatewayConfig`/`Status` proto stub and the `secret_gateway`
`FeatureSet` slot this change fills in.

## What Changes

- **`rayd_core::secret_gateway` (pure domain).** `vault::SecretValue`
  (`Zeroizing`, no `Debug`/`Display`/`serde`, `header_safe` rejects
  CR/LF/NUL), `route::{GatewayRoute, GatewaySpec, RawRoute}` (validated
  bounds: ≤8 routes, ≤16 headers/route, ≤32 allow rules/route, rate
  1..6000/min, `https://host` with no path/query, allow non-empty), a
  wildcard-suffix (`/*`) path-prefix match, `decision::{TokenBucket,
  evaluate}` (integer-milli-token bucket, deterministic), and
  `header_template` (which inbound header names a route's listener must
  strip before forwarding — exactly what it injects, so the sandbox can
  never spoof or read back its own gateway's credential, T24).
- **`rayd::secret_gateway` (adapters) and `features::secret_gateway`.** One
  loopback `axum` listener per configured route
  (`GatewayRuntime::apply`, replacing the whole route set on every
  `Configure`, matching `SecretGatewayConfig`'s replace-whole-state
  semantics), forwarding through a shared `hyper`/`rustls` HTTPS client
  (`GatewayUpstream`, OS trust store, `FilteringResolver` so a route's
  upstream can never resolve to loopback/link-local/IMDS, no buffering of
  either body so SSE and chunked uploads stream through). `SecretGatewayFeature`
  is the first real (non-`Unsupported`) `ConfigurableFeature` slot;
  `Health.features.secret_gateway` and `root_egress` (`SecretGatewayUpstream`,
  the one declared root-egress bypass this feature opens) are flipped to
  report it. No `LifecycleParticipant`: a request in flight when `/suspend`
  freezes the VM is retried by the sandbox's own HTTP client like any other
  cut connection, so there is nothing to flush.
- **Python `_secret_gateway/`.** `_domain.py` (pure: `SecretGateway`,
  `GatewayStatus`, validation mirroring the Rust bounds) and `_section.py`
  (`GatewaySection` implementing `_configure_base.ConfigureSection`,
  resolving each header through the same `SecretCache` `secrets=` already
  uses; `GatewaySectionFactory`, the convention this change introduces for
  a `FeaturePlan.configure_sections` entry that needs something resolved
  after `plan_features` runs; `GatewayHandle`, `sbx.gateways`'s type, with
  `refresh()`/`arefresh()`). `_feature_options.plan_features`'s `gateways`
  branch now validates and returns a real plan instead of raising.
  `sandbox_{sync,async}/main.py` gain the generic `ConfigureSandbox`
  dispatch (`_apply_configure_sections`, reusable by every future 0.6
  feature's own section; it terminates the VM on any failure unless
  `keep_on_failure`, and hands the post-`Configure` status to any
  `PostApplySection`) and the `gateways` property. `SandboxPool.take()`
  gains `gateways=` in both SDKs.
- **TypeScript mirrors.** `configure/base.ts` (the `_configure_base.py`
  seam TS was missing: `AgentFeatures`, `requireConfigureSupport`,
  `sectionError`, `raiseForResults`, `PostApplySection`; placed at the
  `configure/` path the 0.6 architecture reserves for foundations, which
  had not created it yet; flagged for foundations to own), `secret-gateway/{domain,section}.ts`,
  `feature-options.ts`'s `gateways` branch, and the same dispatch wired into
  `sandbox/sandbox.ts`/`sandbox/core.ts` (`ConfigureService` client,
  `agentFeatures` on `SandboxCore`, `#applyConfigureSections`, `gateways`
  getter).
- **E2B shim.** None: E2B has no equivalent REST surface for this. Row 14 of
  `e2b-parity.md` (`network.rules`/`SandboxNetworkRule`, today "imposible en
  la plataforma" because Lambda MicroVMs has no host-level egress hook)
  becomes "divergente (0.6.0)": Rayito now solves the same underlying need
  — injecting a credential outside the sandbox's own reach — with an
  in-guest loopback listener instead (via `docs-delta.md`, applied by
  `m15-docs-integration`).
- **Docs.** `docs/site/docs/funciones-opcionales/pasarela-de-secretos.md`
  filled in (Coste y activación, Python/TypeScript tabs), ADR-023 in
  `ARCHITECTURE.md`, `AWS_API_NOTES.md` §28 (reuses Secrets Manager's own
  §19 API surface — no new AWS service), `MILESTONES.md`'s M15
  secrets-gateway subsection, the three CHANGELOGs' `m15-secrets-gateway`
  anchor, `docs-delta.md` (T24 threat row, e2b-parity row 14,
  optional-features.md, referencia/errores.md; cost.md and limits.md need
  no change, same as `m13-secrets`).

## Off by default (ADR-014 rule 4)

No `gateways=`/`gateways` means: no loopback listener opens, no Secrets
Manager client is built, no `ConfigureSandbox` call is made, and the 0.5.x
golden trace (`test_m15_zero_cost.py`, `zero-cost-defaults.test.ts`) stays
byte for byte unchanged — verified by this change's own tests rather than
re-asserted here.

## Impact

- **Rust**: `crates/rayd-core/src/secret_gateway/{mod,vault,route,decision,
  header_template}.rs` (new), `crates/rayd/src/secret_gateway/{mod,listener,
  upstream}.rs` (new), `crates/rayd/src/features/secret_gateway.rs` (stub
  replaced), `crates/rayd/src/features/mod.rs` and
  `crates/rayd/src/grpc/{configure,health}.rs` (one line each: the
  `SecretGatewayConfig.clone()` a non-`Copy` section needs, and the
  `secret_gateway`/`root_egress` flags), `proto/rayito/v1/
  secret_gateway.proto` (owned fields filled in).
- **Python**: `_secret_gateway/` (new package), `_feature_options.py`
  (`gateways` branch), `sandbox_{sync,async}/main.py` (generic dispatch +
  `gateways` property + `AgentFeatures` capture in `_record_health`), two
  new test files plus an update to the existing
  `test_m15_feature_options.py` parametrize list.
- **TypeScript**: `configure/base.ts` (new), `secret-gateway/{domain,
  section}.ts` (new), `feature-options.ts`, `sandbox/{sandbox,core}.ts`,
  `index.ts`.
- **Docs/OpenSpec**: this change's own files plus a `docs-delta.md` for
  `m15-docs-integration` to apply.
- **No infra**: reuses `infra/secrets-access.yaml`/`RayitoSecretsReader`
  unchanged; no new CloudFormation template.

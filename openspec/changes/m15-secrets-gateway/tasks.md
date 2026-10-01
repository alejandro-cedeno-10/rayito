## 1. `rayd_core::secret_gateway` (pure domain)

- [x] 1.1 `vault.rs`: `SecretValue` (`Zeroizing`, no `Debug`/`Display`/`serde`), `header_safe` (rejects CR/LF/NUL).
- [x] 1.2 `route.rs`: `RawRoute`, `GatewayRoute`, `GatewaySpec::parse` (bounds: ≤8 routes/gateway, ≤16 headers/route, ≤32 allow rules/route, rate 1..6000/min with `0` meaning the default, `https://host` upstream with no path/query, non-empty allow list, uppercase methods, `/*` wildcard-suffix prefix match), closed lowercase-snake `GatewaySpecError`.
- [x] 1.3 `decision.rs`: integer-milli-token `TokenBucket` (deterministic, no floats), `evaluate` (allowlist first, then rate limit), `GatewayErrorClass`.
- [x] 1.4 `header_template.rs`: `stripped_header_names`/`must_drop` (hop-by-hop + `host` + every injected header name).
- [x] 1.5 Unit tests for every module (24 tests): parsing bounds, wildcard matching, token-bucket refill/consume, header stripping.

## 2. `rayd`/`rayd-core` adapters

- [x] 2.1 `crates/rayd/src/secret_gateway/upstream.rs`: `GatewayUpstream` (hyper-util legacy client, `hyper-rustls`, OS trust store, `FilteringResolver` over `rayd_core::transfer::is_forbidden_address`, HTTP/1.1, unbuffered request/response bodies).
- [x] 2.2 `crates/rayd/src/secret_gateway/listener.rs`: `GatewayRuntime` (one loopback `axum` listener per route, `apply` replaces the whole set and aborts every previously-started route on a partial failure so nothing leaks), the `forward` handler (decision → strip/inject headers → forward → strip hop-by-hop response headers, all streamed).
- [x] 2.3 `crates/rayd/src/features/secret_gateway.rs`: `SecretGatewayFeature` (`ConfigurableFeature` impl), `parse_spec` (proto → domain), falls back to `Unsupported` only if the OS trust store cannot load (mirrors `main::transfer_services`).
- [x] 2.4 `grpc/configure.rs`: `request.secret_gateway.clone()` (the one section no longer `Copy`, now that it has real fields).
- [x] 2.5 `grpc/health.rs`: `secret_gateway: true` and `root_egress: [SecretGatewayUpstream]` when supported.
- [x] 2.6 `features/mod.rs`: updated test (`secret_gateway` is now the one supported slot).
- [x] 2.7 Unit/integration tests: slot behaviour (applied/invalid/failed, previous state untouched on an invalid new config, status after apply, no participant, no listener before any `Configure`), `upstream.rs`'s forbidden-address classification. 231 total `rayd`+`rayd-core` tests pass; `cargo clippy --all-targets -- -W clippy::pedantic` clean on every file this change owns.

## 3. Python `_secret_gateway/`

- [x] 3.1 `_domain.py`: `SecretGateway` (validates upstream/headers/allow/rate), `GatewayStatus` (`.url`), `validate_gateways`/`validate_route_name`.
- [x] 3.2 `_section.py`: `GatewaySection` (`ConfigureSection` protocol), `GatewaySectionFactory` (the `configure_sections` convention for an entry that needs the resolved `SecretCache`), `GatewayHandle` (`refresh`/`arefresh`), `gateway_statuses_from_proto`.
- [x] 3.3 `_feature_options.py`: `gateways` branch validates and returns a `GatewaySectionFactory` instead of raising; docstring updated.
- [x] 3.4 `sandbox_sync/main.py` + `sandbox_async/main.py`: `ConfigureServiceStub`, `agent_features_from_health` captured in `_record_health`, generic `_apply_configure_sections` (reusable by every future 0.6 feature), `gateways` property, `refresh`/`arefresh` wired to re-send `Configure`.
- [x] 3.5 Unit tests: `test_m15_secrets_gateway_domain.py`, `test_m15_secrets_gateway_section.py`, `test_m15_secrets_gateway_zero_cost.py`; updated `test_m15_feature_options.py` (gateways is no longer a stub).

## 4. TypeScript mirrors

- [x] 4.1 `configure-base.ts` (new: `AgentFeatures`, `agentFeaturesFromHealth`, `requireConfigureSupport`, `sectionError`, `configSectionName` — the seam foundations had not yet created for TS).
- [x] 4.2 `secret-gateway/domain.ts`, `secret-gateway/section.ts`.
- [x] 4.3 `feature-options.ts`: `gateways` branch mirrors Python's.
- [x] 4.4 `sandbox/core.ts`: `ConfigureService` client, `agentFeatures` field, captured in `recordHealth`.
- [x] 4.5 `sandbox/sandbox.ts`: `#applyConfigureSections`, `#refreshGateways`, `gateways` getter, `gateways` option typed `SecretGateway`.
- [x] 4.6 `index.ts` exports.
- [x] 4.7 Unit tests: `m15-secrets-gateway-domain.test.ts`, `m15-secrets-gateway-section.test.ts`; updated `m15-feature-options.test.ts` (gateways is no longer a stub).

## 5. Docs and OpenSpec

- [x] 5.1 `docs/site/docs/funciones-opcionales/pasarela-de-secretos.md` filled in (Coste y activación, Python/TypeScript tabs, example, limits).
- [x] 5.2 `ARCHITECTURE.md` ADR-023.
- [x] 5.3 `AWS_API_NOTES.md` §28 (reuses §19's Secrets Manager surface; no new service).
- [x] 5.4 `MILESTONES.md` M15 secrets-gateway subsection.
- [x] 5.5 CHANGELOG anchors (Python, TypeScript, root) filled in.
- [x] 5.6 `docs-delta.md` for `m15-docs-integration` (e2b-parity row 111, optional-features.md, cost.md, security.md T24).
- [x] 5.7 This OpenSpec change (`proposal.md`, `tasks.md`, `design.md`, `specs/secret-gateway/spec.md`); `openspec validate m15-secrets-gateway --strict` passes.

## 6. Not done in this change (tracked, not blocking)

- [ ] 6.1 e2e tests (`clients/python/tests/e2e/test_m15_secrets_gateway.py`, `clients/typescript/tests/e2e/m15-secrets-gateway.test.ts`) against real AWS. **Gate for the serialized AWS acceptance stage; not run from this branch.**

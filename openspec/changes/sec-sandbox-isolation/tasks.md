## 1. Build hooks after `/run` (RAYITO-SEC-03, C-02, C-03)

- [x] 1.1 Reproduce: `hooks::tests::build_hooks_after_run` and
  `tests/m4_code.rs::a_validate_after_run_never_touches_the_default_kernel`
  fail on `main` (no anomaly counted; the default kernel restarted).
- [x] 1.2 `LifecycleState::run_claimed` / `SandboxSession::run_claimed`.
- [x] 1.3 `validate_hook_decision(state, run_claimed)` with
  `ValidateDecision::AfterRun`; unit test over every state.
- [x] 1.4 `validate` and `ready` handlers: 200 without side effects after
  `/run`, audited as anomalies.

## 2. Peer-uid check (RAYITO-SEC-05, C-01)

- [x] 2.1 `rayd_core::hook_peer`: `find_in_proc_net` (IPv4, IPv4-mapped
  `tcp6`), `classify_peer`, `peer_action`, `PeerSocketTable`; unit tests.
- [x] 2.2 `SANDBOX_UID_MIN`/`SANDBOX_UID_MAX` pinned to `SANDBOX_UID_RANGE`.
- [x] 2.3 `HookAudit::note_anomaly_after_run` / `SandboxSession::note_hook_anomaly`.
- [x] 2.4 `adapters::proc_net_peers` (real-kernel test of its own socket)
  and `agent_uid`.
- [x] 2.5 `hooks::guard_peers` middleware with `PEER_LOOKUP_TIMEOUT`;
  `hooks::tests::peer_guard` over a fake table and `MockConnectInfo`.
- [x] 2.6 `main.rs`: `guard_peers` + `into_make_service_with_connect_info`.

## 3. Gateway response headers (RAYITO-SEC-04)

- [x] 3.1 `header_template::must_drop_from_response` with
  `MIN_REFLECTED_VALUE_LEN`; unit tests (by name, by value, short value).
- [x] 3.2 `listener::redact_response_headers` in `into_axum_response`;
  test over an echoing header map.

## 4. Docs

- [x] 4.1 `SECURITY.md` T2, T17 and new T24.
- [x] 4.2 `ARCHITECTURE.md` hook table, "Origen de los hooks", ADR-006, ADR-023.
- [x] 4.3 `docs/SECURITY_AUDIT.md` §9 rows and "Lo que sigue abierto".
- [x] 4.4 `docs/site/docs/security.md`, `funciones-opcionales/pasarela-de-secretos.md`.
- [x] 4.5 `scripts/tests/test_security_docs.py` updated and extended.
- [x] 4.6 `crates/rayd/CHANGELOG.md` `[Unreleased]` → Security.

## 5. Build gate (RAYITO-ISO-2)

- [x] 5.1 Reproduce: `tests/m4_code.rs::the_build_only_execute_path_refuses_after_run`
  fails on the previous commit (the cell reaches the sidecar after `/run`).
- [x] 5.2 `SandboxSession::build_gate` (one lock) with
  `session::tests::the_build_gate_closes_with_the_accepted_run`.
- [x] 5.3 `CodeManager::build_gate`, `execute_unchecked` gated,
  `start_execution` shared with `execute`; `run_validation` checks first.

## 6. Persistence scope (RAYITO-ISO-4, C-07)

- [x] 6.1 Reproduce: `grpc::persistence::tests::a_bound_sandbox_reaches_only_the_homes_under_its_scope`
  (another base, a sibling prefix and another bucket all resolved before).
- [x] 6.2 `persistence::binding::PersistBinding` and `BindingRejection`
  with unit tests; `PersistenceError::OutsideBinding` → `PermissionDenied`.
- [x] 6.3 `resolve_location(request, region, binding)` and `SessionScope`
  for `prepare_checkpoint`/`prepare_restore`; flow tests show no store call.
- [x] 6.4 `run_payload` `persist` block and `SandboxSession::persist_binding`.
- [x] 6.5 SDKs: Python `build_run_hook_payload(persist=)` +
  `build_launch_plan(persist=)` (sync and async create), TypeScript
  `buildRunHookPayload({ persist })` + `buildLaunchPlan`; unit tests in both.
- [x] 6.6 Docs: `SECURITY.md` T15, `persistence.md`, `security.md`,
  `ARCHITECTURE.md` (`/run`, `FilesystemService`, ADR-009),
  `docs/SECURITY_AUDIT.md` §9, `test_security_docs.py`, the three CHANGELOGs.

## 7. Verification

- [x] 7.1 Rust gates in the Linux VM (fmt, clippy `-D warnings`,
  `cargo test --workspace --locked`).
- [x] 7.2 `scripts/tests`, docs `mkdocs --strict`, OpenSpec `validate --all --strict`.
- [ ] 7.3 Real AWS (follow-up, not in this change): rebuild the image, run
  `tests/e2e/test_m6_hardening.py`, and measure the uid owning the
  platform's hook connections; forge `/terminate` and `/validate` from
  `commands.run` and expect `peer_refused`; and a `Checkpoint` from a
  sandbox created with `persist=` towards another base answers
  `permission_denied` on the real image.

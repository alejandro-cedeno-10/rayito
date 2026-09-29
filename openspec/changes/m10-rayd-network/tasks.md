## 1. Egress use cases in the manager (rayd-hooks-534)

- [x] 1.1 `NetworkManager::on_run` / `on_resume` with the hook functions'
  bodies; the hooks call them in the same positions.
- [x] 1.2 FakeKernel tests: `/run` without enforce touches nothing; without
  `CAP_NET_ADMIN` it settles with `None`; `/resume` past the budget
  publishes `None` within it.

## 2. Installed state in core (rayd-network-manager-238a)

- [x] 2.1 `rayd_core::network::Installation` with getters and transitions;
  `swap::RECOVERY_SLOT`.
- [x] 2.2 Adapter `Installed { state, proxy }` using only the transitions.
- [x] 2.3 Core unit tests for every transition, `reinstall_plan`,
  `nothing_to_reverify` and `wants_dns_guard`.

## 3. Reinstall of an unrestricted policy and the double writer (rayd-network-manager-238b)

- [x] 3.1 Reported in the proposal and the PR; not implemented
  (behaviour change).

## 4. Verification verdict in core (rayd-network-manager-377)

- [x] 4.1 `probe::VerifyFailure` and the pure checks.
- [x] 4.2 Adapter `verify` over the checks, same order and tokens.
- [x] 4.3 Table-driven core tests.

## 5. One special-address classification (rayd-core-url-policy-227)

- [x] 5.1 `SpecialAddress` with `IMDS_V4` / `IMDS_V6` (re-exported by
  `guard`).
- [x] 5.2 Test oracle with the previous four predicates; old == new over
  every vector.
- [x] 5.3 The four predicates rewritten as policies; oracle deleted, vector
  table kept.

## 6. Exhaustive error mapping (rayd-grpc-network-82)

- [x] 6.1 `NetworkError::status_class` / `NetworkStatusClass`;
  `is_invalid_argument` removed.
- [x] 6.2 `status_for` and `step_name` exhaustive; grpc test updated.

## 7. IMDS numbers and the `ip` family helper (rayd-imds-block-43)

- [x] 7.1 `route_plan::IMDS_TABLE` / `IMDS_PRIORITY` and the ordering test.
- [x] 7.2 `ip_command::family_flag` / `run_ip_for`; `imds_block` and
  `egress_routes` use them.

## 8. Proxy reply contract in core (rayd-network-proxy-289)

- [x] 8.1 `ConnectFailure` with `http_status` / `socks_reply` in
  `proxy_protocol`, with a table test.

## 9. Gates and docs

- [x] 9.1 `cargo fmt`, `clippy -D warnings`, `cargo test --workspace` (as
  `tester`), `cargo deny`, `m9_egress` as root in a network namespace.
- [x] 9.2 `crates/rayd/CHANGELOG.md` and the architecture review rows.

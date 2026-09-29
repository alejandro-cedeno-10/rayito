## Why

The M9 architecture review (`docs/research/2026-09-m9-architecture-review.md`)
left seven findings on `rayd`'s egress side that are structural, not
functional: the hooks adapter ran the `/run` and `/resume` egress use cases
itself and wrote egress state the `NetworkManager` also owns; the installed
state (policy, plan, slot, DNS guard) was a plain struct mutated field by
field in three places; the ADR-012/D6 verification verdict, a security
invariant published in `Health`, was decided in the adapter; three
hand-written copies of "which addresses are special" had already drifted
once; the gRPC error mapping and `step_name` used wildcards; the IMDS
rule ordering was only a comment; and the proxy's client-visible reply
codes lived in the tokio adapter.

## What Changes

- `NetworkManager::on_run` / `on_resume` own the egress use cases of the
  `/run` and `/resume` hooks (net_admin branch, sub-budget, settle and
  `None` publication). The hooks call them in the same positions; the
  `/resume` step order and the `TimeoutWatcher` calls stay in the hooks.
- `rayd_core::network::Installation` holds the installed state with named
  transitions; `swap::RECOVERY_SLOT` names the slot a recovery rebuilds.
- `rayd_core::network::probe` gains `VerifyFailure` and pure per-observation
  checks; the adapter keeps its read order, early returns and tokens.
- `rayd_core::network::SpecialAddress` classifies special addresses once;
  the transfer SSRF predicate, both proxy guards and the probe's skip rule
  are policies over it, each keeping its own canonicalisation.
- `NetworkError::status_class` (exhaustive) replaces `is_invalid_argument`;
  `status_for` and `step_name` have no wildcard.
- `route_plan::IMDS_TABLE` / `IMDS_PRIORITY` with a test that the IMDS rule
  sorts before every egress slot; `ip_command::family_flag` / `run_ip_for`
  replace the copies in `imds_block` and `egress_routes`.
- `ConnectFailure` and its HTTP/SOCKS tables move to
  `rayd_core::network::proxy_protocol`.

No behaviour change: every log line, failure token, gRPC status code and
message, `ip` argv, rule priority and table number is unchanged. No spec
requirement text changes, so there are no spec deltas.

## Reported, not changed

- After `UpdateNetwork` to an unrestricted policy the proxy keeps running,
  so `/resume` re-verifies; if that verification fails transiently the
  reinstall puts deny-all in slot A while the policy stays unrestricted,
  and the next verification publishes `None` with every uid ≥ 1000
  connection blackholed. `reinstall_plan` should clear both slots for an
  unrestricted policy.
- On a `RESUME_VERIFY_BUDGET` expiry `on_resume` publishes `None` while the
  manager task still runs and may publish afterwards (two writers). The
  publication should move inside the manager task.

Both change observable egress enforcement, so they are left to a follow-up
with its own FakeKernel test.

## Impact

- Affected specs: none.
- Affected code: `crates/rayd/src/{hooks,network,grpc,adapters}`,
  `crates/rayd-core/src/network`, `crates/rayd-core/src/transfer/url_policy.rs`.
- The `rayd` binary changes (refactor only); no `.proto`, SDK or image
  contract change.

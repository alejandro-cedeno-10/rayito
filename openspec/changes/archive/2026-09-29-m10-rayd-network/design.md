## Context

Refactor only (M10 wave 2, architecture review findings rayd-hooks-534,
rayd-network-manager-238a/b, rayd-network-manager-377,
rayd-core-url-policy-227, rayd-grpc-network-82, rayd-imds-block-43,
rayd-network-proxy-289). Every decision below keeps the observable
behaviour byte for byte.

## Decisions

- **`on_run` / `on_resume` are the old hook functions, moved.** Same
  checks, same `tokio::time::timeout` around `enforce_deny_all_at_run` /
  `reverify_after_resume`, same log lines. On a `/resume` budget expiry
  `on_resume` still writes `EgressEnforcement::None` while the spawned
  re-verification task keeps running and may publish afterwards. That race
  is preserved on purpose: removing it changes the published
  `Health.egress_enforcement` in the failure path and is reported instead
  (see proposal, "Reported, not changed").
- **`Installation` transitions reproduce today's assignments.** `apply`
  still records the swap (`after_swap`) before `publish_proxy_policy`, the
  recovery (`after_recovery`) still lands on deny-all in `RECOVERY_SLOT`,
  and `reinstall_plan` keeps today's fallback (the stored plan, else
  deny-all), including for an unrestricted policy, which is the reported
  defect. The running `LocalProxy` stays in the adapter's `Installed`.
- **Verification checks are per observation, not a gathered verdict.** The
  adapter reads rules per family, then tables, then samples, then the proxy
  connect, and returns at the first failure, as before; gathering all
  observations first would change which token a multi-failure reports.
- **`SpecialAddress::of` never canonicalises.** Each caller keeps its own
  folding: the transfer predicate, `TargetGuard` and `UpstreamGuard` go
  through `canonical_ip`; the probe's `answered_before_policy` does not. A
  test oracle holding the previous bodies proved old == new for all four
  policies over the vector table (plus a guard with local addresses in
  plain, `::ffff:` and `::a.b.c.d` forms) before it was deleted; the
  vector table stays as the classification test. `canonical_ip` folds like
  `Ipv6Addr::to_ipv4` except `::` and `::1`, which are forbidden either
  way, so the transfer predicate's answer is unchanged.
- **`IMDS_ROUTE_TABLE` / `IMDS_RULE_PRIORITY` stay strings** in the adapter
  (they go into `&[&str]` argv arrays); a unit test pins them to the typed
  core constants.

## MODIFIED Requirements

### Requirement: Hook origin cannot be validated and the port stays the control
`rayd` SHALL NOT attempt to authenticate lifecycle hooks by source address, headers or a shared secret (measured 2026-09-15: hooks and proxied client traffic both arrive from `127.0.0.1` over HTTP/1.1, the proxy strips every `X-aws-proxy-*` header, and AWS shares no per-boot secret with the hooks). The primary control SHALL remain that the hooks port is never included in any `allowedPorts` the SDK mints (`allPorts` never used by default, `get_host(9000)` refused) and that `/run` is accepted once per boot. `SECURITY.md` T2 SHALL state the residual risk as a table of what a holder of an `allPorts` token can still do, with the bounds measured by the acceptance test. Neither `SECURITY.md` T2 nor `ARCHITECTURE.md` (the "Origen de los hooks" paragraph and ADR-006) SHALL present that port as a boundary against the workload inside the MicroVM: both SHALL state that `rayd` listens on `0.0.0.0:9000` in the same network namespace as the sandbox processes (no netns, no seccomp and no cgroup confinement; the M6 policy route blackholes only `169.254.169.254/32` for uid 1000-65535), so any process at uid 1000 reaches the six hook routes over loopback and the port control (`9000` never in `allowedPorts`) bounds only the **external** origin. T2 SHALL list `/terminate` and `/validate` among the hooks a forged caller reaches and SHALL state their effect: a forged `/terminate` cancels `rayd`'s cancellation token and takes the MicroVM with it (`rayd` is the image `CMD`), the one hook effect the operator cannot undo; a forged `/validate` restarts the `default` kernel context once per boot and runs the validation cell past the stream gate. T2 SHALL NOT claim that a forged hook leaves the kernel unrestarted. Both texts SHALL name per-peer-uid authentication of `/terminate` and `/validate` as pending work rather than describing the in-VM origin as mitigated.

#### Scenario: forged run is a no-op
- **WHEN** the e2e posts `/run` through the proxy with an `allPorts` token minted outside the SDK and a body carrying a different `token_sha256`
- **THEN** the hook answers 200 with `outcome: "already_ran"`, the original access token still authenticates `commands.run`, the kernel is not rotated and `get_health().hook_anomalies` is 1

#### Scenario: T2 names the in-VM origin and the two extra forgeable hooks
- **WHEN** `scripts/tests/test_security_docs.py::test_t2_names_the_in_vm_origin` reads the T2 row of `SECURITY.md` and the ADR-006 section of `ARCHITECTURE.md`
- **THEN** both name `0.0.0.0:9000` and the uid 1000 process inside the VM as an origin the port does not bound, T2 names `/terminate` and `/validate` with their effects, and neither text contains the string `el kernel no se reinicia`

### Requirement: Runtime hooks are audited after the first accepted run
After the first accepted `/run` of a boot, `rayd` SHALL log every call to `/run`, `/suspend`, `/resume` and `/terminate` as a `hook_audit` event carrying `hook`, `outcome`, `calls_since_run` (a per-hook monotone counter) and `anomaly` (true for a `/run` after the accepted one, the only call the audit can tell apart from a genuine one), and SHALL NOT log bodies, headers or payload characters for those calls. `rayd` SHALL count anomalous calls and stale-suspend recoveries in `hook_anomalies`, exposed by `HealthService.Health` (`HealthResponse.hook_anomalies`, field 10, `uint64`, 0 at boot). `ARCHITECTURE.md` and `SECURITY.md` T2 SHALL describe that scope as **every runtime hook** ("cada hook de runtime"), never as every hook, and SHALL name `/ready` and `/validate` as build hooks that never reach `audit()`, so their `HookAudit` slots stay at zero and a forged `/validate` produces no `hook_audit` line and no `hook_anomalies` increment.

#### Scenario: audit lines never carry bodies
- **WHEN** an integration test posts `/run` twice with a payload and inspects `rayd`'s log
- **THEN** the second call produced a `hook_audit` line with `hook: "run"`, `outcome: "already_ran"`, `calls_since_run: 1`, `anomaly: true`, and no line contains the payload text or the digest

#### Scenario: counter visible to the SDK
- **WHEN** the e2e reads `get_health()` after one forged `/run`, one stale-suspend recovery and a forged `/suspend` + `/resume` pair followed by another `/suspend` + `/resume`
- **THEN** `hook_anomalies == 2` (the forged `/run` and the recovery; no transition was refused) and the SDK logged one warning per generation it observed with a non-zero counter

#### Scenario: the docs scope the audit to runtime hooks
- **WHEN** `scripts/tests/test_security_docs.py::test_audit_scope_is_runtime_hooks` reads the "Origen de los hooks" paragraph of `ARCHITECTURE.md` and the T2 row of `SECURITY.md`
- **THEN** neither contains `cada hook se audita` or `de cada hook tras`, both contain `cada hook de runtime`, and both say `/ready` and `/validate` are not audited

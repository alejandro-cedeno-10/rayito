## MODIFIED Requirements

### Requirement: Hook origin cannot be validated and the port stays the control
`rayd` SHALL NOT attempt to authenticate lifecycle hooks by source address, headers or a shared secret (measured 2026-09-15: hooks and proxied client traffic both arrive from `127.0.0.1` over HTTP/1.1, the proxy strips every `X-aws-proxy-*` header, and AWS shares no per-boot secret with the hooks); the only identity it MAY use is the owner uid of the connection's client end as the kernel lists it (the peer-uid check). The primary control SHALL remain that the hooks port is never included in any `allowedPorts` the SDK mints (`allPorts` never used by default, `get_host(9000)` refused) and that `/run` is accepted once per boot. `SECURITY.md` T2 SHALL state the residual risk as a table of what a holder of an `allPorts` token can still do, with the bounds measured by the acceptance test. Neither `SECURITY.md` T2 nor `ARCHITECTURE.md` (the "Origen de los hooks" paragraph and ADR-006) SHALL present that port as a boundary against the workload inside the MicroVM: both SHALL state that `rayd` listens on `0.0.0.0:9000` in the same network namespace as the sandbox processes (no netns, no seccomp and no cgroup confinement; the M6 policy route blackholes only `169.254.169.254/32` for uid 1000-65535), so any process at uid 1000 reaches the six hook routes over loopback and the port control (`9000` never in `allowedPorts`) bounds only the **external** origin. T2 SHALL list `/terminate` and `/validate` among the hooks a forged caller reaches and SHALL state their effect: a forged `/terminate` cancels `rayd`'s cancellation token and takes the MicroVM with it (`rayd` is the image `CMD`), the one hook effect the operator cannot undo; a forged `/validate` would restart the `default` kernel context and run the validation cell past the stream gate. T2 SHALL NOT claim that a forged hook leaves the kernel unrestarted. Both texts SHALL describe the peer-uid check as the mitigation of the in-VM origin (`peer_refused` for `/terminate` and `/validate` from a sandbox uid) and SHALL NOT call it pending; T2 SHALL name its residual (a caller the lookup cannot attribute is honoured but counted).

#### Scenario: forged run is a no-op
- **WHEN** the e2e posts `/run` through the proxy with an `allPorts` token minted outside the SDK and a body carrying a different `token_sha256`
- **THEN** the hook answers 200 with `outcome: "already_ran"`, the original access token still authenticates `commands.run`, the kernel is not rotated and `get_health().hook_anomalies` is 1

#### Scenario: T2 names the in-VM origin and the two extra forgeable hooks
- **WHEN** `scripts/tests/test_security_docs.py::test_t2_names_the_in_vm_origin` reads the T2 row of `SECURITY.md`, the "Origen de los hooks" paragraph and the ADR-006 section of `ARCHITECTURE.md`
- **THEN** all name `0.0.0.0:9000` and the uid 1000 process inside the VM as an origin the port does not bound, T2 names `/terminate` and `/validate` with their effects, the peer-uid check and `peer_refused`, and no text contains `el kernel no se reinicia` or `queda pendiente`

### Requirement: Runtime hooks are audited after the first accepted run
After the first accepted `/run` of a boot, `rayd` SHALL log every call to `/run`, `/suspend`, `/resume` and `/terminate`, and every call to the build hooks `/ready` and `/validate`, as a `hook_audit` event carrying `hook`, `outcome`, `calls_since_run` (a per-hook monotone counter) and `anomaly` (true for a `/run` after the accepted one, for any `/ready` or `/validate` after it, and for a hook the peer-uid check refuses), and SHALL NOT log bodies, headers or payload characters for those calls. A `/validate` after the accepted `/run` SHALL answer 200 `validate_skipped` without restarting any kernel context or running the validation cell, whatever the validation state; a `/ready` after it SHALL answer 200 without changing the phase. Before the accepted `/run` no call SHALL be counted, so the build's genuine `/ready` and `/validate` never leave a count in the memory snapshot. `rayd` SHALL count anomalous calls, the peer-uid check's anomalies and stale-suspend recoveries in `hook_anomalies`, exposed by `HealthService.Health` (`HealthResponse.hook_anomalies`, field 10, `uint64`, 0 at boot). `ARCHITECTURE.md` and `SECURITY.md` T2 SHALL describe that scope as **every runtime hook** ("cada hook de runtime") plus the build hooks after `/run`, never as every hook.

#### Scenario: audit lines never carry bodies
- **WHEN** an integration test posts `/run` twice with a payload and inspects `rayd`'s log
- **THEN** the second call produced a `hook_audit` line with `hook: "run"`, `outcome: "already_ran"`, `calls_since_run: 1`, `anomaly: true`, and no line contains the payload text or the digest

#### Scenario: counter visible to the SDK
- **WHEN** the e2e reads `get_health()` after one forged `/run`, one stale-suspend recovery and a forged `/suspend` + `/resume` pair followed by another `/suspend` + `/resume`
- **THEN** `hook_anomalies == 2` (the forged `/run` and the recovery; no transition was refused) and the SDK logged one warning per generation it observed with a non-zero counter

#### Scenario: a validate after run never touches the default kernel
- **WHEN** an integration test posts `/run` and then `/validate` against a real kernel sidecar
- **THEN** `/validate` answers 200 `validate_skipped`, the sidecar received no `execute` and only `/run`'s `restart_context`, and `hook_anomalies` is 1

#### Scenario: build hooks before run are never counted
- **WHEN** a test posts `/ready` and `/validate` before `/run`
- **THEN** both answer as during a build and `hook_anomalies` stays 0 after `/run`

#### Scenario: the docs scope the audit to runtime hooks
- **WHEN** `scripts/tests/test_security_docs.py::test_audit_scope_is_runtime_hooks` reads the "Origen de los hooks" paragraph of `ARCHITECTURE.md`, ADR-006 and the T2 row of `SECURITY.md`
- **THEN** none contains `cada hook se audita`, `de cada hook tras`, `no se auditan` or `nunca llegan a audit()`, and they say that `/ready` and `/validate` after `/run` are audited as anomalies

## ADDED Requirements

### Requirement: A /terminate or /validate from a sandbox uid is refused with 200
`rayd` SHALL serve the hooks listener with each connection's peer address and SHALL look that address up in `/proc/net/tcp` and `/proc/net/tcp6` (an IPv4 peer also matching its IPv4-mapped form) within `PEER_LOOKUP_TIMEOUT` (1 s), classifying the caller as `sandbox` when the socket's owner uid is in `SANDBOX_UID_RANGE` (1000-65535) and is not `rayd`'s own effective uid, as `platform` when it is another uid and the socket is established, and as `unverified` otherwise (not listed, not established, no peer address, lookup failed or timed out). A `/terminate` or `/validate` from a `sandbox` caller SHALL answer 200 with `outcome: "peer_refused"` without running its handler and SHALL be audited as an anomaly. An `unverified` `/terminate` SHALL run and count one anomaly. A `/suspend` or `/resume` from a `sandbox` caller SHALL run (it is never refused) and count one anomaly. `/ready`, `/run` and a non-refused `/validate` SHALL NOT be counted by the check. The check SHALL never make a hook answer a non-2xx status, and it SHALL count nothing before the accepted `/run`.

#### Scenario: a forged terminate from the sandbox keeps the VM alive
- **WHEN** a test posts `/run` and then `/terminate` through `guard_peers` with a socket owned by uid 1000
- **THEN** `/terminate` answers 200 `peer_refused`, the phase stays `running`, the shutdown token is not cancelled and `hook_anomalies` is 1

#### Scenario: the platform's terminate goes through
- **WHEN** the same `/terminate` arrives on an established socket owned by uid 993 or by root
- **THEN** it answers 200 `terminating`, the phase is `terminating` and `hook_anomalies` is 0

#### Scenario: an unattributable terminate is honoured but visible
- **WHEN** `/terminate` arrives with no listed socket, a closing socket owned by root, or no peer address
- **THEN** it answers 200 `terminating` and `hook_anomalies` is 1

#### Scenario: a dual-stack socket cannot hide the sandbox uid
- **WHEN** the client end is listed only in `/proc/net/tcp6` as `::ffff:127.0.0.1`
- **THEN** `find_in_proc_net` returns its owner uid for the IPv4 peer `127.0.0.1`

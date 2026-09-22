# hook-defense Specification

## Purpose
TBD - created by archiving change m6-hardening. Update Purpose after archive.
## Requirements
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

### Requirement: Session-changing hooks are never refused
`rayd` SHALL accept every `/suspend` that arrives while `Running` or `Resumed` and every `/resume` that arrives while `Suspending` (or after a stale-suspend recovery), running the full checklist each time, with no rate limit and no interval between transitions: `rayd` cannot tell a forged call from the genuine one behind it, and a refused genuine `/suspend` would skip the checklist of a real checkpoint (streams left open, no quiesce, no `sync`) and turn the real `/resume` after the restore into an `unchanged` repeat (no generation bump, no probe, no reseed, no `clock_offset`, the frozen span absorbed by the running clock). Idempotent repeats (`unchanged`, `illegal`) SHALL keep answering 200 without running anything and without counting an anomaly. No runtime hook SHALL ever answer a non-200 status (a non-200 `/suspend` terminates the MicroVM, measured 2026-09-15). The bound on a `/suspend` `/resume` flood from an `allPorts` holder is the client (one reconnect per cut on the 0.5 s → 4 s backoff, `ReconnectBudget` ending a handle after four futile cuts) and the IAM the holder already has (it can `TerminateMicrovm`), documented in `SECURITY.md` T2.

#### Scenario: burst of suspends is idempotent
- **WHEN** an integration test posts `/suspend` ten times within one second after `/run`
- **THEN** all ten answer 200, exactly one has `outcome: "changed"` and nine have `outcome: "unchanged"` (a repeat never changes the phase and no anomaly is counted), `suspend_generation` is 1 and `streams_closed` is reported only by the accepted one

#### Scenario: a forged pair never makes rayd skip the real suspend
- **WHEN** an integration test posts a forged `/suspend` (`changed`) and `/resume` (`changed`) and then, 1 s later, the `/suspend` the platform sends for a real checkpoint, followed by a 30 s jump of the session clock (the freeze) and the real `/resume`
- **THEN** the real `/suspend` answers `changed` with its streams closed and the gate shut, the real `/resume` answers `changed` with `resume_generation` bumped, the kernels probed and the 30 s added to `suspended_total` (the running clock does not absorb the freeze), and `hook_anomalies` is 0

#### Scenario: back-to-back cycles are never refused
- **WHEN** an integration test posts five `/suspend` + `/resume` cycles with no gap between them
- **THEN** every call answers 200 `changed`, `resume_generation` reaches 5 and `hook_anomalies` stays 0

### Requirement: A suspend that never freezes the VM is recovered
After an accepted `/suspend`, `rayd` SHALL run a watchdog ticking every 1 s on the monotonic clock: a tick whose elapsed time exceeds 5 s (`FREEZE_THRESHOLD`, the signature of a real checkpoint because `CLOCK_MONOTONIC` advances during a suspension, measured 2026-09-15) ends the watchdog; if 20 s (`SUSPEND_GATE_TIMEOUT`) of unfrozen ticks pass with the phase still `Suspending` at the same `suspend_generation`, `rayd` SHALL reopen the stream gate and move the phase to `Resumed` without bumping `resume_generation` or `suspended_total`, mark the suspend `stale_recovered`, count one anomaly and log `stale_suspend_recovered`. A `/resume` arriving after such a recovery SHALL be accepted as a real resume (generation bump, `resume_after_stale_recovery` logged) without adding anything to `suspended_total`: the VM never froze, so the running clock keeps counting and a span would make it go backwards; any other `/resume` from `Resumed` SHALL stay `unchanged`.

#### Scenario: forged suspend loses no data
- **WHEN** the e2e forges `/suspend` on a sandbox with a background `tick` loop, a PTY, a file and `x = 42` in the kernel, and the MicroVM stays `RUNNING`
- **THEN** within `reconnect_timeout` the handle receives ticks again with `reconnects == 1`, `files.read` returns the file, `run_code("x").text == "42"`, `commands.list()` still shows the loop and the PTY, and `get_health().resume_generation` is unchanged

#### Scenario: real suspend is not recovered
- **WHEN** an integration test posts `/suspend`, jumps the test clock by 30 s and posts `/resume`
- **THEN** `/resume` answers `changed`, no `stale_suspend_recovered` line is logged and `hook_anomalies` is unchanged

#### Scenario: resume after a recovery
- **WHEN** an integration test posts `/suspend`, waits past the (test-scaled) gate timeout without a freeze, then posts `/resume`
- **THEN** `Start` succeeded in between with the generation unchanged, and the `/resume` answers `changed` with `resume_generation` incremented and `resume_after_stale_recovery` logged

### Requirement: SDK reconnects through a stale suspend
When a stream ends with `suspending` and `get-microvm` reports `RUNNING`, the SDK SHALL re-subscribe as soon as `Health` reports `agent_ready` and `kernel_ready` without requiring a new `resume_generation`, and SHALL retry a re-subscription refused with `UNAVAILABLE suspending` on the jittered backoff (0.5 s → 4 s) inside `reconnect_timeout` instead of failing; the SDK SHALL warn once per generation when `SandboxHealth.hook_anomalies` is non-zero.

#### Scenario: gate reopens on the third attempt
- **WHEN** the unit fake ends a `Start` stream with `suspending`, keeps `get-microvm` at `RUNNING`, refuses two `Connect` calls with `UNAVAILABLE suspending` and accepts the third
- **THEN** the handle continues with `reconnects == 1`, no exception reaches the caller and the same holds for the async client

#### Scenario: watch and execution handles retry the gate the same way
- **WHEN** the unit fake ends a live `WatchDir` (or an in-flight `Execute` past `started`) with `UNAVAILABLE suspending`, keeps `get-microvm` at `RUNNING`, refuses two re-subscriptions (`WatchDir`, `Reattach`) with `UNAVAILABLE suspending` and accepts the third
- **THEN** the watch keeps delivering events with `reconnects == 1` and no `on_exit`, the `run_code` call returns its result with exactly one `Execute` and three `Reattach` requests seen by the fake, `resume_generation` is unchanged, and the same holds for the async client


## MODIFIED Requirements

### Requirement: /resume bumps the generation, records the clock offset and probes every kernel
`POST /resume` SHALL, on the first call after a `/suspend`, bump `resume_generation`, add the suspended span to `suspended_total`, record `clock_offset_ms = wall_delta − monotonic_delta` since the matching `/suspend`, reopen the stream gate, send the sidecar `resume` op (a `kernel_info` probe per live kernel, 5 s each, at most 8 concurrently) bounded by a 12 s budget, mark `kernel_state_lost = true` when any kernel failed the probe or the budget expired (recomputed at every `/resume`; `false` when every kernel answered or with `--no-sidecar`), send `restart_context{context_id, envs}` in the background for every kernel that failed (its in-flight executions end with `KernelRestarted`), spawn the reseed in the background as an advisory op (the sidecar answers immediately with `reseeded`/`deferred`/`failed`; a timeout never counts towards the kill switch), re-check the IMDS rule when the block is installed (clearing `imds_blocked` and logging `imds_rule_missing` if it vanished), log `resume_generation`, `clock_offset_ms`, `suspended_ms`, `probe_ms`, `kernels_alive`, `kernels_lost` (warning above 5000 ms of offset) and answer 200 with `kernel_state_lost` in the body. A repeated `/resume` SHALL answer `unchanged` without re-probing, except after a stale-suspend recovery, where it SHALL be accepted as the real resume (`resume_after_stale_recovery`). A `/resume` that changes the phase SHALL never be refused (no rate limit: see hook-defense). `rayd` caches no credentials and holds no outbound connections, so nothing else is invalidated.

#### Scenario: resume after a suspend
- **WHEN** an integration test posts `/suspend` and then `/resume` with every kernel alive
- **THEN** `/resume` answers 200 with `outcome: "changed"` and `kernel_state_lost: false`, `Health` reports `resume_generation 1` and `kernel_state_lost false`, and the fake sidecar received `resume` and then `reseed`

#### Scenario: a kernel lost its state
- **WHEN** the fake sidecar answers the probe with `alive: false` for `ctx-a`
- **THEN** it receives `restart_context` for `ctx-a`, `Health.kernel_state_lost` is `true`, an execution in flight on `ctx-a` ended with `KernelRestarted`, and a later `/resume` where every kernel answers clears the flag

#### Scenario: probe over budget still answers
- **WHEN** the fake sidecar delays its `resume` reply by 13 s
- **THEN** `/resume` answers 200 within 13 s of the request with `kernel_state_lost: true` and `probe_timeout` logged

#### Scenario: slow reseed never restarts the sidecar
- **WHEN** the fake sidecar delays its `reseed` reply by 20 s on three consecutive `/resume` cycles
- **THEN** `sidecar_restarts` stays 0, each cycle logs `advisory_op_timeout: true`, and `Execute` succeeds after the third cycle

### Requirement: Health exposes the resume state
`HealthService.Health` SHALL report `resume_generation` (accepted `/resume` transitions this boot), `clock_offset_ms` (0 before the first resume), `kernel_state_lost` as decided by `/resume`, `imds_blocked` (field 9, `false` until the IMDS block is verified) and `hook_anomalies` (field 10, anomalous hook calls and stale-suspend recoveries this boot); `agent_ready` SHALL stay `true` while `Suspending` and `Resumed`; `uptime_ms` SHALL remain monotonic since boot, suspended time included. The SDK SHALL expose `sbx.get_health() -> SandboxHealth(agent_ready, kernel_ready, agent_version, uptime_ms, sandbox_id, resume_generation, clock_offset_ms, kernel_state_lost, imds_blocked, hook_anomalies)`, warn once per generation when `hook_anomalies` is non-zero, and warn once when `imds_blocked` is `False` on a sandbox created with an execution role.

#### Scenario: generation after a real pause
- **WHEN** the e2e reads `get_health()` before `pause()` and after `resume()`
- **THEN** `resume_generation` increased by exactly one, `kernel_state_lost` is `False` and `abs(clock_offset_ms) <= 5000`

#### Scenario: new fields default on an older agent
- **WHEN** the SDK reads `Health` from a fake that omits the two new fields
- **THEN** `SandboxHealth.imds_blocked` is `False`, `hook_anomalies` is `0` and no warning is logged for a sandbox without a role

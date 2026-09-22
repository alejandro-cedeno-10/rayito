# suspend-resume Specification

## Purpose
TBD - created by archiving change m5-pty-suspend-resume. Update Purpose after archive.
## Requirements
### Requirement: /suspend always answers 200 and never destroys state
`POST /suspend` SHALL always answer HTTP 200 (a non-200 terminates the MicroVM, measured 2026-09-15), inside 80 % of the declared `suspendTimeoutInSeconds` (24 s) with a design target below 5 s, and SHALL be idempotent: the first call of a cycle bumps `suspend_generation`, moves the phase to `Suspending` and runs the checklist; a repeated call answers `unchanged` and runs nothing but `sync`. `rayd` SHALL NOT kill, signal or interrupt any process, PTY, execution, kernel or the sidecar, SHALL NOT wait for executions or processes to finish, and SHALL respond 200 even when a step fails (the failure is logged).

#### Scenario: idempotent suspend
- **WHEN** an integration test posts `/suspend` twice
- **THEN** both answer 200, the first with `outcome: "changed"` and `streams_closed` equal to the number of open streams, the second with `outcome: "unchanged"` and `streams_closed: 0`, and `suspend_generation` is 1

#### Scenario: suspend before run
- **WHEN** `/suspend` arrives before `/run`
- **THEN** it answers 200 with `outcome: "illegal"` and the phase does not change

### Requirement: /suspend closes every client stream with a form the client can recognise
On the first `/suspend` of a cycle `rayd` SHALL stop accepting new streams (`UNAVAILABLE` with details `suspending` for `Start`, `Connect`, `Create`, `WatchDir`, `Read`, `Write`, `Execute`, `Reattach`) and SHALL close every live client stream before answering: `Process.Start`/`Connect` with an in-stream `EndEvent{exited:false, status:"suspending", exit_code:0, error:{code:"suspending"}}` followed by a clean end; `Pty.Create`/`Connect` with `PtyExited{exited:false, status:"suspending", error:{code:"suspending"}}`; `WatchDir`, `Read`, `Execute` and `Reattach` with the gRPC status `UNAVAILABLE` and details `suspending` (an `Execute`/`Reattach` closed this way SHALL NOT emit `ExecutionEnd` and SHALL NOT interrupt the execution); an in-flight `Write` SHALL be aborted (temporary file removed, destination untouched, at most 200 ms of body drained) with `UNAVAILABLE suspending`. `rayd` SHALL wait at most 2 s for the wrapped streams to drop, send `quiesce` to the sidecar with a 2 s timeout, call `sync`, and log `streams_closed`, `streams_pending` and `suspend_ms`.

#### Scenario: eight streams closed with their forms
- **WHEN** an integration test holds a `Start` and a `Connect` on `sleep 30`, a PTY stream, a `WatchDir`, a half-consumed `Read`, an `Execute` of `sleep 3`, a `Reattach` on it and a `Write` mid-body, and posts `/suspend`
- **THEN** within 2 s the process streams end with `EndEvent{status:"suspending"}`, the PTY stream with `PtyExited{status:"suspending"}`, the other five with `UNAVAILABLE` and details `suspending`, the hook body reports `streams_closed: 8`, the fake sidecar received `quiesce` and no `interrupt`, the `Write` temporary is gone, and `sleep 30` and the shell are still listed

#### Scenario: new streams refused while suspending
- **WHEN** `/suspend` has been acknowledged and no `/resume` yet
- **THEN** `Start`, `Create`, `WatchDir` and `Execute` fail with `UNAVAILABLE` and details `suspending`

### Requirement: Server deadlines exclude suspended time
`rayd` SHALL keep a running clock equal to the monotonic clock minus the total time spent between each accepted `/suspend` and its `/resume` (`suspended_total`, accumulated at `/resume`). Every server-enforced deadline — process and PTY `timeout_ms`, the 5 s SIGKILL grace, execution `interrupt_at` and `restart_at`, and the 30 s retention of ended processes, PTYs and executions — SHALL be evaluated against the running clock, so a deadline armed before a suspension keeps exactly the remaining budget it had when the suspension began. tokio sleeps SHALL be treated as wake-ups only and re-validated against the running clock (`CLOCK_MONOTONIC` advances during a suspension, measured 2026-09-15). A sidecar op timeout that expires after a `/resume` newer than the call SHALL be logged as `op_timeout_across_resume` and SHALL NOT count towards the three-consecutive-timeouts kill switch.

#### Scenario: process timeout re-armed
- **WHEN** the SDK starts `sleep 60` with `timeout=25`, pauses the sandbox about 3 s later, keeps it suspended for 30 s and resumes
- **THEN** the process is still listed right after `resume()`, and `commands.connect(pid).wait()` raises `TimeoutException` within 30 s of the resume (about 22 s)

#### Scenario: in-process simulation
- **WHEN** an integration test starts a process with `timeout_ms: 1500`, posts `/suspend` at 0.5 s, sleeps 2 s and posts `/resume`
- **THEN** the process is alive at `/resume` and ends with `status:"timeout"` about 1 s after it; an `Execute` with `timeout_ms: 1500` under the same sequence receives its `interrupt` about 1 s after the resume, not at the resume

#### Scenario: op timeout across a resume is not counted
- **WHEN** three sidecar ops issued before a `/suspend` time out after the matching `/resume`
- **THEN** each caller receives `UNAVAILABLE`, `sidecar_restarts` stays 0 and the next execution succeeds

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

### Requirement: Kernel, processes, PTYs and watches survive a real pause
Against real AWS, a sandbox paused with `pause()` and resumed with `resume()` SHALL keep the variables of every code context, every background process, every PTY and every inotify watch; `commands.connect(pid)` on a surviving process SHALL deliver the correct end event; a PTY SHALL answer input after re-attach; a re-issued watch SHALL deliver new events.

#### Scenario: SPEC section 6 step 5
- **WHEN** the e2e defines `x = 42`, starts `sleep 4000` in the background, opens a PTY and a watch, calls `sbx.pause()` (returns `True`; a second `pause()` returns `False`), waits 30 s and calls `sbx.resume()`
- **THEN** `sbx.run_code("x").text == "42"`, the `sleep 4000` pid is listed and `commands.connect(pid).wait()` after `commands.kill(pid)` raises `CommandExitException` with `exit_code == 137`, `pty.connect(pid, from_seq=last_seq + 1)` answers `echo resumed-$((6*7))` with `resumed-42`, and a file written after the resume appears in `watch.get_new_events()`

#### Scenario: run_code across a pause
- **WHEN** the e2e runs `import time; time.sleep(25); 'slept'` in a thread, pauses the sandbox 3 s later, waits 5 s and resumes
- **THEN** the call returns within 60 s of the resume with `text == "'slept'"` and `error is None`

### Requirement: SDK pause, resume and connect
`Sandbox.pause(*, wait=True) -> bool` SHALL read `get-microvm` first and return `False` without calling `suspend-microvm` when the state is already `SUSPENDING|SUSPENDED` (AWS's `suspend-microvm` is idempotent: on a `SUSPENDED` VM it answers 200, never `ConflictException`, measured 2026-09-16), otherwise call `suspend-microvm` through the 2 TPS token bucket, return `False` on `ConflictException`, and with `wait` poll `get-microvm` until `SUSPENDED`; `Sandbox.resume(*, wait=True)` SHALL call `resume-microvm` (a conflict because the sandbox is already running is not an error), re-mint the JWE, and with `wait` poll `Health` until `agent_ready and kernel_ready` recording `resume_generation`; `Sandbox.connect(sandbox_id, ...)` on a `SUSPENDED` sandbox SHALL call `resume-microvm` only when the idle policy has `auto_resume` disabled, otherwise the readiness poll itself resumes it. The JWE refresher SHALL keep running during a pause. `create()` and `connect()` SHALL accept `reconnect_timeout` (default 60 s = the image's `resumeTimeoutInSeconds` 30 + 30). `IdlePolicy` SHALL default to `max_idle_seconds=300`, `suspended_duration_seconds=None` (resolved to `timeout − max_idle_seconds`) and `auto_resume=True`, with `max_idle_seconds >= 60` validated. `AsyncSandbox` SHALL offer the same surface.

#### Scenario: pause and resume timings
- **WHEN** the e2e calls `pause()` and later `resume()` on a running sandbox
- **THEN** `pause()` returns `True` and `get_info().state == "SUSPENDED"` within 30 s, and `resume()` returns with `get_info().state == "RUNNING"` within 30 s

#### Scenario: resume re-mints
- **WHEN** a unit test calls `resume()` against the stubbed control plane and the fake `rayd`
- **THEN** one `resume_microvm` and one `create_microvm_auth_token` are issued, `Health` is polled until `kernel_ready`, and `get_health().resume_generation` reflects the fake's value

### Requirement: Reconnection contract on stream cuts and unary failures
When a stream ends with the `suspending` form or a stream or unary RPC fails with `UNAVAILABLE` (proxy 502/503 or the `suspending` phase gate), a connection reset, `GOAWAY` or EOF, the SDK SHALL poll `Health` with a jittered backoff from 0.5 s doubling to 4 s for at most `reconnect_timeout`, checking `get-microvm` every 5 s and stopping early with `SandboxNotFoundException` on `TERMINATING|TERMINATED` or with `SandboxStateException` on `SUSPENDED` without auto-resume; on `agent_ready and kernel_ready` it SHALL record the new `resume_generation`, warn when `abs(clock_offset_ms) > 5000`, and: retry the unary once; re-subscribe a `CommandHandle` with `Connect(pid, from_seq=last_seq + 1)` and a `PtyHandle` with `Pty.Connect(pid, from_seq=last_seq + 1)` (falling back to `from_seq=0` with a warning on `OUT_OF_RANGE`, storing `NotFoundException` on `NOT_FOUND`) and continue delivering output transparently, counting `reconnects`; re-issue a `WatchHandle`'s `WatchDir` with the same parameters without calling `on_exit`; continue a `run_code` whose `started` was already received with `Reattach(context_id, execution_id, from_seq=last_seq + 1)` feeding the same `Execution` (a cut before `started` SHALL NOT re-run the cell). Concurrent reconnections of one sandbox SHALL share a single `Health` poll (lock plus generation check). Only a caller that may wake the sandbox SHALL probe `Health` while `get-microvm` reports `SUSPENDING|SUSPENDED`: a unary retry, the opening of a new stream, and an in-flight foreground `run`/`run_code` whose sandbox has no pending `pause()` from this `Sandbox` instance. A background `CommandHandle`, a `PtyHandle`, a `WatchHandle`, and an in-flight foreground stream cut by this instance's `pause()` SHALL instead sleep outside the lock, polling `get-microvm` every 5 s without probing `Health` and without consuming `reconnect_timeout` (whatever the idle policy), SHALL wake as soon as another call records a new `resume_generation` or `close()` runs, and SHALL start the `reconnect_timeout` budget only once the state leaves `SUSPENDING|SUSPENDED`; a terminal state SHALL end the wait with `SandboxNotFoundException`. `pause()` SHALL mark the pending pause before calling `suspend-microvm`, and `resume()` or a new `resume_generation` SHALL clear it. A `disconnect()`ed handle SHALL never reconnect. When the poll fails, the original failure SHALL surface as `SandboxStateException` (suspending), `SandboxNotFoundException` (terminal) or `SandboxException` (deadline). Channels SHALL cap grpc's reconnect backoff at 2 s (`grpc.initial_reconnect_backoff_ms 500`, `grpc.min_reconnect_backoff_ms 500`, `grpc.max_reconnect_backoff_ms 2000`).

#### Scenario: background handle survives a suspend
- **WHEN** a unit test iterates a background `CommandHandle` while the fake `rayd` ends its stream with `EndEvent{status:"suspending"}`, answers `Health` with `UNAVAILABLE` three times and then with `resume_generation + 1`
- **THEN** the handle receives the rest of the output, `reconnects == 1`, and the fake saw `Connect(pid, from_seq == last_seq + 1)`

#### Scenario: unread handle reconnects on first read
- **WHEN** a background handle is never iterated before the fake suspends and resumes, and the test then calls `wait()`
- **THEN** `wait()` returns the process's result after one reconnect

#### Scenario: run_code reattaches
- **WHEN** a unit test runs `slow 2` and the fake aborts the `Execute` stream with `UNAVAILABLE suspending` after `started`, then resumes
- **THEN** the fake saw `Reattach(context_id, execution_id, from_seq == last_seq + 1)` and the returned `Execution` has the cell's result and no error

#### Scenario: cut before started is not retried
- **WHEN** the fake aborts `Execute` with `UNAVAILABLE suspending` before `started`
- **THEN** `run_code` raises `SandboxStateException` and the fake received exactly one `Execute`

#### Scenario: unary retried once after reconnect
- **WHEN** the fake answers `SendSignal` with `UNAVAILABLE suspending` once and `Health` recovers with a new generation
- **THEN** `commands.kill(pid)` returns `True` and the fake received two `SendSignal` calls

#### Scenario: terminated during the poll
- **WHEN** `Health` never answers and the stubbed `get_microvm` reports `TERMINATED`
- **THEN** the handle's `wait()` raises `SandboxNotFoundException` before the reconnect deadline

#### Scenario: suspended without auto-resume
- **WHEN** a foreground `commands.run` is cut by a `suspending` end, `Health` never answers and `get_microvm` reports `SUSPENDED` with `autoResumeEnabled: false`
- **THEN** `SandboxStateException` is raised and the message tells the caller to call `resume()`

#### Scenario: background handle sleeps through a suspension
- **WHEN** a background handle is cut by a `suspending` end while `get_microvm` reports `SUSPENDED` (with or without auto-resume) for longer than `reconnect_timeout`
- **THEN** the handle's consumer is still waiting, no `Health` call was made, and once `get_microvm` reports `RUNNING` and the fake has a new generation the handle reconnects after exactly one `Health` call with `reconnects == 1`

#### Scenario: resume wakes dormant handles at once
- **WHEN** a background handle is dormant and the same `Sandbox` calls `resume()`
- **THEN** the handle re-subscribes within 2 s, before its next `get_microvm` check

#### Scenario: dormant handle does not block a foreground call
- **WHEN** a background handle is dormant on a `SUSPENDED` sandbox with auto-resume and `commands.run("echo hola")` is called
- **THEN** the call probes `Health` immediately, returns `hola` before the next 5 s state check, and the dormant handle reconnects with the generation the call recorded

#### Scenario: explicit pause keeps in-flight foreground streams dormant
- **WHEN** a foreground `commands.run` or a `run_code` is in flight, the same `Sandbox` calls `pause(wait=False)` and the fake suspends while `get_microvm` reports `SUSPENDED` with auto-resume
- **THEN** no `Health` call is made until `resume()`, after which the command result arrives through `Connect` and the cell's result through `Reattach`, and the pending pause is cleared

#### Scenario: watch re-issued
- **WHEN** a `WatchHandle` is live when the fake suspends and resumes, and a file is created afterwards
- **THEN** the fake received two `WatchDir` calls with the same request, `is_running` is still `True`, `on_exit` was not called and `get_new_events()` contains the new file

### Requirement: Auto-resume through the SDK
A sandbox created with an idle policy (`auto_resume=True`) that AWS suspended for inactivity SHALL serve the next SDK call: either the proxy retains the request during the auto-resume or the SDK's reconnection contract recovers it; kernel state SHALL survive.

#### Scenario: idle suspend then a command
- **WHEN** the e2e creates a sandbox with `IdlePolicy(max_idle_seconds=60, suspended_duration_seconds=600, auto_resume=True)`, runs `y = 7`, waits until `get_info().state == "SUSPENDED"` (at most 240 s) and then calls `commands.run("echo back")`
- **THEN** the output is `back` within 30 s, `run_code("y").text == "7"`, `get_health().resume_generation == 1` and `get_info().state == "RUNNING"`

### Requirement: Suspend and resume logging hygiene
`rayd` SHALL log for suspend/resume only `suspend_generation`, `resume_generation`, `clock_offset_ms`, `suspended_ms`, `streams_closed`, `streams_pending`, `suspend_ms`, `probe_ms`, `kernels_alive`, `kernels_lost`, `kernel_state_lost`, context ids and error names; never output, envs, code or tokens. The SDK SHALL log reconnections with the sandbox id, the reason class and the generation only.

#### Scenario: resume log line
- **WHEN** `/resume` completes
- **THEN** one `resume recorded` line carries `resume_generation`, `clock_offset_ms`, `suspended_ms`, `probe_ms`, `kernels_alive` and `kernels_lost`, and no line contains execution output


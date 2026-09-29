## MODIFIED Requirements

### Requirement: Reconnection contract on stream cuts and unary failures
When a stream ends with the `suspending` form or a stream or unary RPC fails with `UNAVAILABLE` (proxy 502/503 or the `suspending` phase gate), a connection reset, `GOAWAY`, EOF, or `CANCELLED` whose `details`/`debug_error_string` carries a reset marker (`RST_STREAM`, `GOAWAY`, `Received RST`, `Socket closed`, `Connection reset`, or `Stream removed` — grpcio's message for a proxy `RST_STREAM` that lands before the caller's own deadline, AWS_API_NOTES.md #33), the SDK SHALL poll `Health` with a jittered backoff from 0.5 s doubling to 4 s for at most `reconnect_timeout`, checking `get-microvm` every 5 s and stopping early with `SandboxNotFoundException` on `TERMINATING|TERMINATED` or with `SandboxStateException` on `SUSPENDED` without auto-resume; on `agent_ready and kernel_ready` it SHALL record the new `resume_generation`, warn when `abs(clock_offset_ms) > 5000`, and: retry the unary once; re-subscribe a `CommandHandle` with `Connect(pid, from_seq=last_seq + 1)` and a `PtyHandle` with `Pty.Connect(pid, from_seq=last_seq + 1)` (falling back to `from_seq=0` with a warning on `OUT_OF_RANGE`, storing `NotFoundException` on `NOT_FOUND`) and continue delivering output transparently, counting `reconnects`; re-issue a `WatchHandle`'s `WatchDir` with the same parameters without calling `on_exit`; continue a `run_code` whose `started` was already received with `Reattach(context_id, execution_id, from_seq=last_seq + 1)` feeding the same `Execution` (a cut before `started` SHALL NOT re-run the cell). A `CANCELLED` without a reset marker (what a local `call.cancel()` produces, e.g. `CommandHandle.disconnect()`, `WatchHandle.stop()`) SHALL NOT be reconnectable. Concurrent reconnections of one sandbox SHALL share a single `Health` poll (lock plus generation check). Only a caller that may wake the sandbox SHALL probe `Health` while `get-microvm` reports `SUSPENDING|SUSPENDED`: a unary retry, the opening of a new stream, and an in-flight foreground `run`/`run_code` whose sandbox has no pending `pause()` from this `Sandbox` instance. A background `CommandHandle`, a `PtyHandle`, a `WatchHandle`, and an in-flight foreground stream cut by this instance's `pause()` SHALL instead sleep outside the lock, polling `get-microvm` every 5 s without probing `Health` and without consuming `reconnect_timeout` (whatever the idle policy), SHALL wake as soon as another call records a new `resume_generation` or `close()` runs, and SHALL start the `reconnect_timeout` budget only once the state leaves `SUSPENDING|SUSPENDED`; a terminal state SHALL end the wait with `SandboxNotFoundException`. `pause()` SHALL mark the pending pause before calling `suspend-microvm`, and `resume()` or a new `resume_generation` SHALL clear it. A `disconnect()`ed handle SHALL never reconnect, even when the cut that follows the `disconnect()` call is itself shaped like a reconnectable reset: the disconnected/stopped check SHALL run before the reset classification. When the poll fails, the original failure SHALL surface as `SandboxStateException` (suspending), `SandboxNotFoundException` (terminal) or `SandboxException` (deadline). Channels SHALL cap grpc's reconnect backoff at 2 s (`grpc.initial_reconnect_backoff_ms 500`, `grpc.min_reconnect_backoff_ms 500`, `grpc.max_reconnect_backoff_ms 2000`).

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

#### Scenario: a proxy RST_STREAM reconnects like any other reset
- **WHEN** a live `CommandHandle`'s stream fails mid-read with `CANCELLED` and details `"Stream removed"` (grpcio's message for a proxy `RST_STREAM`, AWS_API_NOTES.md #33), and the fake `rayd`'s agent is still reachable
- **THEN** the handle reconnects through `Connect(pid, from_seq == last_seq + 1)` with `reconnects == 1`, exactly as it would for an `UNAVAILABLE "Socket closed"` cut, and repeating the same cut three times ends the handle with the M2 classification after exactly three reconnects

#### Scenario: a local cancel is never mistaken for a proxy reset
- **WHEN** a `CANCELLED` carries no reset marker (e.g. details `"Locally cancelled by application!"`), whether from a genuine local `call.cancel()` or injected directly in a test
- **THEN** the handle is not reconnected: a live handle raises the M2/unary classification instead of retrying, and a handle whose `disconnect()` already ran before the cut (even one shaped exactly like `CANCELLED "Stream removed"`) ends with "handle desconectado" and issues no `Connect` call

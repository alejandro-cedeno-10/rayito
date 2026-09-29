## MODIFIED Requirements

### Requirement: Reconnection contract on stream cuts and unary failures
The SDK SHALL classify a `ConnectError` as reconnectable when it is the phase gate (`Unavailable` with `rawMessage` `suspending`/`terminating`) or a stream reset: `Unavailable` that is neither gate (proxy `HTTP 429/502/503/504`, `REFUSED_STREAM`, `ECONNREFUSED`, `ETIMEDOUT`), `Aborted` (`ECONNRESET`, destroyed stream), `Canceled` whose `rawMessage` starts with `http/2 stream closed`, or `Internal` whose `rawMessage` starts with `http/2 stream closed` or equals `protocol error: missing status` or whose cause chain has a Node `code` starting with `ERR_HTTP2_` or equal to `ECONNRESET`/`EPIPE`; never `DeadlineExceeded`, the kernel gate, a proxy 403, or a `Canceled` raised by the SDK's own `AbortSignal`. In-stream `EndEvent{status "suspending"}` and `PtyExited{status "suspending"}` SHALL count as reconnect signals. On such a signal the SDK SHALL poll `Health` with a jittered backoff from 0.5 s doubling to 4 s (±25 %) for at most `reconnectTimeoutMs`, treating `Unavailable`, `DeadlineExceeded` and every stream-reset form above as "not yet" (never as a failed reconnect), checking `get-microvm` every 5 s and stopping early with `SandboxNotFoundError` on `TERMINATING|TERMINATED` or `SandboxStateError` on `SUSPENDED` without auto-resume; after a `suspending` signal only a `Health` with a new `resumeGeneration` counts; on success it SHALL record the generation, warn when `|clockOffsetMs| > 5000`, and: retry the unary once; re-subscribe a `CommandHandle` with `Connect(pid, fromSeq = lastSeq + 1)` and a `PtyHandle` with `Pty.Connect(pid, fromSeq = lastSeq + 1)` (falling back to `fromSeq 0` with a warning on `OutOfRange`, storing `NotFoundError` on `NotFound`), continuing output transparently and counting `reconnects`; re-issue a `WatchHandle`'s `WatchDir` with the same request without calling `onExit`; continue a `runCode` whose `started` arrived with `Reattach(contextId, executionId, fromSeq = lastSeq + 1)` feeding the same `Execution` (a cut before `started` SHALL NOT re-run the cell). Concurrent reconnections of one sandbox SHALL share one `Health` poll (in-flight promise plus generation check). Only a caller that may wake the sandbox SHALL probe `Health` while `get-microvm` reports `SUSPENDING|SUSPENDED`: a unary retry, the opening of a new stream, and an in-flight foreground `run`/`runCode` whose sandbox has no pending `pause()` from this instance. A background `CommandHandle`, a `PtyHandle`, a `WatchHandle` and a foreground stream cut by this instance's `pause()` SHALL instead wait dormant: `get-microvm` every 5 s without probing `Health` and without consuming `reconnectTimeoutMs`, waking as soon as another call records a new generation or `close()` runs, and starting the budget only once the state leaves `SUSPENDING|SUSPENDED`; a terminal state SHALL end the wait with `SandboxNotFoundError`. A `disconnect()`ed handle SHALL never reconnect, even when the cut that follows the `disconnect()` call is itself shaped like a reconnectable reset. Three consecutive reconnects without a new generation SHALL end the handle with the M2 classification. When the poll fails the original failure SHALL surface as `SandboxStateError` (suspending), `SandboxNotFoundError` (terminal) or `SandboxError` (deadline).

#### Scenario: classification truth table
- **WHEN** `isReconnectable` receives `ConnectError`s built for every row above (including `Canceled` with `rawMessage "This operation was aborted"`, `DeadlineExceeded`, `Unavailable "kernel not ready: x"`, `PermissionDenied "HTTP 403"`)
- **THEN** it returns `true` for every reset and phase-gate row and `false` for the four exclusions

#### Scenario: background handle survives a suspend
- **WHEN** a background `CommandHandle` is iterated while the fake ends its stream with `EndEvent{status "suspending"}`, answers `Health` with `Unavailable` three times and then with `resumeGeneration + 1`
- **THEN** the handle receives the rest of the output, `reconnects === 1`, and the fake saw `Connect(pid, fromSeq === lastSeq + 1)`

#### Scenario: a dropped session during the Health poll is not yet
- **WHEN** a background handle is cut by `suspendResume({ unavailableCalls: 1 })` and the next three `Health` probes fail with `Aborted "read ECONNRESET"`, `Internal "http/2 stream closed with error code INTERNAL_ERROR (0x2)"` and `Canceled "http/2 stream closed with error code CANCEL (0x8)"`, and separately a foreground `run` is cut and the next probe fails with the `Aborted` form, and separately `create()` sees the `Aborted` and `Internal` forms on its first two boot probes
- **THEN** every handle still reconnects (`reconnects` 1, full output, five probes for the background case), `create()` resolves after three probes without terminating the VM, and no plain `SandboxError` surfaces

#### Scenario: unread handle reconnects on first read
- **WHEN** a background handle is never iterated before the fake suspends and resumes, and the test then awaits `wait()`
- **THEN** `wait()` resolves with the process's result after one reconnect

#### Scenario: OutOfRange fallback
- **WHEN** the fake answers the re-subscribe `Connect(fromSeq)` with `OutOfRange`
- **THEN** a second `Connect(pid, fromSeq 0)` is issued, a warning is logged, and the handle completes

#### Scenario: PTY and watch re-attach
- **WHEN** a `PtyHandle` and a `WatchHandle` are live when the fake suspends and resumes, and a file is created afterwards
- **THEN** the fake saw `Pty.Connect(pid, fromSeq === lastSeq + 1)`, two `WatchDir` calls with identical requests, `w.isRunning === true`, `onExit` was not called, `w.getNewEvents()` contains the new file and both handles report `reconnects === 1`

#### Scenario: runCode reattaches
- **WHEN** `runCode("slow 2")` is cut with `Unavailable suspending` after `started`, then the fake resumes
- **THEN** the fake saw `Reattach(contextId, executionId, fromSeq === lastSeq + 1)` and the `Execution` has the cell's result and no error

#### Scenario: cut before started is not retried
- **WHEN** the fake aborts `Execute` with `Unavailable suspending` before `started`
- **THEN** `runCode` rejects with `SandboxStateError` and the fake received exactly one `Execute`

#### Scenario: unary retried once after reconnect
- **WHEN** the fake answers `SendSignal` with `Unavailable suspending` once and `Health` recovers with a new generation
- **THEN** `commands.kill(pid)` resolves `true` and the fake received two `SendSignal` calls

#### Scenario: terminated during the poll
- **WHEN** `Health` never answers and the fake plane reports `TERMINATED`
- **THEN** `wait()` rejects with `SandboxNotFoundError` before the reconnect deadline

#### Scenario: suspended without auto-resume
- **WHEN** a foreground `commands.run` is cut by a `suspending` end, `Health` never answers and the plane reports `SUSPENDED` with `autoResume false`
- **THEN** it rejects with `SandboxStateError` whose message tells the caller to call `resume()`

#### Scenario: background handle sleeps through a suspension
- **WHEN** a background handle is cut by a `suspending` end while the plane reports `SUSPENDED` for longer than `reconnectTimeoutMs`
- **THEN** the handle is still pending, no `Health` call was made, and once the plane reports `RUNNING` and the fake has a new generation the handle reconnects after exactly one `Health` call with `reconnects === 1`

#### Scenario: resume wakes dormant handles at once
- **WHEN** a background handle is dormant and the same `Sandbox` calls `resume()`
- **THEN** the handle re-subscribes within 2 s, before its next `get-microvm` check

#### Scenario: dormant handle does not block a foreground call
- **WHEN** a background handle is dormant on a `SUSPENDED` sandbox with auto-resume and `commands.run("echo hola")` is called
- **THEN** the call probes `Health` immediately, resolves `hola` before the next 5 s state check, and the dormant handle reconnects with the generation that call recorded

#### Scenario: explicit pause keeps in-flight foreground streams dormant
- **WHEN** a foreground `commands.run` and a `runCode` are in flight, the same `Sandbox` calls `pause({ wait: false })` and the fake suspends while the plane reports `SUSPENDED` with auto-resume
- **THEN** no `Health` call is made until `resume()`, after which the command result arrives through `Connect` and the cell's result through `Reattach`, and the pending pause is cleared

#### Scenario: concurrent reconnects share one poll
- **WHEN** two background handles are cut in the same tick and the fake resumes
- **THEN** exactly one `Health` poll sequence ran and both handles reconnected with the same generation

#### Scenario: disconnected handles never reconnect and close ends waits
- **WHEN** a `disconnect()`ed handle's stream is cut, and separately `close()` runs while another handle is dormant
- **THEN** the first handle makes no `Health` or `Connect` call, and the second's `wait()` rejects with `SandboxError`

#### Scenario: a live command reconnects through Connect after a Canceled RST_STREAM
- **WHEN** a background `CommandHandle`'s stream is cut with `process.cut(Code.Canceled, "http/2 stream closed with error code CANCEL (0x8)")` (the fake's direct model of an AWS proxy `RST_STREAM(CANCEL)`, the same shape `@connectrpc/connect-node` produces for a real one) while the fake agent stays reachable
- **THEN** the handle reconnects through `Connect(pid, fromSeq === lastSeq + 1)` with `reconnects === 1`, exactly as it would for an `Unavailable "HTTP 502"` cut, and repeating that cut three times ends the handle with the M2 classification after exactly three `Connect` calls

#### Scenario: disconnect wins the race against a Canceled RST_STREAM cut
- **WHEN** `disconnect()` runs on a background `CommandHandle` and `process.cut(Code.Canceled, "http/2 stream closed with error code CANCEL (0x8)")` is then called on the same process
- **THEN** `wait()` rejects naming the handle as disconnected, the fake received no `Connect` call, and `reconnects` stays `0`

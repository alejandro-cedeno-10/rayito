## MODIFIED Requirements

### Requirement: Server-enforced timeout on the monotonic clock
When `StartRequest.timeout_ms > 0`, `rayd` SHALL send `SIGTERM` to the process group when the deadline expires on the running clock (the monotonic clock minus the time spent suspended; M5 replaces the M2 "tokio monotonic" wording because `CLOCK_MONOTONIC` advances during a suspension, measured 2026-09-15), `SIGKILL` 5 s of running time later if the process is still alive, and end the stream with `EndEvent{exited:false, status:"timeout", exit_code:128+signal, signal, error:{code:"deadline_exceeded"}}` regardless of how the process died after the `SIGTERM`. tokio sleeps SHALL be treated as wake-ups and re-validated against the running clock. `timeout_ms == 0` SHALL mean no limit. The SDK SHALL default `timeout` to 60 s, send `timeout_ms = timeout * 1000`, set the gRPC deadline to `timeout + 5 s`, and raise `TimeoutException` on the `timeout` status.

#### Scenario: sleep 10 with timeout 2
- **WHEN** the SDK calls `commands.run("sleep 10", timeout=2)`
- **THEN** `TimeoutException` is raised within 8 s and the `EndEvent` carried `status == "timeout"`, `exited == false`, `signal == 15` and `error.code == "deadline_exceeded"`

#### Scenario: paused time does not count
- **WHEN** a process started with `timeout_ms: 1500` is 0.5 s old at `/suspend`, the session stays suspended for 2 s and `/resume` arrives
- **THEN** the process is still alive at `/resume` and ends with `status:"timeout"` about 1 s later

### Requirement: Terminal events are retained for 30 seconds
After a process or PTY ends, its entry SHALL remain available to `Connect` for 30 s of running time (suspended time excluded), replaying `StartEvent`/`started`, ring data per `from_seq`, and the `EndEvent`/`exited`, after which `Connect` SHALL answer `NOT_FOUND`. At most 256 ended entries SHALL be retained at once; when an entry ends while 256 are already retained, `rayd` SHALL evict the oldest ended entries first, and `Connect` on an evicted pid SHALL answer `NOT_FOUND`. `SendInput`, `CloseStdin`, `SendSignal`, `Resize` and `Kill` on an ended pid SHALL answer `NOT_FOUND` immediately. `List` SHALL return only live entries.

#### Scenario: connect right after exit
- **WHEN** a process exited less than 30 s ago and the SDK calls `commands.connect(pid)`
- **THEN** `wait()` returns the process's `exit_code` without raising `NotFoundException`

#### Scenario: connect after retention
- **WHEN** a process exited more than 30 s of running time ago and the reaper ran
- **THEN** `Connect` fails with `NOT_FOUND`

#### Scenario: retention spans a pause
- **WHEN** a process exits 5 s before `/suspend`, the session stays suspended for 60 s and `Connect` arrives 5 s after `/resume`
- **THEN** `Connect` replays `StartEvent`, the ring and the `EndEvent`

### Requirement: Live process cap and phase gate
`rayd` SHALL accept at most 256 live processes and PTYs per sandbox, counted together in one registry; `Start` and `PtyService.Create` beyond the cap SHALL fail with `RESOURCE_EXHAUSTED` before spawning. `Start`, `Connect`, `PtyService.Create` and `PtyService.Connect` SHALL fail with `UNAVAILABLE` and details `suspending` or `terminating` while the session phase is `Suspending` or `Terminating`. `ProcessService.Connect`, `SendInput` and `CloseStdin` SHALL answer `FAILED_PRECONDITION` for a PTY pid; `ProcessService.SendSignal` SHALL accept process and PTY pids alike.

#### Scenario: 257th entry
- **WHEN** 255 processes and one PTY are alive and another `Start` arrives
- **THEN** it fails with `RESOURCE_EXHAUSTED` and nothing is spawned

#### Scenario: start while suspending
- **WHEN** the `/suspend` hook has been acknowledged and no `/resume` yet
- **THEN** `Start` fails with `UNAVAILABLE` and details `suspending`

#### Scenario: process RPC on a PTY pid
- **WHEN** `ProcessService.Connect{pid}` names a live PTY
- **THEN** the RPC fails with `FAILED_PRECONDITION` and `ProcessService.SendSignal{pid, 9}` on the same pid succeeds

### Requirement: List returns live processes with their kind
`List` SHALL return one `ProcessInfo{pid, config, tag, kind}` per live entry: `PROCESS_KIND_PROCESS` for processes and `PROCESS_KIND_PTY` for PTYs (with `config.cmd` = the shell, `config.args = ["-i","-l"]`, `config.envs` = the request envs, `config.cwd`, no tag). The SDK SHALL map it to `ProcessInfo(pid, cmd, args, envs, cwd, tag, kind="process"|"pty")`.

#### Scenario: background process listed then gone
- **WHEN** the SDK runs `h = commands.run("sleep 30", background=True, tag="m2")`
- **THEN** `commands.list()` contains an item with `pid == h.pid`, `kind == "process"`, `tag == "m2"`, and after the process ends it no longer does

#### Scenario: PTY listed with its kind
- **WHEN** the SDK creates a PTY and calls `commands.list()`
- **THEN** the list contains an item with the PTY's `pid`, `kind == "pty"` and `args == ("-i", "-l")`

### Requirement: SDK stream error contract
The SDK SHALL retry a stream open exactly once after a proxy 403 (`PERMISSION_DENIED` with `Received http2 header with status: 403`) by re-minting the JWE, only when no message has been consumed. A mid-stream `UNAVAILABLE`, connection reset, `GOAWAY` or EOF, and an in-stream `suspending` end, SHALL trigger the reconnection contract of the `suspend-resume` capability (poll `Health` with backoff for `reconnect_timeout`, re-subscribe with `Connect(pid, from_seq=last_seq + 1)`); only when that poll fails SHALL the failure be classified: `TERMINATING|TERMINATED` → `SandboxNotFoundException`, `SUSPENDED` without auto-resume → `SandboxStateException`, otherwise `SandboxException`. gRPC `FAILED_PRECONDITION` SHALL map to `InvalidArgumentException`, `OUT_OF_RANGE` to `NotFoundException`, `DEADLINE_EXCEEDED` on a command stream to `TimeoutException`.

#### Scenario: expired JWE at stream open
- **WHEN** the proxy answers 403 to the `Start` request
- **THEN** the SDK mints a new token and re-issues the `Start` once, and the command runs exactly once

#### Scenario: reset while the sandbox is terminated
- **WHEN** a background stream is reset, `Health` never answers and `get_microvm` reports `TERMINATED`
- **THEN** `wait()` raises `SandboxNotFoundException`

#### Scenario: reset while the sandbox comes back
- **WHEN** a background stream is reset and `Health` answers again with a higher `resume_generation`
- **THEN** the handle re-subscribes with `Connect(pid, from_seq=last_seq + 1)` and `wait()` returns the process's result

# process-lifecycle Specification

## Purpose
TBD - created by archiving change m2-processes-lifecycle. Update Purpose after archive.
## Requirements
### Requirement: Process start streams a StartEvent first
`ProcessService.Start` SHALL spawn the requested program in its own process group and answer with a server-stream whose first message is `StartEvent{pid}`, followed by zero or more `DataEvent`s and exactly one terminal `EndEvent`, with no message after the `EndEvent`. The SDK SHALL send `ProcessConfig{cmd:"/bin/bash", args:["-l","-c", <cmd>]}` for `commands.run`.

#### Scenario: echo through the login shell
- **WHEN** the SDK calls `commands.run("echo hola")`
- **THEN** the stream delivers `StartEvent`, then a `DataEvent{stdout:"hola\n", seq:1}`, then `EndEvent{exited:true, status:"exited", exit_code:0}` and the SDK returns `CommandResult(stdout="hola\n", stderr="", exit_code=0, error=None)`

#### Scenario: non-zero exit
- **WHEN** the SDK calls `commands.run("echo err >&2; exit 3")`
- **THEN** the stream ends with `EndEvent{exited:true, status:"exited", exit_code:3}` and the SDK raises `CommandExitException` with `exit_code == 3`, `stderr == "err\n"`, `error == "exited"`

#### Scenario: empty command
- **WHEN** `Start` is called with an empty `ProcessConfig.cmd`
- **THEN** the RPC fails with gRPC `INVALID_ARGUMENT` before any message

### Requirement: Default identity is uid 1000 and root is refused unless the image allows it
`rayd` SHALL run every process as the user named by `StartRequest.user.username`, falling back to the `user` field of the `/run` payload and then to `"user"`; the identity (uid, gid, supplementary groups from `getgrouplist`, home) SHALL be resolved from the image's user database before `fork`. The identity gate SHALL be a positive check, not a blacklist of root: after the lookup, `UserPolicy::authorize_identity` SHALL accept an identity only when `uid >= 1000`, `gid >= 1000` and no supplementary group is `0`. `"root"` and any alias of uid 0 SHALL be refused with `PERMISSION_DENIED` and the existing root message unless `rayd`'s own environment contains `RAYITO_ALLOW_ROOT=1`, which is an image-level opt-in read once at boot and never influenced by a request, and which bypasses the whole gate for process spawning, PTYs, the filesystem service and code execution. Every other privileged account — a system uid below 1000, a gid below 1000, or membership of group 0 — SHALL be refused with `PERMISSION_DENIED` and a message that says only unprivileged accounts may run code, never quoting the requested username. The single gate SHALL live in `crates/rayd-core/src/process/identity.rs` so process spawning, PTYs, the filesystem service, code execution and persistence inherit it from one place. Persistence SHALL NOT inherit the image opt-in: `resolve_home_identity` SHALL resolve its identity with `UserPolicy::without_root()` — a four-line helper returning the same policy with `allow_root` cleared — so root and every privileged account are refused there even when the image sets `RAYITO_ALLOW_ROOT=1`, which is what keeps T15's promise that persistence never archives `/root`. `crates/rayd-core/src/persistence/mod.rs` SHALL NOT carry a duplicated `uid == 0` check: with the opt-in dropped the shared gate refuses everything that check refused and, unlike it, every other privileged account too. Unknown usernames SHALL fail with `INVALID_ARGUMENT`. The privilege drop SHALL happen in the child, after the resource limits are set, as `setgroups`, `setgid`, `setuid` in that order.

#### Scenario: default user
- **WHEN** the SDK calls `commands.run("whoami")` and `commands.run("id -u")` without `user`
- **THEN** the outputs are `user` and `1000`

#### Scenario: root refused by default
- **WHEN** the image does not set `RAYITO_ALLOW_ROOT=1` and the SDK calls `commands.run("whoami", user="root")`
- **THEN** `Start` fails with `PERMISSION_DENIED` and the SDK raises `AuthenticationException` with `proxy_rejected == False`

#### Scenario: a system account is refused everywhere
- **WHEN** the user lookup resolves the requested name to uid 11 with gid 0 (a system account such as `operator`), to uid 1 with gid 1 (`bin`), to uid 1000 with gid 0, or to uid 1000 whose supplementary groups contain 0
- **THEN** `plan_spawn`, `plan_pty`, the filesystem identity resolution, the code-execution identity and `resolve_home_identity` all refuse it with the privileged-account error, mapped to `PERMISSION_DENIED` (and to the `permission_denied` stream code for persistence), and the message never contains the username

#### Scenario: the image opt-in still works, except for persistence
- **WHEN** `rayd` runs with `RAYITO_ALLOW_ROOT=1`
- **THEN** process spawning, PTYs, the filesystem service and code execution accept uid 0 and the system accounts above exactly as before this change, while `resolve_home_identity` refuses `root`, an alias of uid 0 and a system account such as `operator` with `RootNotAllowed` / `PrivilegedAccount`, because persistence resolves with `UserPolicy::without_root()`

#### Scenario: rayd started unprivileged
- **WHEN** `rayd` starts with `geteuid() != 0` (developer machine or CI)
- **THEN** it logs one warning at boot, runs processes as its own user, and still applies resource limits clamped to its hard limits

### Requirement: Child environment is built from scratch
The child environment SHALL contain, in override order, `PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin`, `HOME`, `USER` and `LOGNAME` of the resolved identity, then the sandbox `envs` from the `/run` payload, then `ProcessConfig.envs`, the last definition winning. Nothing from `rayd`'s own environment SHALL be inherited.

#### Scenario: base variables and no inheritance
- **WHEN** the SDK calls `commands.run("env")`
- **THEN** the output contains `HOME=/home/user`, `USER=user`, `LOGNAME=user` and a `PATH=` line, and contains neither `RAYD_LOG` nor `AWS_LAMBDA_MICROVM_IMAGE_ARN`

#### Scenario: request envs override sandbox envs
- **WHEN** the sandbox was created with `envs={"FOO": "sandbox"}` and the SDK calls `commands.run("echo $FOO", envs={"FOO": "bar"})`
- **THEN** the output is `bar`

### Requirement: Working directory resolution and validation
The working directory SHALL be `ProcessConfig.cwd` if present, else the `workdir` of the `/run` payload, else the user's home. It SHALL be an absolute path naming an existing directory; otherwise `Start` SHALL fail with `INVALID_ARGUMENT` before spawning.

#### Scenario: default and explicit cwd
- **WHEN** the SDK calls `commands.run("pwd")` and `commands.run("pwd", cwd="/tmp")`
- **THEN** the outputs are `/home/user` and `/tmp`

#### Scenario: missing cwd
- **WHEN** the SDK calls `commands.run("true", cwd="/does/not/exist")`
- **THEN** the SDK raises `InvalidArgumentException` and no process was spawned

### Requirement: Resource limits are set in the child before the privilege drop
Every spawned process SHALL start with `RLIMIT_NPROC=512`, `RLIMIT_NOFILE=4096` and `RLIMIT_CORE=0` (soft and hard), set inside `pre_exec` before the privilege drop. When the platform refuses to raise a hard limit (Lambda MicroVMs: `rayd` runs as root without `CAP_SYS_RESOURCE` and inherits `RLIMIT_NOFILE` hard 1024, measured 2026-09-15), the limit SHALL be clamped to the inherited hard limit with soft == hard instead of failing the spawn. When the run payload carries `limits.cpu_seconds` (`1..=28800`), every process started by `ProcessService.Start` and every shell started by `PtyService.Create` SHALL additionally get `RLIMIT_CPU` soft = `cpu_seconds`, hard = `cpu_seconds + 5`, so a CPU-bound process is signalled `SIGXCPU` at the soft limit and `SIGKILL` 5 s later, ending with `status:"signaled"` and the signal number; the sidecar and the kernels SHALL never receive `RLIMIT_CPU`. A payload whose `limits` is out of range SHALL leave the agent token-less (200 on `/run`, never a 4xx). The SDK SHALL expose `Sandbox.create(cpu_time_limit: int | None = None)` (sync and async), validated in the client and serialized into the payload only when set.

#### Scenario: limits visible from the shell
- **WHEN** the SDK calls `commands.run("ulimit -Sn; ulimit -Hn; ulimit -u; ulimit -c")`
- **THEN** the `NOFILE` soft and hard values are equal and are `4096` or the platform's inherited hard limit (`1024` on Lambda MicroVMs), and the remaining lines are `512` and `0`

#### Scenario: CPU-bound process is killed by its CPU budget
- **WHEN** the e2e creates a sandbox with `cpu_time_limit=2` and runs `commands.run("python3 -c 'while True: pass'", timeout=30)`
- **THEN** `CommandExitException` is raised within 8 s with `exit_code` equal to `128 + 24` or `137`, `commands.run("sleep 3")` succeeds, and `run_code("sum(range(10**7))")` succeeds because the kernel is not limited

#### Scenario: sidecar spawn spec is unlimited
- **WHEN** a host test inspects the `SpawnSpec` the sidecar launcher builds
- **THEN** its `ResourceLimits.cpu_seconds` is `None` regardless of the payload

### Requirement: Process groups, signals and kill
Each process SHALL be the leader of its own process group. `SendSignal{pid, signal}` SHALL deliver the signal to the whole group with `killpg`, validate `1 <= signal <= 64` (`INVALID_ARGUMENT` otherwise) and answer `NOT_FOUND` for an unknown or already-ended pid. A process ended by a signal SHALL end its stream with `EndEvent{exited:true, status:"signaled", exit_code:128+signal, signal}`. The SDK `commands.kill(pid)` SHALL send signal 9 and return `True`, or `False` when the pid is not found.

#### Scenario: background sleep killed
- **WHEN** the SDK runs `h = commands.run("sleep 30", background=True)` and calls `commands.kill(h.pid)`
- **THEN** `kill` returns `True`, `h.wait()` raises `CommandExitException` with `exit_code == 137` and `error == "signaled"`, and a second `commands.kill(h.pid)` returns `False`

#### Scenario: invalid signal
- **WHEN** `SendSignal{pid, signal: 0}` is called
- **THEN** the RPC fails with `INVALID_ARGUMENT`

### Requirement: Server-enforced timeout on the monotonic clock
When `StartRequest.timeout_ms > 0`, `rayd` SHALL send `SIGTERM` to the process group when the deadline expires on the running clock (the monotonic clock minus the time spent suspended; M5 replaces the M2 "tokio monotonic" wording because `CLOCK_MONOTONIC` advances during a suspension, measured 2026-09-15), `SIGKILL` 5 s of running time later if the process is still alive, and end the stream with `EndEvent{exited:false, status:"timeout", exit_code:128+signal, signal, error:{code:"deadline_exceeded"}}` regardless of how the process died after the `SIGTERM`. tokio sleeps SHALL be treated as wake-ups and re-validated against the running clock. `timeout_ms == 0` SHALL mean no limit. The SDK SHALL default `timeout` to 60 s, send `timeout_ms = timeout * 1000`, set the gRPC deadline to `timeout + 5 s`, and raise `TimeoutException` on the `timeout` status.

#### Scenario: sleep 10 with timeout 2
- **WHEN** the SDK calls `commands.run("sleep 10", timeout=2)`
- **THEN** `TimeoutException` is raised within 8 s and the `EndEvent` carried `status == "timeout"`, `exited == false`, `signal == 15` and `error.code == "deadline_exceeded"`

#### Scenario: paused time does not count
- **WHEN** a process started with `timeout_ms: 1500` is 0.5 s old at `/suspend`, the session stays suspended for 2 s and `/resume` arrives
- **THEN** the process is still alive at `/resume` and ends with `status:"timeout"` about 1 s later

### Requirement: Output is streamed in 32 KiB chunks with a per-process sequence
`rayd` SHALL read stdout and stderr with 32 KiB reads, emit one `DataEvent` per non-empty read carrying `seq` from a single per-process counter starting at 1, preserve read order across both streams, and emit the `EndEvent` only after both pipes reached EOF and the process was reaped. The SDK SHALL decode each stream with an incremental UTF-8 decoder (`errors="replace"`) flushed at the `EndEvent`.

#### Scenario: three megabytes without loss
- **WHEN** the SDK calls `commands.run("head -c 3000000 /dev/zero | tr '\\0' a")`
- **THEN** `len(stdout) == 3_000_000` and the `EndEvent` was the last message

#### Scenario: multibyte character split across chunks
- **WHEN** a UTF-8 sequence is cut at a 32 KiB boundary
- **THEN** the SDK's decoded `stdout` contains the character intact

#### Scenario: callbacks receive decoded text
- **WHEN** the SDK calls `commands.run("echo a; echo b >&2", on_stdout=out.append, on_stderr=err.append)`
- **THEN** `"".join(out) == "a\n"` and `"".join(err) == "b\n"`

### Requirement: Bounded per-subscriber channels with backpressure and output_truncated
Each subscriber of a process stream SHALL have a bounded channel of 64 events. While the channel is full the output pump SHALL block (backpressure to the process). If a subscriber's channel stays full for 30 s, `rayd` SHALL detach that subscriber by ending its stream with `EndEvent{exited:false, status:"output_truncated", error:{code:"output_truncated"}}` while the process and other subscribers continue. A subscriber whose receiver is dropped SHALL be detached immediately. At most 8 subscribers per pid SHALL be accepted; the 9th `Connect` SHALL fail with `RESOURCE_EXHAUSTED`. The SDK SHALL map the `output_truncated` end to `SandboxException`.

#### Scenario: stalled subscriber is detached
- **WHEN** a client opens `Start` for a process writing 5 MiB and never reads the stream
- **THEN** after the stall timeout its stream ends with `status:"output_truncated"`, and the process finishes for a second consuming subscriber

#### Scenario: subscriber cap
- **WHEN** eight subscribers are attached to a pid and a ninth `Connect` arrives
- **THEN** the ninth fails with `RESOURCE_EXHAUSTED`

### Requirement: Ring buffer replay with Connect(from_seq)
`rayd` SHALL keep the last 1 MiB of `DataEvent` payloads per pid, bounded additionally by a sandbox-wide output budget of 128 MiB shared by every process ring, PTY ring and execution ring. `Connect{pid, from_seq:0}` SHALL deliver only new output; `from_seq:N` SHALL replay retained events with `seq >= N` followed by live events with no gap or duplicate; `N` below the oldest retained `seq` or above the next `seq` SHALL fail with `OUT_OF_RANGE` before any message. When a push would exceed the budget, the pushing ring SHALL evict its own oldest chunks until the chunk fits; if the ring is empty and the chunk still does not fit, the chunk SHALL be delivered live to subscribers but not retained (`oldest_seq` advances past it). `Connect` SHALL always start with `StartEvent{pid}`. The SDK SHALL expose `commands.connect(pid, from_seq=0)` and `CommandHandle.last_seq`, and map `OUT_OF_RANGE` to `NotFoundException`.

#### Scenario: full replay
- **WHEN** the SDK runs `h = commands.run("for i in 1 2 3; do echo $i; sleep 1; done", background=True)` and then `full = commands.connect(h.pid, from_seq=1)`
- **THEN** both `full.wait().stdout` and `h.wait().stdout` equal `"1\n2\n3\n"`

#### Scenario: discarded sequence
- **WHEN** `Connect{pid, from_seq}` names a `seq` already evicted from the ring
- **THEN** the RPC fails with `OUT_OF_RANGE` and the SDK raises `NotFoundException`

#### Scenario: budget exhausted across rings
- **WHEN** an integration test scales the budget to 256 KiB, starts three processes that each print 200 KiB and then calls `Connect(pid, from_seq: 1)` on the first
- **THEN** every live subscriber received its full 200 KiB, the replay fails with `OUT_OF_RANGE`, and the budget counter never exceeded 256 KiB

### Requirement: Terminal events are retained for 30 seconds
After a process or PTY ends, its entry SHALL remain available to `Connect` for 30 s of running time (suspended time excluded), replaying `StartEvent`/`started`, ring data per `from_seq`, and the `EndEvent`/`exited`, after which `Connect` SHALL answer `NOT_FOUND`. At most 256 ended entries SHALL be retained at once; when an entry ends while 256 are already retained, `rayd` SHALL evict the oldest ended entries first, and `Connect` on an evicted pid SHALL answer `NOT_FOUND`. While the sandbox-wide output budget is above its 96 MiB high-water mark, the reaper SHALL additionally drop ended entries oldest-first until it is below, regardless of age, releasing their bytes, and SHALL log `output_budget_bytes` and `entries_dropped` when it crosses the mark. `SendInput`, `CloseStdin`, `SendSignal`, `Resize` and `Kill` on an ended pid SHALL answer `NOT_FOUND` immediately. `List` SHALL return only live entries.

#### Scenario: connect right after exit
- **WHEN** a process exited less than 30 s ago and the SDK calls `commands.connect(pid)`
- **THEN** `wait()` returns the process's `exit_code` without raising `NotFoundException`

#### Scenario: connect after retention
- **WHEN** a process exited more than 30 s of running time ago and the reaper ran
- **THEN** `Connect` fails with `NOT_FOUND`

#### Scenario: retention spans a pause
- **WHEN** a process exits 5 s before `/suspend`, the session stays suspended for 60 s and `Connect` arrives 5 s after `/resume`
- **THEN** `Connect` replays `StartEvent`, the ring and the `EndEvent`

#### Scenario: early reap under memory pressure
- **WHEN** a host test fills a 64 KiB budget with two ended entries and one live ring, then runs the reaper
- **THEN** the oldest ended entry is dropped although less than 30 s old, the budget is below the high-water mark and the live ring is untouched

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

### Requirement: Stdin handling
`StartRequest.stdin=false` SHALL connect the child's stdin to `/dev/null`. `stdin=true` SHALL keep a pipe open; `SendInput{pid, data}` SHALL write and flush `data` in order and return afterwards; `CloseStdin{pid}` SHALL close the pipe (EOF) and be idempotent. `SendInput` or `CloseStdin` on a process started with `stdin=false` SHALL fail with `FAILED_PRECONDITION`, which the SDK maps to `InvalidArgumentException`. `SendInput` after the process closed its end SHALL fail with `FAILED_PRECONDITION`.

#### Scenario: cat with stdin
- **WHEN** the SDK runs `h = commands.run("cat", background=True, stdin=True)`, `h.send_stdin("hola\n")`, `h.close_stdin()`
- **THEN** `h.wait().stdout == "hola\n"`

#### Scenario: stdin not opened
- **WHEN** the SDK runs `h = commands.run("sleep 30", background=True)` (a process that stays alive on `/dev/null` stdin) and calls `h.send_stdin("x")`
- **THEN** `InvalidArgumentException` is raised and `h.kill()` returns `True`

### Requirement: KeepAlive on idle streams
`Start` and `Connect` streams SHALL emit `ProcessEvent{keepalive}` after 30 s without any other message, repeatedly while idle. SDK iterators and `wait()` SHALL ignore keepalives. Holding a stream open therefore keeps the MicroVM out of its idle policy.

#### Scenario: silent process through the proxy
- **WHEN** the SDK calls `commands.run("sleep 35", timeout=None)`
- **THEN** the call returns `exit_code == 0` without the proxy closing the stream

#### Scenario: keepalive cadence
- **WHEN** the integration test configures a 200 ms keepalive interval and runs a silent `sleep 1`
- **THEN** at least three `keepalive` messages arrive before the `EndEvent`

### Requirement: List returns live processes with their kind
`List` SHALL return one `ProcessInfo{pid, config, tag, kind}` per live entry: `PROCESS_KIND_PROCESS` for processes and `PROCESS_KIND_PTY` for PTYs (with `config.cmd` = the shell, `config.args = ["-i","-l"]`, `config.envs` = the request envs, `config.cwd`, no tag). The SDK SHALL map it to `ProcessInfo(pid, cmd, args, envs, cwd, tag, kind="process"|"pty")`.

#### Scenario: background process listed then gone
- **WHEN** the SDK runs `h = commands.run("sleep 30", background=True, tag="m2")`
- **THEN** `commands.list()` contains an item with `pid == h.pid`, `kind == "process"`, `tag == "m2"`, and after the process ends it no longer does

#### Scenario: PTY listed with its kind
- **WHEN** the SDK creates a PTY and calls `commands.list()`
- **THEN** the list contains an item with the PTY's `pid`, `kind == "pty"` and `args == ("-i", "-l")`

### Requirement: HealthService.Metrics reports procfs metrics
`Metrics` SHALL require `x-access-token` and return `cpu_used_pct` (0–100, from two `/proc/stat` samples 100 ms apart), `mem_used_bytes = MemTotal - MemAvailable`, `mem_total_bytes`, `disk_used_bytes`/`disk_total_bytes` of `/` from `statvfs`, `cpu_count >= 1`, and `timestamp_unix_ms` from the wall clock. The SDK SHALL expose `sbx.get_metrics() -> SandboxMetrics`.

#### Scenario: metrics from the SDK
- **WHEN** the SDK calls `sbx.get_metrics()`
- **THEN** `cpu_count >= 1`, `mem_total_bytes > 0`, `mem_used_bytes <= mem_total_bytes`, `disk_total_bytes > 0`, `0 <= cpu_used_pct <= 100`, and `timestamp` is within 60 s of now

#### Scenario: metrics without a token
- **WHEN** `Metrics` is called without `x-access-token`
- **THEN** it fails with `UNAUTHENTICATED`

### Requirement: SDK command surface, sync and async
The SDK SHALL expose `sbx.commands` with `run(cmd, *, background=False, envs=None, user=None, cwd=None, on_stdout=None, on_stderr=None, stdin=False, timeout=60.0, request_timeout=None, tag=None)` returning `CommandResult` (foreground) or `CommandHandle` (background), `list()`, `kill(pid)`, `send_stdin(pid, data)`, `close_stdin(pid)`, `connect(pid, *, from_seq=0, on_stdout=None, on_stderr=None, timeout=None, request_timeout=None)`. `CommandHandle` SHALL expose `pid`, `last_seq`, `stdout`, `stderr`, `exit_code`, `error`, `wait()`, `kill()`, `disconnect()`, `send_stdin()`, `close_stdin()` and iteration yielding `(stdout, stderr, pty)` tuples with `pty` always `None`. `AsyncSandbox.commands` SHALL offer the same names as coroutines/async iteration. Foreground `run` SHALL use the unary channel; background `run` and `connect` SHALL use a second, lazily opened channel; a sandbox SHALL never open more than two channels.

#### Scenario: foreground and background parity
- **WHEN** the SDK calls `commands.run("echo hola")` and `commands.run("echo hola", background=True).wait()`
- **THEN** both yield `CommandResult(stdout="hola\n", exit_code=0)`

#### Scenario: thirty sequential commands stay under the connection cap
- **WHEN** the SDK runs `commands.run(f"echo {i}")` for `i` in 0..29
- **THEN** every result is correct, no `RateLimitException` is raised, and only one channel was used

#### Scenario: async parity
- **WHEN** `AsyncSandbox.connect(...)` is used and `await sbx.commands.run("echo async")` runs
- **THEN** the result's `stdout.strip() == "async"`

#### Scenario: disconnect keeps the process alive
- **WHEN** the SDK calls `h = commands.run("sleep 30", background=True)`, `h.disconnect()`, then `commands.list()`
- **THEN** the pid is still listed

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

### Requirement: Image provides the acceptance tooling and keeps rayd as root
The image SHALL keep user `user` (uid 1000, home `/home/user`, shell `/bin/bash`), SHALL make `python3` resolve to Python 3.12 on the child `PATH`, SHALL fail its build if `/bin/bash`, `whoami`, `id`, `sleep`, `head`, `tr`, `env` or `python3` are missing, SHALL run `rayd` as root, and SHALL NOT set `RAYITO_ALLOW_ROOT`.

#### Scenario: http.server reachable through get_host
- **WHEN** the SDK runs `commands.run("python3 -m http.server 3000 --bind 0.0.0.0", background=True, timeout=None)` and requests `sbx.get_host(3000).url` with `host.headers`
- **THEN** the response is HTTP 200 within 10 s, and the same request without headers is HTTP 403

### Requirement: Cross-process connect honours the access token
`Sandbox.connect(sandbox_id, access_token=...)` from another process SHALL be able to run commands; a wrong token SHALL make every `ProcessService` RPC fail with `UNAUTHENTICATED`, surfaced as `AuthenticationException`. `RAYITO_ACCESS_TOKEN` SHALL keep working as the fallback for `connect()` and for `create()`, but the SDK SHALL log exactly one `WARNING` per process, the first time `create()` falls back to that variable, stating that every sandbox created by this process then shares one secret and that passing `access_token=` per call gives each sandbox its own. The warning SHALL name only the variable — never the token, a prefix of it, or any `envs`/`metadata` value — and the `connect()` path SHALL NOT warn, because reading the variable there is its documented purpose.

#### Scenario: right and wrong token
- **WHEN** another process connects with the sandbox's token and runs `echo hola`, and a third connects with a different valid base64url token and runs `true`
- **THEN** the first succeeds and the second raises `AuthenticationException`

#### Scenario: rejection reaches the client as a gRPC status through the proxy
- **WHEN** a request without a valid `x-access-token` arrives through the AWS proxy
- **THEN** `rayd` reads the request body to its end (bounded to 1 MiB / 2 s) before answering, so the client receives `UNAUTHENTICATED` rather than the `CANCELLED` the proxy produces when the app resets a half-open stream

#### Scenario: the shared-token warning fires once
- **WHEN** `RAYITO_ACCESS_TOKEN` is set and the launch plan resolves the access token twice in the same process
- **THEN** both resolutions return the same token and exactly one `WARNING` naming `RAYITO_ACCESS_TOKEN` was logged

#### Scenario: explicit, generated and connect paths stay silent
- **WHEN** the token is passed explicitly, or no variable is set and a fresh 32-byte token is generated, or `connect()` reads the variable
- **THEN** nothing is logged

### Requirement: Process logging hygiene
`rayd` SHALL log only `pid`, `status`, `exit_code`, `signal`, `seq`, subscriber and live counts, durations, errno names and identity-switch mode for process events, and SHALL never log `cmd`, `args`, `envs`, `cwd`, `tag`, stdout/stderr bytes or stdin bytes.

#### Scenario: spawn failure log
- **WHEN** spawning fails with `ENOENT`
- **THEN** the log line contains the errno name and no part of the command, while the gRPC error message returned to the client names the errno


## Context

State after M1 (accepted 2026-09-15 against real AWS): `rayd` is a hexagonal
workspace (`crates/rayito-proto` generated, `crates/rayd-core` domain,
`crates/rayd` adapters) serving `HealthService.Health` and the six hooks;
every other RPC is registered as `Pending*Service` answering `UNIMPLEMENTED`
behind the `AccessTokenLayer`. The Python SDK has `Sandbox`/`AsyncSandbox`
with `create/connect/kill/list/get_info/is_running/get_host/pause/resume`,
one gRPC channel per sandbox, a `TokenRefresher` (45 min, retry 60 s), and
the proxy-403 re-mint for unary calls. Image `rayito-base` 1.0 runs `rayd`
as root (capped capabilities, `RLIMIT_NOFILE` 1024, no cgroup, `setpriv`
present, M0 Q20) with user `user` (uid 1000) created by the Dockerfile.

Measured facts that shape this design (`AWS_API_NOTES.md` §7, §15, §16):

- `CLOCK_MONOTONIC` **advances** during suspend (Q19): tokio timers fire on
  resume. The proto comment saying otherwise has been corrected.
- A stream with **no bytes** does not count as endpoint traffic for the idle
  policy (Q15); `KeepAlive` messages are bytes.
- Token expiry is evaluated only when a connection is opened (Q28); a live
  stream survives its token.
- 8 concurrent connections per MicroVM at 1 vCPU (not enforced as 429 in M0,
  Q16); the SDK keeps ≤ 2 HTTP/2 channels per sandbox.
- The proxy's 403 has no `grpc-status` and surfaces as `PERMISSION_DENIED`
  with `Received http2 header with status: 403` (M1, Q17).
- `runHookPayload` already carries `envs`, `user`, `workdir` defaults that
  `rayd-core::run_payload` parses into `RunDefaults`; nothing consumed them
  yet.

Constraints: `openspec/project.md` hard rules (no invented AWS parameters,
`.proto` is the source of truth, hexagonal boundaries, ARM64 musl, security
defaults ship now, `clippy` pedantic, no `unwrap` outside tests, identifiers
in English, no inline comments in bodies, never log commands/envs/output/
tokens).

## Goals / Non-Goals

**Goals:**

- `ProcessService` complete and correct on real AWS, with its final security
  posture (identity, environment, rlimits, process groups, bounded output,
  caps).
- `HealthService.Metrics` real, procfs-based.
- Python SDK `commands.*`, `CommandHandle`, `get_metrics()`, second channel,
  stream error contract; sync and async with identical surfaces.
- Every decision below is closed so implementers never guess; every rule has
  a host-side unit test, a `cfg(unix)` integration test, or an e2e assertion.

**Non-Goals:**

- PTYs (`PtyService`, M5), filesystem (M3), code execution (M4).
- Suspend/resume checklists for streams (`StreamError{code:"suspending"}`,
  re-arming timeouts on `/resume`, SDK re-subscription): M5. In M2 the hooks
  keep their M1 behaviour; `Start`/`Connect` while the session phase is
  `Suspending` answer `UNAVAILABLE` and nothing else changes.
- cgroup slices, IMDS blocking, egress allowlists (M6).
- `commands.send_signal` in the SDK (only `kill` = SIGKILL, E2B parity).
- `HostAccess.client()` (`httpx`), TypeScript client (M6).
- Any change to `create-microvm-image`/`run-microvm` parameters.

## Decisions

### D1. Proto usage: no field changes, two comment corrections (done)

The existing `process.proto` and `health.proto` already carry everything M2
needs. Wire mapping used by both sides:

| SDK call | RPC | Request | Notes |
|---|---|---|---|
| `commands.run(cmd, ...)` | `Start` (server-stream) | `StartRequest{process: ProcessConfig{cmd:"/bin/bash", args:["-l","-c", cmd], envs, cwd?}, user?: User{username}, timeout_ms, stdin, tag?}` | first message is always `StartEvent{pid}` |
| `commands.connect(pid, from_seq)` | `Connect` | `ConnectRequest{pid, from_seq}` | replays `StartEvent`, then data with `seq >= from_seq`, then `EndEvent` if ended |
| `commands.send_stdin(pid, data)` | `SendInput` | `SendInputRequest{pid, data: bytes}` | unary |
| `commands.close_stdin(pid)` | `CloseStdin` | `CloseStdinRequest{pid}` | unary, idempotent |
| `commands.kill(pid)` | `SendSignal` | `SendSignalRequest{pid, signal: 9}` | unary, `killpg` |
| `commands.list()` | `List` | `ListRequest{}` | live processes only, `ProcessInfo{pid, config, tag?, kind}` |
| `sbx.get_metrics()` | `Metrics` | `MetricsRequest{}` | requires `x-access-token` |

`EndEvent` field semantics (closed set; the proto comment now lists all five
statuses):

| `status` | `exited` | `exit_code` | `signal` | `error` | Meaning |
|---|---|---|---|---|---|
| `exited` | true | wait status | absent | absent | process called `exit` |
| `signaled` | true | `128 + signal` | the signal | absent | killed by a signal not sent by the timeout (e.g. `SendSignal`, or from inside the sandbox) |
| `timeout` | false | `128 + signal` (15 or 9) | the signal | `{code:"deadline_exceeded"}` | `timeout_ms` expired; `rayd` sent SIGTERM (and SIGKILL after 5 s) |
| `suspending` | false | 0 | absent | `{code:"suspending"}` | reserved for M5; not emitted in M2 |
| `output_truncated` | false | 0 | absent | `{code:"output_truncated", message:"subscriber stalled for 30 s at seq N"}` | this subscriber only; the process is alive; `Connect(pid, from_seq=N+1)` resumes |

`exited` therefore means "terminated without `rayd` intervening". Comment
edits applied: `StartRequest.timeout_ms` (monotonic clock advances during
suspend, M5 re-arms) and `EndEvent.status` (`output_truncated`). `buf lint`
and `buf build` pass; `python scripts/gen_python.py` regenerated
`clients/python/src/rayito/v1`; Rust regenerates on `cargo build`. Comments
are not part of `buf breaking FILE`.

Alternatives considered: adding an `OutputTruncated` message to the
`ProcessEvent` oneof (breaking for nothing: `EndEvent.error` already carries
a `StreamError`); a separate `Detached` status (rejected: one status per
cause keeps the SDK mapping table flat).

### D2. `rayd-core` domain: `process` and `metrics` modules

New modules in `crates/rayd-core/src/` (no `tokio`, `tonic`, `nix`, `libc`):

```text
process/
  mod.rs        pub use of the types below; `Pid(u32)` newtype
  identity.rs   ProcessIdentity{uid, gid, groups: Vec<u32>, username, home}
                UserPolicy{allow_root: bool}
                resolve_username(request: Option<&str>, defaults: Option<&str>) -> String   // "user" fallback
                UserPolicy::authorize(&self, username) -> Result<(), ProcessError::RootNotAllowed>
  env.rs        DEFAULT_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
                build_child_env(identity, sandbox_envs, request_envs) -> Vec<(String, String)>
  cwd.rs        resolve_cwd(request: Option<&str>, workdir_default: Option<&str>, home) -> PathBuf
                validate_cwd(path) -> Result<(), ProcessError::InvalidCwd>   // absolute, no NUL
  limits.rs     ResourceLimits{nproc: 512, nofile: 4096, core: 0} (Default), StdinMode{Null, Pipe}
  spec.rs       SpawnSpec{program, args, env, cwd, identity, stdin, limits}
                SpawnInput{config: ProcessConfigInfo, user: Option<String>, timeout, stdin, tag}
                plan_spawn(input, defaults: &RunDefaults, policy, lookup: &dyn UserLookup) -> Result<SpawnSpec, ProcessError>
  ring.rs       OutputRing{capacity_bytes: 1 MiB}: push(stream, bytes) -> seq (starts at 1),
                replay_from(from_seq) -> Result<Vec<OutputEvent>, ReplayError::OutOfRange{oldest, next}>
  timeout.rs    TimeoutPlan{term_at, kill_at = term_at + 5 s}, TimeoutStep{Term, Kill},
                EndReason{Natural, Timeout, Signal}, end_from_wait(WaitOutcome, EndReason) -> ProcessEnd
  events.rs     OutputStream{Stdout, Stderr}, OutputEvent{seq, stream, bytes}, EndStatus, ProcessEnd,
                ProcessEvent{Started{pid}, Output(OutputEvent), Ended(ProcessEnd)}
  registry.rs   ProcessRegistry{max_live: 256, max_subscribers_per_pid: 8, retention: 30 s}
                register(pid, kind, config, tag, stdin_mode) -> Result<(), ProcessError::TooManyProcesses>
                push_output / mark_ended(pid, end, now) / reap_expired(now) -> Vec<Pid>
                replay(pid, from_seq) -> Result<ReplayPlan, ProcessError>
                live() -> Vec<ProcessSummary>   // List
                subscribe(pid) -> Result<SubscriberTicket, ProcessError::TooManySubscribers>
  ports.rs      trait UserLookup { fn lookup(&self, username) -> Result<ProcessIdentity, LookupError> }
                trait ProcessSpawner: Send + Sync { type Child: SpawnedChild; fn spawn(&self, spec: &SpawnSpec) -> Result<Self::Child, SpawnError>; }
                trait SpawnedChild: Send { fn pid(&self) -> Pid; fn signal_group(&self, signal: i32) -> Result<(), SignalError>; }
  error.rs      ProcessError (thiserror): TooManyProcesses{max}, TooManySubscribers{pid, max}, NotFound{pid},
                StdinNotOpen{pid}, RootNotAllowed, UnknownUser, InvalidCwd(reason), EmptyCommand,
                InvalidSignal(i32), OutOfRange{oldest, next}, Suspending, Spawn(SpawnError)
metrics/
  mod.rs        CpuTimes{busy, idle} (parsed jiffies), cpu_used_pct(prev, next) -> f64 (0..=100, 0 when no delta)
                parse_proc_stat(&str) -> Result<CpuTimes, MetricsError>, parse_meminfo(&str) -> Result<MemoryInfo{total, available}, MetricsError>
                MetricsSnapshot{cpu_used_pct, mem_used, mem_total, disk_used, disk_total, cpu_count, wall}
                trait MetricsProbe: Send + Sync { fn cpu_times(&self) -> Result<CpuTimes, MetricsError>; fn memory(&self) -> ...; fn disk_root(&self) -> Result<DiskUsage, MetricsError>; fn cpu_count(&self) -> u32; }
```

`session.rs` gains `spawn_defaults(&self) -> RunDefaults` (empty defaults
when `/run` delivered none) and `accepts_new_streams(&self) -> Result<(),
ProcessError::Suspending>` (`Running | Resumed` only).

`plan_spawn` is the pure orchestration tested on Windows with a fake
`UserLookup`: policy → identity → env → cwd → limits → `SpawnSpec`. Rules:

- `config.cmd` empty → `EmptyCommand`.
- username: request `User.username` if present and non-empty, else
  `RunDefaults.user`, else `"user"`. `"root"` → `RootNotAllowed` unless
  `policy.allow_root`. Any other name → `UserLookup` (`UnknownUser` on
  miss).
- stdin: `StdinMode::Pipe` iff `StartRequest.stdin`.
- `tag`: stored verbatim, returned by `List`, never logged.

Alternative considered: putting the tokio pumps in `rayd-core` behind async
traits. Rejected: it drags `tokio` into the domain and the pumps are pure
plumbing; the rules worth testing on the host are all in the registry, ring,
timeout plan and `plan_spawn`.

### D3. Privilege drop and rlimits happen inside one `pre_exec`, in a fixed order

`std::process::Command` (and therefore `tokio::process::Command`) runs
`pre_exec` closures **after** it applies `uid()/gid()/groups()`. The VM's
inherited `RLIMIT_NOFILE` hard limit is 1024 (M0 Q20), so raising it to 4096
must run while still root, and `initgroups(3)` is not async-signal-safe.
Decision: `TokioProcessSpawner` does **not** call `Command::uid/gid/groups`.
It resolves the identity before `fork` (`nix::unistd::User::from_name` and
`nix::unistd::getgrouplist` in the parent) and installs a single `pre_exec`
that performs, in order, only syscalls:

1. `setrlimit(RLIMIT_NPROC, 512/512)`, `setrlimit(RLIMIT_NOFILE,
   4096/4096)`, `setrlimit(RLIMIT_CORE, 0/0)` (`nix::sys::resource`).
   Each value is first set as requested and, if the kernel refuses
   (`EPERM`), clamped to the parent's hard limit read with `getrlimit`
   before `fork`, in both `IdentitySwitch` modes. Measured 2026-09-15 on
   Lambda MicroVMs: `rayd` runs as root **without `CAP_SYS_RESOURCE`** and
   inherits `RLIMIT_NOFILE` soft=hard=1024, so children get 1024/1024
   (`NPROC` 512 and `CORE` 0 apply as designed). Raising it needs
   `additionalOsCapabilities: ["ALL"]` on the image (M6 decision).
2. `setgroups(groups)` (`nix::unistd::setgroups`).
3. `setgid(gid)`, then `setuid(uid)`.
4. return `Ok(())`. Any error aborts the exec and `spawn` fails with the
   `io::Error`, mapped to `INTERNAL` (errno logged, never the command).

`Command::process_group(0)` is used (std applies `setpgid(0, 0)` before the
closures; the child's pgid equals its pid, which is what `SendSignal`,
timeout and `kill` target with `killpg`). `Command::env_clear()` +
`envs(spec.env)`, `current_dir(spec.cwd)` (std `chdir`s as root before the
drop; the directory was validated in `plan_spawn` and re-checked with
`tokio::fs::metadata` right before spawn), `stdin` = `Stdio::null()` or
`Stdio::piped()`, `stdout`/`stderr` = `Stdio::piped()`, `kill_on_drop(false)`
(lifetimes are explicit).

`IdentitySwitch`: `Enforce` when `geteuid() == 0` (the image), `KeepCurrent`
when `rayd` starts unprivileged (WSL2 dev, CI) with a single `warn!` at boot
and steps 2–3 skipped. `main.rs` picks it; the gRPC layer never knows.
`RAYITO_ALLOW_ROOT=1` in `rayd`'s own environment (image
`environmentVariables` or Dockerfile `ENV`) sets `UserPolicy.allow_root`;
anything else means root is refused with `PERMISSION_DENIED`.

`nix` features to add in the workspace: `resource`. `tokio` features to add:
`process`, `io-util`. `rayd` gains `nix`, `futures`, `tokio-stream` (feature
`sync`) as dependencies.

### D4. Child environment built from scratch

`build_child_env` produces, in this order (a later key overwrites an earlier
one; the result is sorted for determinism):

1. `PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin`
2. `HOME=<identity.home>`, `USER=<username>`, `LOGNAME=<username>`
3. `RunDefaults.envs` (the `envs=` passed to `Sandbox.create()`)
4. `ProcessConfig.envs` of the request

Nothing from `rayd`'s own environment is inherited, including `RAYD_LOG`,
`RAYITO_ALLOW_ROOT` and the `AWS_LAMBDA_MICROVM_*`/`AWS_REGION` variables the
platform injects. The SDK sends `ProcessConfig{cmd:"/bin/bash", args:["-l",
"-c", cmd]}`; the login shell may add more from `/etc/profile`, which is the
image's business. No `LANG`/`TERM` in M2 (M5 sets them for PTYs).

### D5. Working directory

`resolve_cwd`: request `ProcessConfig.cwd` if present, else
`RunDefaults.workdir`, else `identity.home`. `validate_cwd` requires an
absolute path without NUL; the adapter then requires an existing directory
(`tokio::fs::metadata(...).is_dir()`), otherwise `Start` fails with
`INVALID_ARGUMENT` ("cwd is not a directory") before anything is spawned.
The check runs as root; permission errors for uid 1000 surface from the
shell itself (bash reports and exits 1). Not chasing that edge case in M2.

### D6. Process groups, signals and the server timeout

- Every child is its own process group (`setpgid(0,0)`); `SpawnedChild::
  signal_group(signal)` = `killpg(pid, signal)`. `SendSignal` validates
  `1 <= signal <= 64`, else `INVALID_ARGUMENT`; `ESRCH` → `NOT_FOUND`.
- `timeout_ms > 0` builds `TimeoutPlan{term_at = now + timeout_ms, kill_at =
  term_at + 5 s}` on the tokio clock (`tokio::time::sleep_until`). At
  `term_at`: set `EndReason::Timeout`, `killpg(SIGTERM)`. At `kill_at`, if
  the child has not been reaped: `killpg(SIGKILL)`. The final `EndEvent` uses
  `status:"timeout"`, `exited:false`, `exit_code:128+signal`, `signal`,
  `error:{code:"deadline_exceeded"}` regardless of how the process actually
  died after the SIGTERM (even if it exited 0 while handling it).
- `timeout_ms == 0` → no timer.
- Known and accepted in M2: the tokio clock advances during suspend (M0
  Q19), so a deadline may expire during a pause and fire on resume. M5
  re-arms plans in `/resume`.
- `SendSignal` from the client sets `EndReason::Signal`; a signal from inside
  the sandbox produces the same `signaled` status (the domain cannot tell
  them apart and does not try).

Reaping: the adapter awaits `child.wait()` after both output pumps reach
EOF, so `EndEvent` is always the last message after every `DataEvent`. The
raw wait status is turned into `WaitOutcome{Exited(code) | Signaled(sig)}`
with `std::os::unix::process::ExitStatusExt` and mapped by `end_from_wait`.

### D7. Output pipeline: chunks, `seq`, ring, subscribers, truncation

- Two pump tasks per process read `stdout` and `stderr` with a 32 KiB
  buffer (`tokio::io::AsyncReadExt::read`); each non-empty read becomes one
  `OutputEvent` with the next `seq` (one monotonic counter per pid shared by
  both streams, starting at 1), pushed into the pid's `OutputRing`
  (1 MiB by payload bytes, evicting the oldest whole events), then fanned out
  to subscribers.
- A subscriber is a `tokio::sync::mpsc::Sender<ProcessEvent>` with capacity
  **64**. Fan-out uses `send_timeout(event, 30 s)`: while the client keeps
  up, sends complete immediately; when tonic's HTTP/2 flow control stops
  polling (the client stopped reading), the channel fills and the pump
  blocks, which in turn stops reading the pipe and applies real backpressure
  to the process. If the channel stays full for 30 s
  (`SUBSCRIBER_STALL_TIMEOUT`), the subscriber is detached: its sender is
  dropped after storing `Detached{last_seq}` in the subscriber slot, the
  stream wrapper emits `EndEvent{status:"output_truncated", error:{code:
  "output_truncated", message:"subscriber stalled for 30 s at seq N"}}` and
  ends; the process continues for the other subscribers. A dropped receiver
  (client cancelled, connection reset) detaches immediately without a
  message: the sink implements the `SubscriberSlot` port (`is_open` =
  `!Sender::is_closed()`) and `attach` prunes closed slots before applying
  the cap, so `disconnect()` cycles on a silent process (`sleep 30`) never
  pile dead senders up to `RESOURCE_EXHAUSTED`. Memory per pid is bounded
  by 1 MiB (ring) + 64 × 32 KiB per subscriber × ≤ 8 subscribers.
- Ordering guarantee per subscriber: `StartEvent`, then `DataEvent`s in
  `seq` order (stdout and stderr interleaved as read), then exactly one
  terminal `EndEvent` (or the truncation `EndEvent`), never anything after.
- `Connect(pid, from_seq)`: `0` = subscribe to new output only; `N > 0` =
  replay ring events with `seq >= N` **then** live events, without gaps or
  duplicates (the subscriber is registered under the registry lock before
  the replay snapshot is taken); `N < oldest_retained` or `N > next_seq` →
  `OUT_OF_RANGE` as gRPC status before any message. Replayed events are not
  subject to the stall timeout (they are queued before the stream starts and
  the queue is unbounded for the replay slice only).
- Max 8 subscribers per pid (`Start` counts as one); the 9th `Connect` →
  `RESOURCE_EXHAUSTED`.

Alternative considered: non-blocking `try_send` with immediate truncation.
Rejected: a foreground `cat bigfile` over the 4 MB/s proxy link would be
truncated by design; the 30 s stall threshold gives real backpressure to
well-behaved clients and still bounds memory and stall time for stuck ones.

### D8. Retention and `List`

`mark_ended` stores the `ProcessEnd` and `ended_at`; the entry stays for
`RETENTION = 30 s` so `Connect` can replay `StartEvent` + ring + `EndEvent`
(`from_seq` rules unchanged). A reaper task runs every 5 s and removes
expired entries (`reap_expired(now)` uses the `Clock` port; tests drive it
with the fake clock). Retained entries are also capped: `RegistryLimits.
max_retained = 256`, and `mark_ended` evicts the oldest ended entries (by
`ended_at`) once more than 256 coexist, so a sandbox looping short
commands at any spawn rate holds at most 256 rings (≤ 256 MiB) regardless
of the 30 s window; an evicted pid answers `NOT_FOUND` like an expired
one. `List` returns live entries only (`ProcessKind::
Process` in M2; `Pty` slots exist for M5). `SendInput`, `CloseStdin` and
`SendSignal` on an ended-but-retained pid answer `NOT_FOUND` (the process is
gone); `Connect` still works until expiry. The retention timer also uses the
monotonic clock, so a pause longer than 30 s expires retained entries on
resume; accepted in M2.

### D9. Live cap and phase gate

`ProcessRegistry.max_live = 256` counts live processes (M5 adds PTYs to the
same count). `Start` beyond the cap → `RESOURCE_EXHAUSTED` ("max 256 live
processes") before spawning. `Start`/`Connect` while `SandboxSession.phase()`
is `Suspending` → `UNAVAILABLE` ("suspending"); while `Terminating` →
`UNAVAILABLE` ("terminating"). Nothing else in the hooks changes in M2.

### D10. `KeepAlive` every 30 s of silence, and what it means for idle

Both `Start` and `Connect` streams are wrapped by `grpc::keepalive::
KeepAliveStream` (hand-written `Stream` with a `tokio::time::Sleep` reset on
every yielded item) emitting `ProcessEvent{keepalive: KeepAlive{}}` when 30 s
pass without a message. Interval is a constructor parameter
(`ProcessGrpc::with_keepalive_interval`) so the integration test uses 200 ms.
Consequence, documented in the SDK: a client holding a background stream
open keeps the MicroVM out of the idle policy (`KeepAlive` bytes cross the
endpoint, M0 Q15); disconnecting the handle lets the idle policy act. This
matches E2B's "activity" semantics.

### D11. gRPC status mapping (server side, unary and pre-stream)

| Domain error | gRPC status |
|---|---|
| `EmptyCommand`, `InvalidCwd`, `UnknownUser`, `InvalidSignal`, cwd not a directory | `INVALID_ARGUMENT` |
| `RootNotAllowed` | `PERMISSION_DENIED` |
| `NotFound{pid}` (unknown, or ended for `SendInput`/`CloseStdin`/`SendSignal`), `ESRCH` | `NOT_FOUND` |
| `StdinNotOpen` (started with `stdin=false`), `EPIPE` on write | `FAILED_PRECONDITION` |
| `TooManyProcesses`, `TooManySubscribers` | `RESOURCE_EXHAUSTED` |
| `OutOfRange{oldest, next}` | `OUT_OF_RANGE` (message carries both numbers) |
| `Suspending`/`Terminating` phase | `UNAVAILABLE` |
| `Spawn(io::Error)` (`ENOENT`/`EACCES` for `cmd`) | `INVALID_ARGUMENT` ("cannot execute: <errno name>") |
| any other `Spawn`/I/O failure | `INTERNAL` |
| missing/invalid `x-access-token` (layer; the rejected body is drained, bounded, before the trailers-only answer) | `UNAUTHENTICATED` |

Errors after the first stream message are never gRPC statuses: they are the
`EndEvent` shapes of D1. `CloseStdin` on an already-closed pipe is `OK`
(idempotent). `SendInput` returns after `write_all` + `flush` completed
(bounded by the client's `request_timeout`), which is the stdin backpressure.

### D12. `HealthService.Metrics` from procfs

`ProcfsMetricsProbe` (`cfg(unix)`): `/proc/stat` first `cpu` line →
`CpuTimes{busy = user+nice+system+irq+softirq+steal, idle = idle+iowait}`;
`/proc/meminfo` → `MemTotal`, `MemAvailable` (`used = total - available`);
`nix::sys::statvfs::statvfs("/")` → `total = f_blocks × f_frsize`, `used =
(f_blocks - f_bfree) × f_frsize`; `cpu_count = std::thread::
available_parallelism()` (≥ 1). `cpu_used_pct` = two `/proc/stat` samples
100 ms apart per call (stateless; the call costs ≈ 100 ms). `timestamp_unix_ms`
from the wall clock. Parsers live in `rayd-core::metrics` with fixture-based
host tests; on non-unix the probe returns `MetricsError::Unsupported` →
`UNAVAILABLE`. `Metrics` keeps requiring `x-access-token`.

### D13. `rayd` adapter and application modules

```text
crates/rayd/src/
  adapters/mod.rs
  adapters/process_spawner.rs   TokioProcessSpawner{identity_switch, lookup: NixUserLookup} (cfg(unix));
                                 UnsupportedSpawner (cfg(not(unix)), every spawn -> SpawnError::Unsupported)
  adapters/procfs_metrics.rs    ProcfsMetricsProbe (cfg(unix)); UnsupportedProbe otherwise
  process/mod.rs
  process/manager.rs            ProcessManager{session, registry: Mutex<ProcessRegistry>, spawner, clock}
                                 start(input) -> Result<(Pid, SubscriberStream), ProcessError>
                                 connect(pid, from_seq) -> Result<SubscriberStream, ProcessError>
                                 send_input(pid, bytes) / close_stdin(pid) / send_signal(pid, sig) / list()
                                 spawns per process: stdout pump, stderr pump, stdin writer (mpsc<StdinCommand{Write(Bytes), Close}>),
                                 timeout task, waiter task; one reaper task per manager (every 5 s)
  process/subscriber.rs         Subscriber{sender: mpsc::Sender<ProcessEvent>, last_seq, detached}, SubscriberStream (ReceiverStream + truncation tail)
  grpc/process.rs               ProcessGrpc: proto <-> domain conversion, status mapping (D11), KeepAliveStream wrapping
  grpc/keepalive.rs             KeepAliveStream<S, F>
  grpc/health.rs                Metrics via MetricsProbe (D12)
  grpc/pending.rs               PendingProcessService removed; Filesystem/Pty/Code stay pending
  main.rs                       builds TokioProcessSpawner (IdentitySwitch from geteuid), UserPolicy from RAYITO_ALLOW_ROOT,
                                 ProcessManager, ProcfsMetricsProbe; passes them to grpc::router(session, manager, probe)
```

`grpc::router` signature becomes `router(session, process: Arc<ProcessManager>,
metrics: Arc<dyn MetricsProbe>)`; the M1 integration test adapts (it builds
a manager with the `UnsupportedSpawner` on Windows, the real one on unix).

Logging allowlist additions (`logging.rs` doc): `pid`, `status`, `exit_code`,
`signal`, `seq`, `subscribers`, `live_processes`, `duration_ms`, `errno`,
`identity_switch`. Never `cmd`, `args`, `envs`, `cwd`, `tag`, output or
stdin bytes.

### D14. Python SDK: modules, models and signatures

New/changed modules (`clients/python/src/rayito/`):

- `_models.py` adds:
  - `CommandResult(stdout: str, stderr: str, exit_code: int, error: str | None)` (frozen dataclass).
  - `ProcessInfo(pid: int, cmd: str, args: tuple[str, ...], envs: dict[str, str], cwd: str | None, tag: str | None, kind: Literal["process", "pty"])`.
  - `SandboxMetrics(cpu_used_pct: float, mem_used_bytes: int, mem_total_bytes: int, disk_used_bytes: int, disk_total_bytes: int, cpu_count: int, timestamp: datetime)`.
- `_process_base.py` (pure, shared by sync and async):
  - `DEFAULT_COMMAND_TIMEOUT_SECONDS = 60.0`, `STREAM_DEADLINE_GRACE_SECONDS = 5.0`, `SIGKILL = 9`, `SHELL = "/bin/bash"`, `SHELL_ARGS = ("-l", "-c")`.
  - `build_start_request(cmd, *, envs, user, cwd, stdin, timeout, tag) -> process_pb2.StartRequest` (validates `cmd` non-empty, `timeout` `None`/`0` → `timeout_ms=0`, negative → `InvalidArgumentException`, `user=""` → omitted).
  - `stream_deadline(timeout) -> float | None` (`timeout + 5` or `None`).
  - `validate_pid(pid) -> int`, `validate_from_seq(seq) -> int`.
  - `OutputAccumulator`: two `codecs.getincrementaldecoder("utf-8")(errors="replace")` instances, `on_stdout`/`on_stderr` callbacks, `last_seq`, `feed(event) -> tuple[str | None, str | None]`, `finish(end: EndEvent) -> CommandOutcome`.
  - `CommandOutcome = CommandResult | Exception`; `outcome_from_end(end, stdout, stderr)`: `exited`/`signaled` with `exit_code == 0` → `CommandResult`; non-zero → `CommandExitException(exit_code, stdout, stderr, error=status)`; `timeout` → `TimeoutException`; `output_truncated` → `SandboxException`; `suspending` → `SandboxStateException`.
  - `process_info_from_proto`, `metrics_from_proto`.
  - `stream_failure_exception(exc, *, health_ok: bool, state: str | None)`: `UNAVAILABLE`/`INTERNAL` with reset markers (`RST_STREAM`, `GOAWAY`, `Received RST`, `Socket closed`, `Connection reset`) → if `health_ok` → `SandboxException("stream reset but the sandbox is alive; reconnect with commands.connect(pid)")`; else state `TERMINATING|TERMINATED` → `SandboxNotFoundException`; `SUSPENDING|SUSPENDED` → `SandboxStateException`; otherwise `SandboxException`. Any other code → `translate_rpc_error`.
- `_transport.py`: `translate_rpc_error` gains `FAILED_PRECONDITION →
  InvalidArgumentException`, `OUT_OF_RANGE → NotFoundException`;
  `translate_stream_error("output_truncated")` → `SandboxException`;
  `is_stream_reset(exc)`.
- `sandbox_sync/commands.py`:

```python
class Commands:
    def __init__(self, sandbox: Sandbox) -> None: ...
    @overload
    def run(self, cmd: str, *, background: Literal[False] = False, envs=None, user=None, cwd=None,
            on_stdout=None, on_stderr=None, stdin: bool = False, timeout: float | None = 60.0,
            request_timeout: float | None = None, tag: str | None = None) -> CommandResult: ...
    @overload
    def run(self, cmd: str, *, background: Literal[True], ...) -> CommandHandle: ...
    def list(self, *, request_timeout: float | None = None) -> list[ProcessInfo]
    def kill(self, pid: int, *, request_timeout: float | None = None) -> bool      # SIGKILL; False on NotFoundException
    def send_stdin(self, pid: int, data: str | bytes, *, request_timeout=None) -> None  # str encoded UTF-8
    def close_stdin(self, pid: int, *, request_timeout=None) -> None
    def connect(self, pid: int, *, from_seq: int = 0, on_stdout=None, on_stderr=None,
                timeout: float | None = None, request_timeout=None) -> CommandHandle

class CommandHandle:
    pid: int            # property
    last_seq: int       # property
    stdout / stderr: str          # accumulated so far (properties)
    exit_code: int | None; error: str | None
    def wait(self) -> CommandResult   # consumes the stream in the calling thread; raises per outcome_from_end
    def kill(self) -> bool
    def disconnect(self) -> None      # cancels the stream; the process keeps running
    def send_stdin(self, data: str | bytes) -> None
    def close_stdin(self) -> None
    def __iter__(self) -> Iterator[tuple[str | None, str | None, bytes | None]]   # (stdout, stderr, pty=None), KeepAlive skipped
```

- `sandbox_async/commands.py`: `AsyncCommands`/`AsyncCommandHandle` with the
  same names, `async` methods, `__aiter__`; built on `grpc.aio` streams.
- `sandbox_sync/main.py` / `sandbox_async/main.py`: `commands` property
  returns the `Commands` created in `__init__`; `get_metrics(request_timeout
  =None) -> SandboxMetrics` via `_call_unary`; `_stream_channel` opened
  lazily on first background/`connect` use (second and last channel; closed
  in `close()`); `_open_stream(factory)` and `_stream_failure(exc)` (below).
  The `commands` `NotImplementedError` stubs disappear; `files`, `pty`,
  `run_code` keep theirs.

Semantics fixed here:

- Foreground `run` = build a `CommandHandle` on the **unary** channel and
  call `wait()`; `on_stdout`/`on_stderr` run in the calling thread with the
  decoded text of each chunk. Background `run` and `connect` use the
  **stream** channel. Never a channel per call.
- `timeout` (E2B vocabulary) is the server-enforced `timeout_ms`; default
  60 s for `run` in both modes (`None`/`0` disables); `connect` sends none.
  gRPC deadline for `Start` = `timeout + 5 s` (or none), for `Connect` =
  `timeout` argument (or none). `request_timeout` (default: the sandbox's
  `request_timeout`, 60 s) is the deadline of unary calls.
- `wait()` outcomes: exit 0 → `CommandResult`; non-zero or signaled →
  `CommandExitException` (`error` = `"exited"`/`"signaled"`); server timeout
  or gRPC `DEADLINE_EXCEEDED` → `TimeoutException`; `output_truncated` →
  `SandboxException`; reset → `_stream_failure`. `wait()` is idempotent
  after completion (returns the cached result or re-raises).
- Iteration and `wait()` consume the stream lazily (E2B behaviour); a
  background handle nobody reads is subject to the server's 30 s stall rule
  (D7). Documented in the `Commands.run` docstring.
- `kill(pid)` → `SendSignal(9)`; `True` on success, `False` when the pid is
  unknown/ended. After a kill, `wait()` raises `CommandExitException(
  exit_code=137, error="signaled")`.
- Text decoding: incremental UTF-8 with `errors="replace"`, one decoder per
  stream, flushed at `EndEvent`.

### D15. SDK transport: two channels, keepalive, 403 on streams, resets

- Channel options stay `CHANNEL_OPTIONS` (keepalive 30 s, permit without
  calls, 64 MiB receive). Both channels share the `ProxyAuthPlugin`, so the
  refresher rotates the JWE for streams too (metadata is read per call).
- Proxy 403 on a **stream** is retried once, only at open: `_open_stream`
  creates the call and pulls the first message; if that raises with
  `is_proxy_forbidden`, nothing was consumed and the proxy never forwarded
  the request to `rayd`, so `refresh_all()` and re-issue once. A 403 after
  the first message cannot happen (Q28: expiry is evaluated at connect).
- Mid-stream `UNAVAILABLE`/reset: `_stream_failure(exc)` probes `Health`
  with a 5 s timeout; if it answers → `SandboxException` (alive; M5 adds
  re-subscription); otherwise one `get_microvm` and the mapping of D14.
  `is_running()` is not used because it swallows errors.
- Connection budget: 30 sequential foreground commands reuse one channel;
  the e2e asserts no `RateLimitException`/429 appears.

### D16. Tests

**`rayd-core` (host, Windows and Linux)** — `cargo test -p rayd-core`:
`plan_spawn` (defaults, request override, root refused/allowed, unknown
user, empty cmd, cwd precedence and validation), `build_child_env` (order,
override, nothing inherited), `OutputRing` (seq from 1, eviction by bytes,
`replay_from` boundaries incl. `next_seq`), `ProcessRegistry` (cap 256,
subscriber cap 8, `mark_ended` + `reap_expired` at 30 s with the fake clock,
`live()` excludes ended), `TimeoutPlan`/`end_from_wait` (exited, signaled,
timeout after SIGTERM exit 0, timeout after SIGKILL), `metrics` parsers with
`/proc/stat` and `/proc/meminfo` fixtures and `cpu_used_pct` edge cases.

**`rayd` integration, `cfg(unix)`** — `crates/rayd/tests/m2_process.rs`
(`#![cfg(unix)]`, in-process router on `127.0.0.1:0`, `/run` installed via
the hooks router, keepalive interval 200 ms, `IdentitySwitch::KeepCurrent`
when not root): `echo` (StartEvent first, data, EndEvent exited 0 last);
`sh -c 'exit 3'`; stderr routing; `cat` with `stdin=true` + `SendInput` +
`CloseStdin`; `SendInput` on `stdin=false` → `FAILED_PRECONDITION`; `sleep
30` with `timeout_ms=300` → `EndEvent{timeout, deadline_exceeded}` within
2 s and the pid gone from `List`; `SendSignal(9)` → `signaled`, 137;
`Connect(from_seq=1)` replays everything, `from_seq=0` only new,
`from_seq=999` → `OUT_OF_RANGE`, `Connect` after exit within retention →
Start+End, after the reaper (retention overridden to 200 ms) → `NOT_FOUND`;
`List` shows live only; 257th `Start` → `RESOURCE_EXHAUSTED`; 9th `Connect`
→ `RESOURCE_EXHAUSTED`; a subscriber that never reads while the process
writes 5 MiB → `output_truncated` (stall timeout overridden to 300 ms) and
the process finishes; keepalive arrives on a silent `sleep 1`; env from
scratch (`env` shows no `RAYD_LOG`, has `HOME/USER/LOGNAME/PATH`); cwd
default and invalid cwd; `Metrics` returns `cpu_count >= 1` and
`mem_total_bytes > 0`; root refused without `RAYITO_ALLOW_ROOT` (runs only
as root: `#[ignore]` unless `geteuid()==0`). `cargo clippy --workspace
--all-targets -- -D warnings` clean on both hosts.

**Python unit** — `clients/python/tests/unit/`: `fake_process.py` adds
`FakeProcessService` to the in-process fake `rayd` (registered by the
`fake_rayd` fixture; every RPC verifies `x-access-token` like `Metrics`
does). It interprets `args[-1]` of the `/bin/bash -l -c` wrapper with a tiny
scripted table (`echo X` → stdout, `exit N`, `sleep N` honouring `timeout_ms`
and `SendSignal`, `cat` echoing stdin until `CloseStdin`, `err X` → stderr,
`big N` → N bytes in 32 KiB chunks, unknown → 127) and keeps a per-pid ring
for `Connect(from_seq)`. Tests (`test_commands_sync.py`,
`test_commands_async.py`, `test_process_base.py`): request shape (shell
wrapper, `timeout_ms`, `stdin`, `user`, `cwd`, `envs`, `tag`), deadline
math, decoder split across chunks (a multibyte char cut at a 32 KiB
boundary), `CommandExitException` fields, `TimeoutException` on `timeout`
status, `kill` True/False, `send_stdin`/`close_stdin`, `connect` replay,
`list` mapping incl. `kind`, `output_truncated` → `SandboxException`,
`stream_failure_exception` matrix, 403-on-open re-mint (Stubber expects one
extra `create_microvm_auth_token`), two channels max (the fake counts
distinct client ports), `get_metrics` mapping. `uv run pytest tests/unit`,
`ruff check`, `ruff format --check`, `mypy src` all clean.

**e2e (real AWS)** — `clients/python/tests/e2e/test_m2_processes.py`; see
"Acceptance test list".

### D17. Image

`image/Dockerfile`: keep `useradd -m -u 1000 -s /bin/bash user`; add
`ln -sf /usr/bin/python3.12 /usr/local/bin/python3` (the acceptance test
runs `python3 -m http.server`; `/usr/local/bin` precedes in the child
`PATH`) and a build-time sanity `RUN` that fails the image build if
`/bin/bash`, `whoami`, `id`, `sleep`, `head`, `tr`, `env`, `python3` are
missing; do **not** set `RAYITO_ALLOW_ROOT`; `rayd` stays `CMD` as root
(uid 0, capped caps) because it must `setuid` to 1000. Publish with
`image-publish` (new version, three-state gate). No `create-microvm-image`
parameter changes.

### D18. Performance budgets (checked in e2e/integration, logged, asserted where marked)

| Budget | Value | Where |
|---|---|---|
| `Start` → `StartEvent` in-VM (`/bin/true`) | ≤ 20 ms p50 | integration (logged) |
| `commands.run("echo hola")` via proxy | ≤ 1.5 s p95 over 30 runs | e2e (asserted total ≤ 45 s) |
| Timeout precision (`sleep 10`, `timeout=2`) | `TimeoutException` within 4 s | e2e (asserted) |
| 3 MB stdout through the 4 MB/s link | ≤ 3 s, no loss | e2e (asserted length, logged time) |
| `KeepAlive` cadence | 30 s ± 1 s | integration (200 ms override) + e2e `sleep 35` |
| `Metrics` call | ≤ 200 ms in-VM | integration (logged) |
| Memory per pid | ≤ 1 MiB ring + 2 MiB × subscribers (≤ 8) | by construction |
| Retained output, whole sandbox | ≤ 256 ended entries × 1 MiB ring (`max_retained`), whatever the spawn rate | registry unit test (300 ended → 256 kept) + integration (`max_retained = 2`) |

### D19. Docs alignment

`MILESTONES.md` M2 acceptance snippet: replace the `r.error ==
"deadline_exceeded"` lines with the `TimeoutException` assertion and add
`get_metrics()`; M6 bullet notes `get_metrics()` moved to M2.
`ARCHITECTURE.md` `ProcessService` row: add the 30 s stall rule and
`output_truncated` status; §"Suspend / resume" table already says the
monotonic clock advances. `SECURITY.md` T7 row: unchanged wording, now
implemented (add "M2 ✔").

## Risks / Trade-offs

- [Proxy pings] The client sends HTTP/2 pings every 30 s without calls; the
  proxy's ping policy is unknown and could `GOAWAY` an idle stream channel →
  Mitigation: the e2e `sleep 35` foreground and a 60 s idle stream channel
  check surface it; fallback is raising `keepalive_time_ms` to 60 s (still
  under the observed > 180 s idle survival, M0 Q15).
- [Monotonic timers across pause] Timeouts and retention expire during a
  pause → Mitigation: accepted for M2 and documented; M5 re-arms in
  `/resume`. The M2 e2e does not pause.
- [`RLIMIT_NOFILE` hard 1024 in the VM] Root in the VM has no
  `CAP_SYS_RESOURCE`, so the hard limit cannot be raised even before
  `setuid` (measured 2026-09-15) → Mitigation: the clamp path of D3 is the
  production path; the e2e asserts soft == hard in {4096, 1024} and logs the
  effective value; `additionalOsCapabilities` is an M6 decision.
- [Early rejection through the proxy] A trailers-only `UNAUTHENTICATED`
  written before the request body is consumed makes hyper reset the
  half-open stream and the proxy delivers `RST_STREAM(CANCEL)` to the client
  (`CANCELLED`, measured 2026-09-15; M1 had passed on timing) → Mitigation:
  the token layer drains the rejected body (at most 1 MiB / 2 s) before
  answering.
- [Stall rule vs lazy handles] A background handle nobody reads is detached
  after 30 s of a full channel → Mitigation: documented; `connect(pid)`
  recovers; memory stays bounded.
- [`bash -l` cost and `/etc/profile`] Login shell adds a few ms and may
  alter `PATH` → Mitigation: acceptable and E2B-identical; the e2e asserts
  `whoami`, `HOME`, `USER`, `LOGNAME`, not `PATH` verbatim.
- [Unprivileged dev mode] `KeepCurrent` runs processes as the developer's
  user → Mitigation: only when `geteuid() != 0`, logged once; the image
  always runs as root; the root-refusal test runs as root only.
- [256 × 1 MiB rings] Worst case 256 MiB of retained output on a 2 GB VM →
  Mitigation: rings hold real output only; retention 30 s; and the
  `max_retained = 256` cap makes the worst case real (without it,
  `head -c 1M /dev/zero` in a loop at ~30 spawns/s would retain ~900 MiB
  within the 30 s window); accepted.
- [Metrics 100 ms sample] Each `get_metrics()` costs ≈ 100 ms → acceptable
  for a diagnostic call; documented.

## Migration Plan

1. Merge; CI green (`buf lint`, `buf breaking`, `cargo fmt/clippy/test`,
   `pytest tests/unit`, `ruff`, `mypy`, ARM64 build).
2. `make image-zip` + `image-publish` → new `rayito-base` version;
   three-state gate.
3. `RAYITO_E2E=1 RAYITO_TEMPLATE=<arn> make test-e2e` → `test_m1_hello.py`
   and `test_m2_processes.py` green.
4. Rollback: `update-microvm-image-version --status INACTIVE` on the new
   version; the SDK is backwards compatible with the M1 agent for
   `Health`/lifecycle (commands would answer `UNIMPLEMENTED` →
   `InvalidArgumentException`).
5. Acceptance agent archives the change.

## Open Questions

None blocking. To measure during e2e and record in `AWS_API_NOTES.md` §16:
proxy behaviour with 30 s client pings on an idle stream channel; observed
`Start` latency through the proxy for server-streams (first server-stream
measurement, extends Q17).

## Acceptance test list

`clients/python/tests/e2e/test_m2_processes.py`, marker `e2e`, one
`sandbox` fixture (`maximumDurationInSeconds=900`, no `idlePolicy`,
`ingress=["ALL_INGRESS"]`), each `run` foreground unless stated:

1. `run("echo hola")`: `stdout.strip() == "hola"`, `stderr == ""`, `exit_code == 0`, `error is None`.
2. `run("whoami").stdout.strip() == "user"`; `run("id -u").stdout.strip() == "1000"`; `run("id -G")` contains `1000`.
3. `run("env")`: contains `HOME=/home/user`, `USER=user`, `LOGNAME=user`, a `PATH=` line; contains neither `RAYD_LOG` nor `AWS_LAMBDA_MICROVM_IMAGE_ARN`; `run("pwd").stdout.strip() == "/home/user"`.
4. `run("echo $FOO", envs={"FOO": "bar"}).stdout.strip() == "bar"`; `run("pwd", cwd="/tmp").stdout.strip() == "/tmp"`; `run("true", cwd="/does/not/exist")` raises `InvalidArgumentException`.
5. `run("ulimit -Sn; ulimit -Hn; ulimit -u; ulimit -c")` → `NOFILE` soft == hard in {`4096`, `1024` (Lambda MicroVMs hard cap)}, then `512`, `0`; the effective value is logged.
6. `run("echo err >&2; exit 3")` raises `CommandExitException` with `exit_code == 3`, `stderr.strip() == "err"`, `error == "exited"`.
7. `run("sleep 10", timeout=2)` raises `TimeoutException`; elapsed ≤ 4 s; afterwards no `ProcessInfo` with that pid in `commands.list()` (pid captured via a background variant: `h = run("sleep 10", background=True, timeout=2)`, `h.wait()` raises `TimeoutException`, `h.pid not in {p.pid for p in commands.list()}`).
8. `h = run("sleep 30", background=True, tag="m2")`: `any(p.pid == h.pid and p.kind == "process" and p.tag == "m2" for p in commands.list())`; `commands.kill(h.pid) is True`; `h.wait()` raises `CommandExitException` with `exit_code == 137` and `error == "signaled"`; `commands.kill(h.pid) is False`.
9. stdin: `h = run("cat", background=True, stdin=True)`; `h.send_stdin("hola\n")`; `h.close_stdin()`; `h.wait().stdout == "hola\n"`. `h2 = run("sleep 30", background=True)`; `h2.send_stdin("x")` raises `InvalidArgumentException` (`cat` without stdin reads `/dev/null` and exits before the RPC lands, so it would be `NotFoundException`); `h2.kill() is True`.
10. connect/replay: `h = run("for i in 1 2 3; do echo $i; sleep 1; done", background=True)`; `full = commands.connect(h.pid, from_seq=1)`; `full.wait().stdout == "1\n2\n3\n"`; `h.wait().stdout == "1\n2\n3\n"`; `commands.connect(h.pid)` immediately after (within 30 s) → `wait()` returns `exit_code 0` with empty stdout; `commands.connect(999999)` raises `NotFoundException`.
11. callbacks: `run("echo a; echo b >&2", on_stdout=out.append, on_stderr=err.append)` → `"".join(out) == "a\n"`, `"".join(err) == "b\n"`.
12. large output: `r = run("head -c 3000000 /dev/zero | tr '\\0' a")` → `len(r.stdout) == 3_000_000`; log the elapsed time.
13. keepalive through the proxy: `run("sleep 35", timeout=None).exit_code == 0`.
14. root policy: `run("whoami", user="root")` raises `AuthenticationException` (agent `PERMISSION_DENIED`, `proxy_rejected is False`).
15. `get_host`: `run("python3 -m http.server 3000 --bind 0.0.0.0", background=True, timeout=None)`; `host = sbx.get_host(3000)`; `urllib.request` GET `host.url` with `host.headers` → 200 within 10 s (retry every 0.5 s); the same GET without headers → `HTTPError` 403.
16. `m = sbx.get_metrics()`: `m.cpu_count >= 1`, `m.mem_total_bytes > 0`, `m.mem_used_bytes <= m.mem_total_bytes`, `m.disk_total_bytes > 0`, `0 <= m.cpu_used_pct <= 100`, `abs(now - m.timestamp) < 60 s`.
17. connection budget: 30 × `run(f"echo {i}")` sequential, each `stdout.strip() == str(i)`, no exception, total ≤ 45 s (logged); then `commands.list() == []`.
18. cross-process: `Sandbox.connect(sbx.sandbox_id, access_token=sbx.access_token)` → `run("echo hola")` ok; `Sandbox.connect(sbx.sandbox_id, access_token=<other valid base64url>)` → `run("true")` raises `AuthenticationException`.
19. async parity: `asyncio.run(...)` with `AsyncSandbox.connect(...)`: `(await sbx.commands.run("echo async")).stdout.strip() == "async"`; background `sleep 5` handle `await h.kill()` then `await h.wait()` raises `CommandExitException`.
20. teardown: `sbx.kill() is True`; `get_info().state in TERMINAL_STATES` within 30 s (reuse M1's poll helper).

Cost: one image version (+$0.037) and one MicroVM of ≈ 3 min (< $0.01).

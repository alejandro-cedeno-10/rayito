## Context

State after M4 (accepted 2026-09-15 against real AWS, image `rayito-base`
8.0): `rayd` serves `HealthService`, `ProcessService`, `FilesystemService`
and `CodeService`; `PtyService` is `PendingPtyService` (`UNIMPLEMENTED`)
and `Code.Reattach` answers `UNIMPLEMENTED`. The lifecycle domain already
has the phase machine (`Booting → Ready → Running → Suspending → Resumed`,
`Terminating`), `suspend_generation`, `resume_generation`, `suspended_at`
and `clock_offset_ms` (`rayd-core::lifecycle`, `session`, `clock`), and
`HealthSnapshot` already carries `resume_generation`, `clock_offset_ms`
and `kernel_state_lost` (the last one always `false`). The hooks router
answers `/suspend` with the phase transition plus `libc::sync()` and
`/resume` with the transition plus a background `reseed`; both inside the
80 % budget of the 30 s declared in `scripts/publish_image.py`. The
process registry (`rayd-core::process::registry`) already models
`ProcessKind::Pty`, the 1 MiB ring with `seq`, the 30 s retention and the
per-subscriber stall rule; `ProcessManager` owns it privately. The sidecar
protocol already has the `quiesce` and `resume` ops (the latter probes
each kernel with `kernel_info`, 5 s, ≤ 8 concurrent, and replies
`{"contexts": [{"context_id", "alive"}]}`). The Python SDK has
`pause(wait)` / `resume(wait)` over the control plane (the latter re-mints
the JWE and waits for `Health`), `CommandHandle` with `last_seq`,
`commands.connect(pid, from_seq)`, `WatchHandle` consuming in a daemon
thread, `run_code` consuming `Execute` in the calling thread, and a
stream-failure classifier that turns a mid-stream `UNAVAILABLE`/reset
into an exception after one 5 s `Health` probe. `Sandbox.pty` raises
`NotImplementedError`.

Measured facts that shape this design (`AWS_API_NOTES.md`):

- A `/suspend` answering non-200 **terminates** the VM in < 5 s with
  `stateReason` "Suspend lifecycle hook returned HTTP status 500…"; no
  retries observed (§5, §8, Q10). `/suspend` must always answer 200.
- `CLOCK_MONOTONIC` advances during a suspension (Δmono ≈ Δwall, 313 s
  measured) and AWS corrects the wall clock on resume (−1 s); a
  `sleep 120` started before the pause finished right at resume (§15,
  Q5, Q19). Every tokio timer armed before a pause fires at resume unless
  it is re-validated.
- Memory of every process, pipes, PTYs, inotify, `ipc://` sockets and
  the ipykernel with its variables survive (`x == 42` after 313 s); a
  `kernel_info` round-trip costs 1.9 ms (Q5–Q7, §15).
- Incoming proxy streams are lost on resume: the client sees the
  connection closed (`Response ended prematurely`, seen ~57 s after the
  idle suspend in M0) and must reconnect (§15, Q15).
- A stream with no bytes does not count as traffic for the idle policy;
  keepalives with data do (Q15).
- `resume-microvm` → first 200 in 1.2 s; auto-resume answers the first
  request in 1.1–1.6 s and AWS retains that request; a failed auto-resume
  is a 502 (§5, Q4).
- A JWE minted before the pause is still valid after it; expiration is
  only checked at connection time (Q28). `SuspendMicrovm` is 2 TPS (§5).
- 8 concurrent connections per 1-vCPU MicroVM; the SDK keeps two channels
  (§7).
- The guest is root with capped capabilities (`chown`, `setuid`, `setgid`,
  `kill` present; no `sys_admin`), `/bin/bash` and `setpriv` exist (Q20).
  Whether `devpts` is mounted in the guest was **not** measured in M0: it
  is the first thing this milestone measures (D17, task 0.1).

Constraints: `openspec/project.md` hard rules (no invented AWS parameters,
`.proto` is the source of truth, hexagonal boundaries, ARM64 musl, security
defaults ship now, `clippy` pedantic, no `unwrap` outside tests,
identifiers in English, no inline comments in bodies, never log PTY bytes,
code, output, envs or tokens).

## Goals / Non-Goals

**Goals:**

- `PtyService` complete on real AWS: an interactive login shell as the
  sandbox user with a real controlling terminal, resize, kill, server
  timeout, gap-free re-attach, the shared process registry and caps.
- `/suspend` and `/resume` implementing the `ARCHITECTURE.md` checklists
  exactly, with every client stream closed cleanly before the checkpoint,
  nothing killed, deadlines that exclude suspended time, kernels probed
  and `kernel_state_lost` honest, and a 200 that never fails to arrive.
- `Code.Reattach` real: executions outlive their stream, are recorded in
  a bounded ring and can be followed again after a resume.
- The Python SDK's reconnection contract: every live handle, watch and
  `run_code` survives a `pause()`/`resume()` or an auto-resume without
  user code noticing beyond latency; `pause()`/`resume()`/`connect()` and
  `.pty` with E2B names, sync and async identical.
- Every rule below has a host-side unit test, a `cfg(unix)` integration
  test, an SDK unit test against the fakes, or an e2e assertion.

**Non-Goals:**

- Bidirectional streams (ADR-005), a PTY over WebSocket, terminal
  recording, `script`-style replay of the whole scrollback.
- Replaying watch events that happened while suspended (`inotify` state
  survives but nobody was listening; the SDK re-issues the watch).
- Re-running an `Execute` whose stream was cut before `started` (it would
  double-run the cell; the SDK raises instead).
- Rotating IMDS credentials or reopening outbound connections in `rayd`
  (it has none; sandbox code owns its own connections).
- Changing hook timeouts or any `create-microvm-image`/`run-microvm`
  parameter; `set_timeout` (ADR-007); pre-warmed pools; the TypeScript
  client; the E2B shim (M6).
- Making the SDK's unary retry idempotency-aware beyond "one retry after
  a successful reconnect" (documented caveat, E2B-equivalent).

## Decisions

### D1. Proto usage: one additive field, otherwise as written

`pty.proto` gains `PtyServerMessage.seq` (`uint64`, field 5, outside the
`oneof`): the monotone per-PTY sequence of `data` messages, `0` on
`started`, `exited` and `keepalive`, the same rule as `DataEvent.seq` and
`ExecuteEvent.seq`. Without it `Connect(pid, from_seq)` could not be
gap-free for a terminal, and the SDK's `PtyHandle` could not share
`CommandHandle.last_seq`. Done in this change: `buf lint` and `buf build`
clean, `python scripts/gen_python.py` regenerated `clients/python/src/
rayito/v1/pty_pb2*`; Rust regenerates at `cargo build`. No other proto
edit.

Wire mapping fixed here:

| SDK call | RPC | Request | Response / notes |
|---|---|---|---|
| `pty.create(size, user, cwd, envs, shell, on_data, timeout)` | `PtyService.Create` (server-stream) | `PtyStart{size?, envs, cwd?, user?, shell?, timeout_ms}` | `started{pid}` (seq 0) → `data`* (seq 1, 2, …) → `exited{…}` (seq 0); `keepalive` (seq 0) after 30 s of silence |
| `pty.connect(pid, from_seq)` | `PtyService.Connect` | `ConnectRequest{pid, from_seq}` (shared with `ProcessService`) | `started{pid}` → replayed `data` with `seq >= from_seq` → live `data` → `exited`; `from_seq = 0` → live only |
| `pty.send_input(pid, data)` | `PtyService.SendInput` | `SendInputRequest{pid, data}` (shared) | `SendInputResponse{}` after the bytes are written to the master |
| `pty.resize(pid, size)` | `PtyService.Resize` | `ResizeRequest{pid, size}` | `ResizeResponse{}` after `TIOCSWINSZ` |
| `pty.kill(pid)` | `PtyService.Kill` | `KillPtyRequest{pid}` | `KillPtyResponse{}`; `SIGKILL` to the process group |
| `run_code` after a resume | `CodeService.Reattach` | `ReattachRequest{context_id, execution_id, from_seq}` | `ExecuteEvent` sequence with `seq >= from_seq`, `keepalive` every 5 s, ends after `end` |
| `get_health()`, reconnect poll | `HealthService.Health` | `HealthRequest{}` | `resume_generation`, `clock_offset_ms`, `kernel_state_lost` real |

Field semantics:

- `PtyStart.size`: absent → `80×24`; present with `cols` or `rows` equal
  to `0` or above `MAX_PTY_DIMENSION = 4096` → `INVALID_ARGUMENT`.
- `PtyStart.envs`, `cwd`, `user`: exactly the `StartRequest` rules of M2
  (`_payload.validated_envs`, `resolve_cwd`/`validate_cwd`,
  `resolve_username` + `UserPolicy`).
- `PtyStart.shell`: absent or empty → the user's login shell from the
  image's user database (`pw_shell`; `/bin/sh` if empty); present → must
  be an absolute path (`INVALID_ARGUMENT` otherwise) and executable
  (`INVALID_ARGUMENT` "shell not executable" from the spawn error).
- `PtyStart.timeout_ms`: `0` = no limit; otherwise the M2 rule (SIGTERM
  to the group at the deadline, SIGKILL 5 s later, `PtyExited{exited:
  false, status:"timeout", error:{code:"deadline_exceeded"}}`), measured
  on the **running clock** (D6).
- `PtyExited`: same shape and same five statuses as `EndEvent`
  (`exited`, `signaled`, `timeout`, `suspending`, `output_truncated`),
  `exit_code = 128 + signal` on a signal, identical builders in the
  domain (`ProcessEnd`).
- `PtyServerMessage.data`: raw master-side bytes, at most 16 KiB per
  message (`PTY_CHUNK_BYTES`), never decoded or logged by `rayd`.
- `ConnectRequest.from_seq` for a PTY: `0` live only; `N` replays from
  the 1 MiB ring; `N` below the oldest retained `seq` or above the next
  one → `OUT_OF_RANGE` before any message (M2 rule).
- `ReattachRequest`: `context_id` (empty → `default`), `execution_id`
  (`exec-<16 hex>`, else `INVALID_ARGUMENT`), `from_seq` (`0` live only;
  `N` replays counted events with `seq >= N`; evicted → `OUT_OF_RANGE`).
- `HealthResponse.uptime_ms`: monotonic since boot, suspended time
  included (documented, unchanged). `resume_generation`: number of
  accepted `/resume` transitions this boot. `clock_offset_ms`:
  `wall_delta − monotonic_delta` between the last `/suspend` and
  `/resume` (≈ −1000 measured). `kernel_state_lost`: D10.

Alternatives considered: no `seq` on PTY frames with `from_seq` refused
(rejected: the registry ring exists per entry anyway and the SDK handle
already tracks `last_seq`; refusing replay would make the PTY the only
stream that loses bytes across a resume); a `PtyData{bytes, seq}` message
(rejected: `bytes data` inside the `oneof` is already generated in three
languages and the field outside the `oneof` is the pattern `ExecuteEvent`
uses).

### D2. PTY domain (`rayd-core::pty`)

New module, pure, host-tested. Constants: `DEFAULT_PTY_COLS = 80`,
`DEFAULT_PTY_ROWS = 24`, `MAX_PTY_DIMENSION = 4096`, `PTY_CHUNK_BYTES =
16 * 1024`, `PTY_TERM = "xterm-256color"`, `PTY_LOCALE = "C.UTF-8"`,
`PTY_SHELL_ARGS = ["-i", "-l"]`, `PTY_DRAIN_GRACE = 500 ms`,
`FALLBACK_SHELL = "/bin/sh"`.

- `PtySize { cols: u16, rows: u16 }` (validated newtype): `PtySize::new
  (cols: u32, rows: u32) -> Result<Self, PtyError::InvalidSize{cols, rows,
  max}>`, `Default` = 80×24, `from_request(Option<(u32, u32)>)`.
- `ProcessIdentity` (M2) gains `shell: String`, filled by `NixUserLookup`
  from `pw_shell` (`/bin/sh` when empty) and by `CurrentUserLookup` the
  same way; every existing struct literal (tests, fakes) adds the field.
- `resolve_shell(request: Option<&str>, identity: &ProcessIdentity) ->
  Result<String, PtyError>`: `None`/`""` → `identity.shell`; a relative
  path → `PtyError::InvalidShell`.
- `pty_base_env(shell: &str) -> BTreeMap<String, String>` = `{TERM:
  "xterm-256color", LANG: "C.UTF-8", LC_ALL: "C.UTF-8", SHELL: <shell>}`.
- `PtySpawnInput { size: Option<(u32, u32)>, envs: BTreeMap, cwd:
  Option<String>, user: Option<String>, shell: Option<String>, timeout:
  Option<Duration> }` and `PtyPlan { spec: SpawnSpec, size: PtySize,
  config: ProcessConfigInfo }`.
- `plan_pty(input, defaults: &RunDefaults, policy, lookup) ->
  Result<PtyPlan, PtyError>`: size → identity (`resolve_username`,
  `policy.authorize`, `lookup`, `authorize_identity`, as `plan_spawn`) →
  shell → env `build_child_env(identity, &layered, &input.envs)` where
  `layered = pty_base_env(shell)` overridden by `defaults.envs` (so the
  override order is identity vars → PTY defaults → payload envs → request
  envs, last wins) → cwd (`resolve_cwd` + `validate_cwd`) → `SpawnSpec{
  program: shell, args: ["-i", "-l"], env, cwd, identity, stdin:
  StdinMode::Pipe, limits: ResourceLimits::default()}`; `config` for the
  registry is `ProcessConfigInfo{cmd: shell, args: ["-i","-l"], envs:
  input.envs, cwd: Some(cwd)}`.
- `PtyError` (thiserror): `InvalidSize{cols, rows, max}`, `InvalidShell`,
  `NotAPty{pid}`, `NoPtyDevices`, `Process(#[from] ProcessError)`,
  `Unsupported`. `Display` never quotes envs, cwd or bytes.
- Port `rayd-core::pty::ports::PtyBackend`:

```rust
pub trait PtyChild: Send { fn pid(&self) -> Pid; }
pub trait PtyBackend: Send + Sync {
    type Pty: PtyChild;
    fn open(&self, spec: &SpawnSpec, size: PtySize) -> Result<Self::Pty, SpawnError>;
    fn signal_group(&self, pid: Pid, signal: i32) -> Result<(), SignalError>;
}
```

The I/O side (`take_reader`, `take_writer`, `resize`, `wait`) is not a
domain concern: it lives in `rayd::pty::child::PtyIo` (D3), exactly as
`ChildIo` extends `SpawnedChild` in M2.

- `rayd-core::process::registry::ProcessRegistry` gains `kind(pid) ->
  Result<ProcessKind, ProcessError::NotFound>` and `ProcessError` gains
  `WrongKind{pid, expected: ProcessKind}` (→ `FAILED_PRECONDITION`, D5).

### D3. PTY adapter (`rayd::adapters::pty_backend::NixPtyBackend`, `cfg(unix)`)

`open(spec, size)`:

1. `nix::pty::openpty(Some(&Winsize{ws_col: size.cols, ws_row: size.rows,
   ws_xpixel: 0, ws_ypixel: 0}), None)` → `OpenptyResult{master, slave}`
   (`OwnedFd`s). Default termios (cooked mode, echo on) so a shell behaves
   as on a real terminal. Failure (`ENOENT`/`EACCES` on `/dev/ptmx`,
   `ENOSPC`) → `SpawnError::Failed(errno_name)`; `main` additionally logs
   `pty_devices = Path::new("/dev/ptmx").exists()` at boot and the manager
   turns `!exists` into `PtyError::NoPtyDevices` before calling `open`.
2. Under `IdentitySwitch::Enforce`: `fchown(slave, uid, gid)` and
   `fchmod(slave, 0o620)` so programs that reopen `/dev/tty` or
   `/dev/pts/N` as the user (`tty`, `ssh`, `passwd`, `script`) work;
   `openpty` granted the slave to root (the caller).
3. `tokio::process::Command::new(spec.program).args(spec.args)
   .env_clear().envs(spec.env).current_dir(spec.cwd).stdin(slave.try_clone())
   .stdout(slave.try_clone()).stderr(slave).kill_on_drop(false)` —
   **without `process_group(0)`**: `std` applies `setpgid` before the
   `pre_exec` closures and `setsid` fails with `EPERM` for a process-group
   leader. `pre_exec` does, in order: `setsid()` (new session, pgid ==
   pid, so `killpg(pid)` keeps working), `ioctl(0, TIOCSCTTY, 0)` (the
   slave becomes the controlling terminal; allowed for a session leader
   without privilege), then the M2 `PreExecPlan::apply()` (rlimits,
   `setgroups`, `setgid`, `setuid`). All three are async-signal-safe.
4. Parent: the slave copies are dropped after `spawn`; the master is set
   `O_NONBLOCK` and wrapped in `tokio::io::unix::AsyncFd<OwnedFd>`
   (`PtyMaster`), which implements `AsyncRead` (`readable()` + `read(2)`;
   `EIO` means every slave is closed → reported as EOF) and `AsyncWrite`
   (`writable()` + `write(2)`); `resize_handle()` returns a cheap clone
   (`Arc`) that issues `ioctl(master, TIOCSWINSZ, &winsize)` — the kernel
   delivers `SIGWINCH` to the foreground group by itself.
5. `NixPty{pid, child, master}` implements `PtyChild` and `PtyIo`:
   `take_reader`, `take_writer` (both boxed over the shared master),
   `resize_handle`, `wait` (`child.wait()` → `WaitOutcome`).
6. `signal_group` = the M2 `killpg` implementation (shared function).

`UnsupportedPtyBackend` (`cfg(not(unix))`) answers `SpawnError::Unsupported`
so the workspace compiles and the host tests run on Windows.
`adapters/mod.rs` exports `PlatformPtyBackend`.

`rayd::pty::child`: `PtyIo: PtyChild + 'static { fn take_reader(&mut
self) -> Option<ChildReader>; fn take_writer(&mut self) ->
Option<ChildWriter>; fn resize_handle(&self) -> PtyResizer; fn wait(&mut
self) -> WaitFuture<'_>; }`, `PtyResizer: Send + Sync { fn resize(&self,
size: PtySize) -> io::Result<()> }`, `PtySpawner: PtyBackend<Pty: PtyIo>`.

### D4. `PtyManager` and the shared registry (`rayd::pty::manager`)

The registry becomes shared: `pub type SharedRegistry =
Arc<Mutex<ProcessRegistry<SubscriberSink>>>`, created once in `main` (and
in every integration test) and handed to both `ProcessManager::new(...,
registry)` and `PtyManager::new(session, backend, lookup, policy, registry,
PtySettings{stall_timeout, drain_grace})`. One live cap (256), one `List`,
one reaper (the process reaper tick reaps both kinds and, D9, ended
executions). The fan-out and finish logic of `ProcessManager` moves to
`rayd::process::fanout` as `pub(crate) async fn fan_out(registry, pid,
stream, bytes)` and `pub(crate) fn mark_ended(registry, pid, end,
running_now) -> Vec<SubscriberSink>` so both managers share it verbatim.

- `create(input: PtySpawnInput) -> Result<(Pid, SubscriberStream),
  PtyError>`: `session.accepts_new_streams()`; `plan_pty`;
  `ensure_directory(cwd)`; under the registry lock: `ensure_capacity`,
  `backend.open(&spec, size)`, `register(pid, ProcessKind::Pty, config,
  None, StdinMode::Pipe)`, first subscriber attached (`Started{pid}`
  head). Runtime entry `PtyRuntime{writer: SharedWriter, resizer:
  PtyResizer, control: Arc<ProcessControl>, timeout_task: Option<
  AbortHandle>}`; timeout task per D6 when `timeout` is set. Supervise
  task: pump the master in `PTY_CHUNK_BYTES` reads → `fan_out(...,
  OutputStream::Stdout, bytes)`; `tokio::select!` between the pump ending
  (EOF/EIO) and `wait()`; when `wait` finishes first (a background child
  of the shell still holds the slave), keep draining for
  `PTY_DRAIN_GRACE` then stop; close the master; `mark_ended` with
  `end_from_wait(outcome, control.reason())` → `PtyExited`. Log `pid`,
  `cols`, `rows`, `live_processes`, `duration_ms`, `status`, `exit_code`.
- `connect(pid, from_seq)`: gate; `registry.kind(pid)? == Pty` else
  `NotAPty`; `attach` → `SubscriberStream` (head `Started`, replay from
  the ring, `end` if retained-ended; the M2 semantics unchanged, incl.
  `OUT_OF_RANGE`, 30 s retention on the running clock, 8 subscribers).
- `send_input(pid, bytes)`: kind check; `write_all` + `flush` on the
  master writer; a closed master (PTY ended) → `NotFound`.
- `resize(pid, size)`: kind check; `PtySize::new`; `resizer.resize(size)`
  (`NotFound` if ended). Logged `cols`, `rows`.
- `kill(pid)`: kind check; `control.record(EndReason::Signal)`;
  `backend.signal_group(pid, SIGKILL)`; `NotFound` for an ended or unknown
  pid.
- `ProcessManager`: `connect`, `send_input`, `close_stdin` gain the
  `Process` kind check (`WrongKind` → `FAILED_PRECONDITION`);
  `send_signal` accepts **both** kinds (E2B: `commands.kill(pid)` works on
  a PTY); `list` returns both with their kind.

### D5. gRPC `PtyService` (`rayd::grpc::pty::PtyGrpc<B>`)

Replaces `PendingPtyService` (`pending.rs` is deleted; the M1 probe that
asserted `Resize → UNIMPLEMENTED` now asserts `Resize` on an unknown pid
→ `NOT_FOUND`). Conversion: `PtyStart` → `PtySpawnInput` (`size.map(|s|
(s.cols, s.rows))`, `timeout_ms > 0` → `Duration`), `ProcessEvent::
Started{pid}` → `started{pid}` (seq 0), `ProcessEvent::Output(ev)` →
`PtyServerMessage{data: ev.bytes, seq: ev.seq}`, `ProcessEvent::Ended
(end)` → `exited{exit_code, exited, status, error, signal}` (seq 0),
`keepalive` (seq 0) from `KeepAliveStream` at `StreamSettings.
keepalive_interval` (30 s), wrapped by the suspend wrapper (D7) with the
terminal `exited{exited:false, status:"suspending", exit_code:0, error:
{code:"suspending", message:"sandbox suspending; reconnect with
Connect(pid, from_seq)"}}`.

Status table (`rayd::grpc::pty::status_for`, unit-tested):

| `PtyError` | gRPC status |
|---|---|
| `InvalidSize`, `InvalidShell`, `Process(EmptyCommand \| InvalidCwd \| UnknownUser \| InvalidEnvs \| InvalidSignal)`, spawn `CannotExecute` | `INVALID_ARGUMENT` |
| `Process(RootNotAllowed)` | `PERMISSION_DENIED` |
| `Process(NotFound)` (unknown, ended for `SendInput`/`Resize`/`Kill`, expired for `Connect`) | `NOT_FOUND` |
| `NotAPty`, `Process(WrongKind)` | `FAILED_PRECONDITION` ("pid N is not a PTY" / "pid N is a PTY; use PtyService") |
| `Process(ReplayOutOfRange)` | `OUT_OF_RANGE` |
| `Process(TooManyProcesses)`, `Process(TooManySubscribers)` | `RESOURCE_EXHAUSTED` |
| `Process(NotAcceptingStreams{phase})` | `UNAVAILABLE` with the phase name (`suspending`, `terminating`) |
| `NoPtyDevices` | `FAILED_PRECONDITION` ("pty devices unavailable") |
| `Process(SpawnFailed)`, `Internal` | `INTERNAL` (message without envs/cwd) |
| `Unsupported` | `UNIMPLEMENTED` |

`x-access-token` is required on every `PtyService` RPC (the M2 layer,
unchanged). Messages never contain PTY bytes or envs.

### D6. Deadlines measure running time (Q19 decision)

Decision: **server-side deadlines exclude suspended time.** A command with
`timeout=60` paused after 10 s still has 50 s after resume, however long
the pause lasted; the same for PTY timeouts, the SIGKILL grace, execution
interrupt/restart deadlines and the 30 s retention of ended processes,
PTYs and executions. Rationale: a monotonic deadline would kill every
timed process at resume after any pause longer than its remaining budget,
which contradicts "pause/resume keeps everything alive" (`SPEC.md` §2) and
`ARCHITECTURE.md` ("todo lo que tenga plazo debe rearmarse en `/resume`").
The alternative (wall-clock semantics) is rejected as a silent behavioural
difference from what a user asked for; the client's own gRPC deadlines
stay on the client's clock and are re-issued by the reconnection contract
(D14).

Mechanism (no timer registry, no re-arm loop):

- `rayd-core::lifecycle::LifecycleState` gains `suspended_total:
  Duration`, accumulated in `resume(now)` as `now.monotonic −
  suspended_at.monotonic` (a repeated `/resume` adds nothing), plus
  `suspended_total()`. `SandboxSession` exposes `running_now() ->
  Duration` = `clock.monotonic().saturating_sub(suspended_total)` and
  `suspended_total()`. Between the `/suspend` 200 and the checkpoint (a
  few hundred ms) the running clock still advances; documented as
  negligible.
- `rayd-core::clock::Deadline { at: Duration }` (running-time instant)
  with `remaining(&self, running_now) -> Option<Duration>` (`None` when
  due) and `Deadline::after(running_now, timeout)`. `process::timeout::
  TimeoutPlan{term_at, kill_at}` keeps its shape but its fields are
  `Deadline`s built from `running_now`.
- `rayd::lifecycle::running_sleep(session: &SandboxSession, deadline:
  Deadline)`: `loop { match deadline.remaining(session.running_now()) {
  None => return, Some(d) => tokio::time::sleep(d).await } }`. After a
  resume the pending `sleep` wakes early (the monotonic clock jumped), the
  loop re-reads the running clock and sleeps the rest. Used by the
  process timeout task, the PTY timeout task and the execution recorder
  (D9) for `interrupt_at`/`restart_at`.
- The registry reaper calls `reap_expired(session.running_now())` and
  `mark_ended(..., session.running_now())` (both already take a
  `Duration`).
- Not converted: the 30 s process/PTY subscriber stall timeout (a suspend
  closes the stream anyway), `KeepAlive` intervals (harmless early fire), tonic's
  HTTP/2 keepalive (the proxy connection is dead after resume regardless),
  the sidecar relaunch backoff.
- Sidecar op timeouts (`OpTimeouts`, tokio `timeout`) stay monotonic, with
  one rule: a timeout whose call was issued at `resume_generation` `g` and
  expires when the generation is `> g` is logged as
  `op_timeout_across_resume` and **does not count** towards the
  three-consecutive-timeouts kill switch (the caller still gets
  `SidecarUnavailable`; its stream was closed by the suspend anyway).

### D7. `SuspendSignal` and the stream wrapper (`rayd::lifecycle::suspend`)

`rayd-core` has no runtime types, so the broadcast lives in `rayd`:

```rust
pub struct SuspendSignal { generation: watch::Sender<u64>, open_streams: Arc<AtomicUsize> }
impl SuspendSignal {
    pub fn broadcast(&self, suspend_generation: u64);
    pub fn subscribe(&self) -> SuspendWatch;          // receiver + open-stream guard
    pub fn open_streams(&self) -> usize;
    pub async fn wait_streams_closed(&self, grace: Duration) -> usize; // returns still-open count
}
pub enum SuspendClose<T> { Terminal(T), Status }
pub trait SuspendAware { fn on_suspend(&mut self) {} }
pub struct SuspendableStream<S, F> { inner: S, watch: SuspendWatch, close: F, closing: bool }
```

`SuspendableStream<S, F>: Stream<Item = Result<T, Status>>` where `S:
Stream<Item = Result<T, Status>> + SuspendAware` and `F: Fn() ->
SuspendClose<T>`. `poll_next` checks `watch.has_changed()` before polling
`inner`; when the generation changed it calls `inner.on_suspend()`, then
yields `Ok(terminal)` followed by `None` for `SuspendClose::Terminal`, or
`Err(Status::unavailable("suspending"))` for `SuspendClose::Status`, and
drops `inner` immediately (dropping a `SubscriberStream` detaches the
subscriber; dropping a `WatchStream` ends the pump and removes the inotify
watches; dropping a `Read` stream closes the file). The open-stream guard
decrements on drop, which is what `wait_streams_closed` observes.

Close forms, one per stream kind (this is the contract the SDK relies on):

| Stream | Close on `/suspend` | Why |
|---|---|---|
| `Process.Start`/`Connect` | in-stream `EndEvent{exited:false, status:"suspending", error:{code:"suspending"}}`, then OK end | the schema has a terminal message and the M2 SDK already parses it |
| `Pty.Create`/`Connect` | in-stream `PtyExited{…status:"suspending"…}`, then OK end | same |
| `WatchDir`, `Read` | gRPC `UNAVAILABLE` "suspending" (trailers) | no terminal message in the schema |
| `Execute`, `Reattach` | gRPC `UNAVAILABLE` "suspending" (trailers), **no `ExecutionEnd`**, `on_suspend()` marks the subscriber detached so its drop does not `interrupt` | keeps "`ExecutionError` is never terminal" true; the SDK classifies `UNAVAILABLE suspending` with the existing `is_phase_gate` |
| `Write` (client-stream) | the handler `select!`s on the watch; on suspend the write session aborts (temporary removed, destination untouched), the remaining body is drained for at most 200 ms, `UNAVAILABLE` "suspending" | ARCHITECTURE checklist step 3 |

`ARCHITECTURE.md` currently says the `Execute` close is
`ExecuteEvent.error`; this design chooses the trailing status for the
reason above and the docs task aligns the text. Wrapping order in
`grpc/*`: `SuspendableStream::new(KeepAliveStream::new(inner, …),
suspend.subscribe(), close)`; `router_with_settings` and
`hooks::router` receive the same `Arc<SuspendSignal>`.

### D8. `/suspend` hook implementation (`rayd::hooks::suspend`)

Inside `within_budget` (24 s), target < 5 s, never waits for executions
or processes, never kills anything, always 200:

1. `transition = session.suspend()` (generation++, phase `Suspending`;
   the stream gate now refuses `Start`, `Connect`, `Create`, `WatchDir`,
   `Read`, `Write`, `Execute`, `Reattach` with `UNAVAILABLE suspending`).
   `rayd-core::hooks::suspend_actions(&transition) -> SuspendActions{
   close_streams: bool}` (pure: `changed()` → `true`; a repeated
   `/suspend` → `false`).
2. If `close_streams`: `code.detach_for_suspend()` (every origin
   `Execute` subscriber is marked detached so no `interrupt` is sent; the
   recorders keep recording), then `suspend.broadcast(session.
   suspend_generation())`.
3. `streams_pending = suspend.wait_streams_closed(STREAM_CLOSE_GRACE =
   2 s)`; logged as `streams_closed` / `streams_pending`.
4. `code.quiesce(QUIESCE_TIMEOUT = 2 s)` — best effort, failure logged
   (`--no-sidecar` skips it).
5. `flush_page_cache()` (`libc::sync`, M2).
6. 200 with `HookReply{outcome: changed|unchanged, …, streams_closed:
   Some(n)}` (`HookReply` gains `streams_closed: Option<usize>` and
   `kernel_state_lost: Option<bool>`, `skip_serializing_if = None`).

`transition` errors (`Illegal`: `/suspend` before `/run`) are logged and
answered 200 `illegal` as in M2. Elapsed logged as `suspend_ms`.

### D9. Execution ring, execution registry and `Reattach` (`rayd-core::code::{ring, executions}`, `rayd::code::{executions, execute}`)

Executions become first-class and outlive their `Execute` stream.

Domain (`rayd-core`, host-tested):

- `code::ring::ExecuteRing { capacity_bytes: EXECUTE_RING_CAPACITY_BYTES
  = 4 MiB, events: VecDeque<ExecuteOutput>, oldest_seq, next_seq,
  retained_bytes }`: `push(event)` (events already carry their `seq` from
  the `ExecutionTracker`; cost = `stdout`/`stderr` text length, sum of
  the `ResultBundle` string lengths, error value + traceback lengths; 0 for
  `Started`/`End`; oldest evicted until it fits; an event larger than the
  capacity evicts everything, is **not** stored and sets `oldest_seq =
  seq + 1`), `replay_from(from_seq) -> Result<Vec<ExecuteOutput>,
  CodeError::ReplayOutOfRange{oldest, next}>` (`0` → empty; `N < oldest`
  or `N > next` → error), `oldest_seq()`, `next_seq()`.
- `code::executions::ExecutionRegistry<S: SubscriberSlot> { entries:
  BTreeMap<ExecutionId, ExecutionEntry{context_id, ring, ended:
  Option<Duration>, subscribers: Vec<(SubscriberId, S)>}>, limits:
  ExecutionLimits{max_subscribers: 8, retention: 30 s, max_retained:
  32} }`: `register(execution_id, context_id)`, `push(execution_id,
  event) -> Vec<(SubscriberId, S)>` (ring push + subscriber snapshot),
  `attach(context_id, execution_id, from_seq, sink) -> Result<Attachment{
  replay, ended: bool, subscriber: Option<SubscriberId>}, CodeError>`
  (`ExecutionNotFound` on unknown id or context mismatch; no subscriber
  for an ended execution; `TooManySubscribers` at 9), `detach`,
  `mark_ended(id, running_now)`, `reap_expired(running_now) ->
  Vec<ExecutionId>` (retention + `max_retained` oldest-first), `len`,
  `retained_count`. New `CodeError` variants: `ExecutionNotFound`,
  `InvalidExecutionId`, `ReplayOutOfRange{oldest, next}`,
  `TooManySubscribers{max}`.
- `ExecutionId::parse(&str)` (`exec-<16 hex>`).

Runtime (`rayd`):

- `code::executions::ExecutionRecorder`: one task per execution, spawned
  by `CodeManager::execute` after `open_execution`. It consumes the
  dispatcher's per-request `mpsc::Receiver<SidecarEvent>` (M4, unchanged
  on the supervisor side), runs the `ExecutionTracker` (seq numbering,
  timeout rewrite), pushes every counted `ExecuteOutput` into the ring via
  the registry and fans it out to the subscribers' `ExecuteSink`s (bounded
  channel of `EXECUTE_QUEUE_CAPACITY = 256`, `try_send` only: the recorder
  never awaits a client; a subscriber whose queue is full is detached on
  the spot and receives its own synthetic `Error{OutputTruncated}` +
  `End{0}`, nobody else is affected, and the ring lets it `Reattach` from
  `last_seq + 1`; the 30 s `stall_timeout` governs the dispatcher →
  recorder hop and the process/PTY subscribers only),
  runs the D7 timeout schedule with `running_sleep` (`interrupt_at` →
  `mark_timed_out` + `interrupt` op; `restart_at` → `restart_context`),
  drains queued events before consulting the timers (M4 R1 rule kept),
  and after `End` calls `mark_ended(running_now)` and exits. The
  dispatcher therefore never parks on a slow client for `Execute`
  (`dispatch_blocked` stays as a guard for a recorder that cannot keep
  up); the M4 behaviour "a stalled client stalls the sidecar's stdout"
  is replaced by "a stalled client is truncated at its subscriber and the
  ring keeps the last 4 MiB".
- `code::execute::ExecutionSubscriberStream { head: VecDeque<
  ExecuteOutput>, live: Option<ExecuteReceiver>, origin: bool, detached:
  Arc<AtomicBool>, interrupt: Option<InterruptHandle>, guard:
  InFlightGuard }` replaces `ExecuteStream`: yields the replay head, then
  live events, ends after `End` or the subscriber's synthetic end. `Drop`
  before `End` with `origin && !detached` → `interrupt{context_id,
  execution_id}` (M4 cancel rule); `Reattach` subscribers never
  interrupt; `on_suspend()` sets `detached`.
- `CodeManager::execute(input)` → register the execution, spawn the
  recorder, attach the origin subscriber with `from_seq = 0`.
  `CodeManager::reattach(context_id, execution_id, from_seq) -> Result<
  ExecutionSubscriberStream, CodeError>`: phase gate, id parsing,
  `attach`; an ended execution replays through `End` and closes.
  `CodeManager::detach_for_suspend()` flips `detached` on every live
  origin subscriber. `CodeManager::reap_expired(running_now)` is called
  from the process reaper tick. `CodeManager::quiesce(timeout)` wraps the
  `quiesce` op.
- `grpc/code.rs`: `Reattach` served with the 5 s `KeepAliveStream` and
  the suspend wrapper (`SuspendClose::Status`); `Execute` likewise. Status
  additions: `ExecutionNotFound` → `NOT_FOUND`, `InvalidExecutionId` →
  `INVALID_ARGUMENT`, `ReplayOutOfRange` → `OUT_OF_RANGE`,
  `TooManySubscribers` → `RESOURCE_EXHAUSTED`. Memory bound: ≤ 4 MiB per
  retained execution, ≤ 32 retained, plus 256 queued events per
  subscriber.

`RestartContext`, `DestroyContext`, sidecar death and `kernel_died` keep
delivering their synthetic pairs through the dispatcher channel, so the
recorder records them like any other event.

### D10. `/resume` hook implementation (`rayd::hooks::resume`)

Inside `within_budget` (24 s), target < 1 s, hard cap 12 s on the probe:

1. `transition = session.resume()` (generation++, `clock_offset_ms`,
   `suspended_total += Δ`, phase `Resumed`; the stream gate reopens). A
   repeated `/resume` (`unchanged`) answers 200 without re-probing.
2. `outcome = code.probe_after_resume(RESUME_PROBE_BUDGET = 12 s).await`:
   sends the `resume` op (`OpTimeouts.resume` lowered from 50 s to 15 s;
   the hook wraps the call in the 12 s budget) and decodes `contexts[
   {context_id, alive}]` through the pure `rayd-core::code::hooks::
   probe_outcome(&[(ContextId, bool)]) -> ProbeOutcome{alive: Vec, lost:
   Vec}`; `kernel_state_lost = !lost.is_empty()`, latched in
   `CodeManager` (read by `Health`, recomputed at every `/resume`; `false`
   with `--no-sidecar`; `true` when the probe timed out: state unknown).
   For every lost context: `restart_after_resume(lost, registry) ->
   Vec<SidecarOp::RestartContext{context_id, envs}>` sent in a background
   task (in-flight executions of that context end with `KernelRestarted`
   through the recorder), logged `kernel_restarted_after_resume`.
3. `code.spawn_resume_reseed()` (M4, background; it queues behind a cell
   still running in that kernel, which is why it is never awaited).
4. `tracing::info!(hook, resume_generation, clock_offset_ms, suspended_ms,
   probe_ms, kernels_alive, kernels_lost, "resume recorded")`; `warn!`
   when `clock_offset_ms.abs() > 5000`.
5. 200 with `HookReply{outcome, …, kernel_state_lost: Some(bool)}`.

Credentials: `rayd` caches none and opens no outbound connections in
M1–M5, so the checklist's step 3 is a documented no-op. The `KernelStatus`
port gains `fn kernel_state_lost(&self) -> bool`.

### D11. `Health`

`HealthGrpc` fills `kernel_state_lost` from `KernelStatus::
kernel_state_lost()`; `resume_generation` and `clock_offset_ms` come from
the session as in M2. `agent_ready` stays `phase != Terminating` (true
while `Suspending`: the VM is frozen anyway). No proto change.

### D12. SDK: the PTY surface

`clients/python/src/rayito/_models.py`: `PtySize(cols: int = 80, rows:
int = 24)` (frozen; `__post_init__` validates `1 <= cols, rows <= 4096`,
`InvalidArgumentException` otherwise) and `SandboxHealth(agent_ready,
kernel_ready, agent_version, uptime_ms, sandbox_id, resume_generation,
clock_offset_ms, kernel_state_lost)` (frozen).

`_pty_base.py` (pure, shared): `DEFAULT_PTY_TIMEOUT_SECONDS = 60.0`,
`MAX_PTY_DIMENSION = 4096`, `PtyDataCallback = Callable[[bytes], None]`,
`validate_pty_size`, `validate_shell` (absolute path or `None`),
`build_pty_start_request(size, user, cwd, envs, shell, timeout) ->
pty_pb2.PtyStart`, `build_resize_request(pid, size)`,
`build_kill_request(pid)`, `pid_from_pty_started(message)`, and
`PtyMessages`, the `StreamAdapter` (D13) for `PtyServerMessage`:
`started` → pid, `data` → `(seq, None, None, bytes)`, `exited` → end,
`keepalive` → nothing.

`sandbox_sync/pty.py`:

```python
class Pty:
    def __init__(self, sandbox: Sandbox) -> None: ...
    def create(self, *, size: PtySize | None = None, user: str | None = None,
               cwd: str | None = None, envs: Mapping[str, str] | None = None,
               shell: str | None = None, on_data: PtyDataCallback | None = None,
               timeout: float | None = DEFAULT_PTY_TIMEOUT_SECONDS,
               request_timeout: float | None = None) -> PtyHandle
    def connect(self, pid: int, *, from_seq: int = 0, on_data: PtyDataCallback | None = None,
                timeout: float | None = None, request_timeout: float | None = None) -> PtyHandle
    def send_input(self, pid: int, data: bytes | str, *, request_timeout: float | None = None) -> None
    send_stdin = send_input                      # E2B name
    def resize(self, pid: int, size: PtySize, *, request_timeout: float | None = None) -> None
    def kill(self, pid: int, *, request_timeout: float | None = None) -> bool

class PtyHandle(CommandHandle):
    # pid, last_seq, stdout (terminal bytes decoded UTF-8 with replace), stderr == "",
    # exit_code, error, wait(), kill() (PtyService.Kill), disconnect(), send_input(data)
    # (alias send_stdin), resize(size), iteration yielding (None, None, bytes),
    # on_data(bytes) invoked per data message while iterating/waiting, reconnects: int
```

`Sandbox.pty` returns the `Pty` client; `AsyncSandbox.pty` the async
mirror (`AsyncPty`, `AsyncPtyHandle`, coroutines and `async for`;
callbacks stay synchronous). Both `Create` and `Connect` open on the
**stream** channel (a terminal is long-lived); the gRPC deadline is
`stream_deadline(timeout)` (`timeout + 5 s`, none for `None`/`0`);
`connect(timeout)` is the deadline of that stream. `pty.kill` maps
`NOT_FOUND` to `False`; `send_input`/`resize` map `NOT_FOUND` to
`NotFoundException` and `FAILED_PRECONDITION` (not a PTY) to
`InvalidArgumentException`. `create(timeout=60.0)` is the E2B vocabulary
(`ARCHITECTURE.md` "commands/pty 60 s"); the docstring says how to keep a
shell alive (`timeout=None`). `__init__.py` exports `PtySize`, `PtyHandle`,
`SandboxHealth`.

### D13. SDK: handle progress generalised and handle reconnection

`_process_base.py`:

- `StreamAdapter` protocol: `pid(first) -> int`, `consume(message) ->
  Consumed` where `Consumed` is `Chunk(seq, stdout: str | None, stderr:
  str | None, pty: bytes | None)`, `Nothing`, `Ended(end)` or
  `Suspending` (an in-stream end with `status == "suspending"`).
  `ProcessEvents` (today's `ProcessEvent` logic, incl. UTF-8 incremental
  decoding through `OutputAccumulator`) and `PtyMessages` (D12) implement
  it; `PtyMessages` also decodes the bytes into `stdout` text so
  `CommandResult.stdout` holds the terminal output.
- `CommandProgress(pid, accumulator, adapter)`: `consume` returns the
  `Consumed`; on `Suspending` it sets `progress.suspended = True` without
  finishing; `outcome_from_end` keeps the M2 table (a `suspending` end
  that reaches it because reconnection is impossible still yields
  `SandboxStateException`).
- `CommandHandle._events()` (sync) / `AsyncCommandHandle` (async): on
  `Suspending`, or on a `grpc.RpcError` for which
  `sandbox._is_reconnectable(exc)` holds while not `disconnected`:
  `outcome = sandbox._reconnect(exc_or_reason, seen_generation)`; if
  `outcome.resumed`: `self._call = self._resubscribe(from_seq=self.
  last_seq + 1)` (`commands.connect` for a process, `pty.connect` for a
  PTY, reusing `on_stdout`/`on_stderr`/`on_data` and the remaining
  deadline), the leading `StartEvent`/`started` is consumed, iteration
  continues, `self.reconnects += 1`; `OUT_OF_RANGE` on the re-subscribe →
  one more attempt with `from_seq=0` and a `logger.warning` ("se perdió
  salida entre …"); `NOT_FOUND` → `NotFoundException` stored as the
  outcome; if not `outcome.resumed` → `progress.fail(outcome.error)`. A
  handle nobody reads sees the `suspending` end whenever it is next
  iterated and reconnects then (lazy; no registry of handles needed).
  `disconnect()`ed handles never reconnect.

### D14. SDK: the reconnection contract, `pause`/`resume`/`connect`

`_sandbox_base.py` (pure): `DEFAULT_RECONNECT_TIMEOUT_SECONDS = 60.0`
(the image's `resumeTimeoutInSeconds`, 30, plus 30; the SDK cannot read
the image's hook config from `get-microvm`, so it is a documented
constant overridable per sandbox), `CLOCK_OFFSET_WARN_MS = 5000`,
`ReconnectPoll(timeout, monotonic, random)`: delay `0.5 s` doubling to
`4 s` with ±25 % jitter (`random` injectable), `rpc_timeout()` as
`ReadinessPoll`, `should_check_state()` every 5 s, `timed_out()`;
`ReconnectOutcome(resumed: bool, generation_changed: bool,
resume_generation: int, error: Exception | None)`;
`reconnect_failure(reason, info) -> Exception`: `TERMINATING|TERMINATED`
→ `SandboxNotFoundException`, `SUSPENDED` without `idle.auto_resume` →
`SandboxStateException("el sandbox está suspendido; llama a resume()")`,
timeout → `SandboxException("el sandbox no volvió a responder en N s")`
chaining `reason`; `health_from_proto(response) -> SandboxHealth`.

`_transport.py`: `is_reconnectable(exc) = is_stream_reset(exc) or
is_phase_gate(exc)` (never `DEADLINE_EXCEEDED`, never the kernel gate,
never a proxy 403 — that path re-mints); `CHANNEL_OPTIONS` gains
`("grpc.initial_reconnect_backoff_ms", 500)`, `("grpc.min_reconnect_
backoff_ms", 500)`, `("grpc.max_reconnect_backoff_ms", 2000)` so a channel
whose connection died with the pause retries within seconds instead of
grpc's default 120 s ceiling.

`Sandbox` (sync; `AsyncSandbox` mirrors with `asyncio.Lock`,
`asyncio.sleep`, `asyncio.to_thread` for boto3):

- State: `_resume_generation: int` (from the first `Health` in
  `_wait_until_ready`), `_reconnect_lock`, `_reconnect_timeout` (new
  kwarg `reconnect_timeout: float = DEFAULT_RECONNECT_TIMEOUT_SECONDS` on
  `create()` and `connect()`). Every `Health` consumer goes through
  `_record_health(response)`, which updates the generation, logs
  `kernel_state_lost` and warns when `abs(clock_offset_ms) > 5000`.
- `_reconnect(reason: Exception, seen_generation: int, *, wake: bool)
  -> ReconnectOutcome`: if `_resume_generation > seen_generation` someone
  already reconnected → `resumed=True, generation_changed=True`
  immediately; if `_closed` → `SandboxException`. With `wake=True`, under
  the lock (one `Health` poll per sandbox; later wake callers block on it
  and then hit the generation check): poll `Health` with
  `ReconnectPoll(self._reconnect_timeout)` through
  `_probe_health(poll.rpc_timeout())` (`None` on
  `UNAVAILABLE`/`DEADLINE_EXCEEDED`, i.e. 502/503/timeouts) until
  `agent_ready and kernel_ready`; every 5 s `get_microvm` and stop early
  on a terminal state or on `SUSPENDED` without auto-resume; on success
  `_record_health`, `generation_changed = response.resume_generation !=
  seen_generation`, `logger.info("reconectado …")`. With
  `auto_resume=True` the poll itself is the request that auto-resumes the
  VM (Q4), which is the whole auto-resume story for idle suspensions.
  With `wake=False` the caller is *dormant*: see the amendment below.
- **Amendment (M5 review, 2026-09-16) — who may wake a suspended VM.**
  Any `Health` probe against a `SUSPENDING|SUSPENDED` VM with
  `auto_resume=True` is the request that resumes it (Q4). If every
  reconnect probed `Health`, an explicit `pause()` would be undone within
  a second by whatever was in flight — the `WatchHandle` consumer thread
  is *always* iterating, so `watch_dir` alone would make `pause()` a
  no-op — and an idle suspension would be undone by the same threads,
  turning the idle policy into a suspend/resume loop that costs ≈ 5 min of
  compute per cycle (`SPEC.md` §7). So `wake` is decided per caller:
  - `wake=True`: a **new request** — a unary retry, the first message of a
    stream the caller is opening (`_open_stream`) — and an **in-flight
    foreground stream** (`commands.run` foreground, `run_code`) *unless
    this `Sandbox` has a pending `pause()`* (`_paused`, set before
    `suspend_microvm` is called so the in-stream `suspending` end can never
    overtake it, cleared by `resume()` and by any `_record_health` that
    sees a new generation, kept untouched when `pause()` returns `False`).
    These callers probe `Health` exactly as above: the poll wakes an
    auto-resume VM, stops on `SUSPENDED` without auto-resume
    (`SandboxStateException("… llama a resume()")`) and on the
    `reconnect_timeout` budget. A new request on a paused VM wakes it
    because AWS itself would (a request that arrives after `SUSPENDED` is
    retained and auto-resumed by the proxy without any SDK involvement);
    refusing would leave `pause(wait=False); files.write(...)` deadlocked.
  - `wake=False`: a **background handle, a PTY, a watch**, and an in-flight
    foreground stream cut by *this* `Sandbox`'s `pause()`. The caller
    sleeps **outside the lock** (so it never blocks a `wake=True` caller):
    after `ReconnectPoll.INITIAL_DELAY` (0.5 s, so a `RUNNING` read that
    is stale right after the `suspending` end is not trusted) it reads
    `get-microvm` every `STATE_CHECK_INTERVAL` (5 s) and, while the state
    is `SUSPENDING|SUSPENDED`, touches neither `Health` nor the
    `reconnect_timeout` budget, whatever the idle policy (`SUSPENDED`
    without auto-resume is not a failure for it: it waits for the
    `resume()` like the frozen process it follows). It wakes early when
    `_record_health` registers a new generation (a `threading.Condition`
    / an `asyncio.Event`; `resume()` in the same process resumes every
    dormant handle at once) or when `close()` runs. Once the state leaves
    the suspended pair it takes the lock and runs the `Health` poll with a
    **fresh** `reconnect_timeout` budget, re-checking the state after every
    failed probe and going back to sleep if the VM is suspended again. A
    terminal state ends it with `SandboxNotFoundException`; a `timeout`
    on the handle still applies through the client-side stream deadline
    at re-subscribe time (`remaining_deadline`). Consequence, documented in
    `README.md` and the handle docstrings: reading a handle never wakes a
    sandbox, so `wait()` on a handle of an idle-suspended VM blocks until
    someone resumes it (`resume()`, any foreground call with auto-resume,
    or the VM's own `suspendedDurationSeconds`/`timeout` termination).
  - Pauses issued from another process or through the classmethod
    `Sandbox.pause(sandbox_id)` are invisible to a `Sandbox` instance: its
    in-flight foreground streams keep `wake=True` and would undo them.
    `reconnect_failure(reason, info=…, wake=…)` carries the rule in
    `_sandbox_base.py`.
- `_call_unary(call)`: after the existing one-shot 403 re-mint, a
  reconnectable `RpcError` → `_reconnect` → on `resumed` retry **once**;
  otherwise raise `outcome.error`. Documented caveat: a `send_stdin`/
  `send_input` cut after `rayd` applied it may be applied twice (as in
  E2B). `_open_stream` applies the same rule before the first message.
- `get_health(request_timeout=None) -> SandboxHealth` (public; `Health`
  on the unary channel; the e2e reads `resume_generation` from it).
- `pause(wait=True) -> bool`: reads `get-microvm` first and returns
  `False` without calling the API when the state is already
  `SUSPENDING|SUSPENDED` (acceptance amendment, 2026-09-16: AWS's
  `suspend-microvm` is idempotent and answers 200 on a `SUSPENDED` VM, so
  the `ConflictException` path never fires there; it is kept for any other
  conflict), otherwise `suspend_microvm` through the 2 TPS bucket and, with
  `wait`, polls `get-microvm` until `SUSPENDED`. Handles need nothing eager.
- `resume(wait=True) -> None`: `resume_microvm` (a `False`/Conflict — the
  VM is already `RUNNING` or auto-resumed — continues), `refresh_all()`
  (re-mint; the old JWE is valid but a fresh one costs nothing and covers
  a pause longer than 45 min when the refresher could not reach AWS),
  `_wait_until_ready` → `_record_health`. Handles reconnect lazily (D13).
- `connect()`: `SUSPENDED` without auto-resume → explicit
  `resume_microvm` (M2); with auto-resume the readiness poll resumes it.
- `WatchHandle` (`sandbox_sync/filesystem.py`, async mirror): the
  consumer thread, on a reconnectable `RpcError` while not stopped, calls
  `_reconnect`; on `resumed` re-issues `WatchDir` with the same `path`,
  `recursive`, `include_entry`, `user` and the remaining deadline, waits
  for `WatchStarted`, keeps the same `WatchState` and continues; `on_exit`
  is not called and `is_running` stays `True`; events raised while
  suspended are lost (documented); on failure the M3 end path runs
  (`record_end(exc)`, `on_exit(exc)`).
- `run_code` (`sandbox_sync/code.py`, async mirror): `ExecutionBuilder`
  records `context_id` (the request's or `"default"`), `execution_id`
  (from `started`) and `last_seq`. `_feed_events`, on a reconnectable
  `RpcError` (including the trailing `UNAVAILABLE suspending`) after
  `started`: `_reconnect` → on `resumed` open `Reattach(context_id,
  execution_id, from_seq=last_seq + 1)` on the unary channel with the
  remaining deadline and keep feeding the same builder (`builder.
  reattached += 1`); `OUT_OF_RANGE` → `SandboxException("se perdió salida
  de la ejecución …")`; `NOT_FOUND` → `SandboxException` (ended more than
  30 s of running time ago); before `started` → no retry, the failure
  propagates (`SandboxStateException` for the phase gate) so a cell never
  runs twice. `_code_base.build_reattach_request` added.
- Idle policy: defaults unchanged (`IdlePolicy(max_idle_seconds=300,
  suspended_duration_seconds=None → timeout − max_idle, auto_resume=
  True)`; minimum 60 s validated); the `IdlePolicy` docstring states that
  a suspend/resume cycle costs ≈ 5 min of compute at 2 GB, so
  `max_idle_seconds < 300` does not save money (`SPEC.md` §7).
- The JWE refresher keeps running during a pause (M2 behaviour kept;
  unit test asserts the thread is alive across `pause()`).
- `close()` during a reconnect makes the poll return `SandboxException`.

### D15. SDK error mapping additions

| Origin | Exception |
|---|---|
| `PtyService` `NOT_FOUND` | `NotFoundException` (`pty.kill` → `False`) |
| `FAILED_PRECONDITION` ("not a PTY" / "is a PTY") | `InvalidArgumentException` (M2 rule) |
| `OUT_OF_RANGE` on `Connect`/`Reattach` | handled internally by the re-subscribe fallback; `NotFoundException` when raised by a user `connect(from_seq=N)` |
| `EndEvent`/`PtyExited` `status == "suspending"` | reconnect (D13); `SandboxStateException` only when reconnection is impossible |
| `UNAVAILABLE` "suspending" on a stream or unary | reconnect (D14); `SandboxStateException` when impossible |
| `UNAVAILABLE`/RST/GOAWAY/EOF/502 on a stream or unary | reconnect; then `SandboxNotFoundException` (terminal state), `SandboxStateException` (suspended without auto-resume) or `SandboxException` (deadline) |
| `Reattach` `NOT_FOUND` / `OUT_OF_RANGE` | `SandboxException` with the reattach message |
| `ConflictException` on `suspend_microvm` | `pause()` returns `False` (M2) |

The M2 "probe `Health` 5 s then `get_microvm`" classification
(`stream_failure_exception`) remains only as the fallback when
`_reconnect` fails; `translate_rpc_error` is unchanged.

### D16. Tests

**`rayd-core` (host, Windows and Linux)** — `cargo test -p rayd-core`:
`PtySize` (defaults, bounds, 4096 accepted, 4097 refused, 0 refused);
`resolve_shell` (identity shell, empty → `/bin/sh`, relative request →
`InvalidShell`, absolute request kept); `pty_base_env` keys and the
override order in `plan_pty` (identity → PTY defaults → payload → request;
`SHELL` present; `TERM` overridable by the request); `plan_pty` argv
`["-i", "-l"]`, `StdinMode::Pipe`, registry `config` (cmd = shell, cwd
filled), root refused, unknown user; `ProcessRegistry::kind` and
`WrongKind`; `LifecycleState` `suspended_total` accumulates across cycles
and not on a repeated `/resume`; `SandboxSession::running_now` (fake
clock: advance, suspend, advance 300, resume → running advanced by the
non-suspended part only); `Deadline::remaining` before/at/after and after
a simulated pause; `TimeoutPlan` on the running clock; `ExecuteRing`
(push/replay, `from_seq 0`, eviction to capacity, below-oldest and
above-next → `ReplayOutOfRange`, oversized event evicts all and is not
stored, result cost counts every mime string); `ExecutionRegistry`
(register/attach/detach, 9th subscriber refused, context mismatch →
`ExecutionNotFound`, ended execution replays with `ended: true` and no
subscriber, retention 30 s on the running clock, 33rd retained evicts the
oldest, `reap_expired`); `ExecutionId::parse`; `suspend_actions`
(changed → close, unchanged → no close); `probe_outcome` and
`restart_after_resume` (lost contexts with their envs, none when all
alive); `CodeError`/`PtyError` `Display` never quotes envs, cwd, code or
bytes.

**`rayd` unit (host)**: `SuspendableStream` over an in-memory stream with
a paused tokio clock: terminal close (`Terminal` item then `None`), status
close (`Err(UNAVAILABLE suspending)`), `on_suspend` called before the
inner drop, open-stream counter down on drop, a generation change before
the first poll closes immediately, no change → transparent;
`running_sleep` wakes early on a monotonic jump and re-sleeps the
remainder (fake clock); `ExecutionRecorder` over the M4 in-memory
`fake_sidecar` (`code/fake_sidecar.rs`): ring + two subscribers, stalled
subscriber truncated alone, origin drop → `interrupt`, detached origin
drop → no `interrupt`, `Reattach` subscriber drop → no `interrupt`,
timeout schedule fires on the running clock and is delayed by a
simulated pause, ended execution retained then reaped;
`grpc::pty::status_for` table; `hooks` `HookReply` serialisation with the
optional fields.

**`rayd` integration, `cfg(unix)`** — `crates/rayd/tests/m5_pty.rs`
(in-process routers, `/run` installed, `IdentitySwitch::KeepCurrent` when
not root, `--no-sidecar`): `Create` → `started{pid}` first, `seq` 0;
`SendInput("echo hola\n")` → `data` containing `hola` with `seq >= 1`
within 2 s; `stty size` → `24 80`; `Resize(30, 100)` then `stty size` →
`30 100`; `tty` → `/dev/pts/`; `id -u` → the current uid; `echo $TERM
$LANG $LC_ALL $SHELL` → `xterm-256color C.UTF-8 C.UTF-8 <shell>`; env
override `TERM=vt100` in `PtyStart.envs` wins; `List` shows the pid with
`PROCESS_KIND_PTY` and the shell as `cmd`; `ProcessService.Connect`/
`SendInput`/`CloseStdin` on the PTY pid → `FAILED_PRECONDITION`;
`PtyService.Connect`/`SendInput`/`Resize`/`Kill` on a plain process pid →
`FAILED_PRECONDITION`; `ProcessService.SendSignal(pty, 9)` works;
`head -c 100000 /dev/zero | tr '\0' x` → every `data` ≤ 16 KiB and the
total is 100 000 `x` plus terminal echo; `Connect(pid, from_seq=1)`
replays everything then follows; `Connect(from_seq=next+1)` →
`OUT_OF_RANGE`; second subscriber truncated at `stall_timeout` 1 s
while the first keeps receiving; `Kill` → `exited{status:"signaled",
exit_code:137, signal:9}`; `Kill` again → `NOT_FOUND`; `exit 3` typed in
the shell → `exited{status:"exited", exit_code:3}`, `Connect` within the
retention replays `started`, ring, `exited`; `timeout_ms=1500` on an idle
shell → `exited{status:"timeout"}` with `error.code == "deadline_exceeded"`
at ≈ 1.5 s and SIGTERM first (`trap` in the shell prints `TERM`); size
`0×0`/`5000×24` → `INVALID_ARGUMENT`; relative `shell` →
`INVALID_ARGUMENT`; `shell=/bin/false` → `exited{exit_code:1}`; 256 live
entries (processes + PTYs) → `RESOURCE_EXHAUSTED`; `Create` after
`/suspend` → `UNAVAILABLE suspending`; `KeepAlive` at 200 ms on a silent
shell; a `sleep 30 &` typed in the shell then `exit` → the PTY ends
within `PTY_DRAIN_GRACE + 1 s` even though the child holds the slave.

`crates/rayd/tests/m5_suspend_resume.rs` (fake sidecar from M4 with the
`resume` reply scripted through a `--resume-lost <ctx>` flag, real
processes and PTYs): with a `Start` stream on `sleep 30`, a `Connect` on
it, a PTY stream, a `WatchDir` stream, a `Read` of a 4 MiB file half
consumed, an `Execute` of `sleep 3`, a `Reattach` on it and a `Write`
mid-body open, `POST /suspend` answers 200 within 2 s with `outcome:
changed`, `streams_closed == 8`, and: the process streams end with
`EndEvent{exited:false, status:"suspending", error.code:"suspending"}`
then OK; the PTY stream with `PtyExited{status:"suspending"}`; `WatchDir`,
`Read`, `Execute`, `Reattach`, `Write` end with `UNAVAILABLE` and details
`suspending`; the fake sidecar received `quiesce` and **no** `interrupt`;
the `Write` temporary is gone and the destination absent; `sleep 30` and
the shell are still alive (`List`); `Start`/`Create`/`WatchDir`/`Execute`
→ `UNAVAILABLE suspending`; a second `POST /suspend` → `unchanged`,
`streams_closed == 0`; `POST /resume` → 200 `changed`,
`resume_generation == 1`, `kernel_state_lost == false`, the fake received
`resume` then `reseed`; `Health` reports `resume_generation 1`,
`clock_offset_ms` within ±100 (no real pause); `Connect(pid, from_seq =
last+1)` replays without gap; `Pty.Connect` likewise and `echo back`
answers; `Reattach(ctx, exec, from_seq=last+1)` replays and delivers the
`end` of `sleep 3`; `Reattach` on a finished execution older than the
retention → `NOT_FOUND`; a stalled `Execute` client gets `OutputTruncated`
while a `Reattach` subscriber of the same execution receives everything;
timeout re-arm: `Start` `sleep 10` with `timeout_ms=1500`, `/suspend`
at 0.5 s, the test **sleeps 2 s** with the session in `Suspending` (the
phase machine is what the running clock reads; no VM freeze in-process),
`/resume`, the process is still alive at resume and ends with `timeout`
≈ 1 s after it; the same for an `Execute` `sleep 5` with `timeout_ms=
1500` (interrupt reaches the fake ≈ 1 s after resume, not at resume);
`--resume-lost ctx-a` → the fake receives `restart_context{ctx-a}`,
`Health.kernel_state_lost == true`, an in-flight execution on `ctx-a`
ended with `KernelRestarted`, a later `/resume` with every kernel alive
clears it; a `resume` op delayed beyond 12 s (fake `--resume-delay-ms
13000`) → `/resume` still 200 within 13 s, `kernel_state_lost == true`,
`probe_timeout` logged; a `CreateContext` op in flight across a
`/suspend`+`/resume` that times out is not counted (`sidecar_restarts`
stays 0 after three such timeouts); `/suspend` before `/run` → 200
`illegal`; `/suspend` and `/resume` under the budget log `suspend_ms` /
`probe_ms`. `m1_hello.rs`, `m2_process.rs`, `m3_filesystem.rs`,
`m4_code.rs` adapt to the shared registry, `SuspendSignal` and the new
router signatures; the M1 `PtyService.Resize → UNIMPLEMENTED` probe
becomes `NOT_FOUND`; the M4 `Reattach → UNIMPLEMENTED` case becomes
`NOT_FOUND` for an unknown id; the M4 `big 2000` stall case now asserts
the truncation at the subscriber with the sidecar never parked.

**Python SDK unit** — fakes: `tests/unit/fake_pty.py` `FakePtyService`
(`Create` → `started` + an echo shell: every `SendInput` is echoed back
as `data` with `\r\n`, `echo <x>` answers `<x>\r\n`, `stty size` answers
the current size, `exit <n>` ends the PTY; sizes recorded; `Resize`
recorded and answered by the next `stty size`; `Kill` → `exited{signaled
137}`; `Connect(from_seq)` replays from a per-pid ring with `seq`, `OUT_OF_
RANGE` beyond it; every RPC checks `x-access-token`; `suspend()` ends live
streams with `exited{suspending}`; timeouts recorded). `FakeProcessService`
gains `suspend()` (ends live streams with `EndEvent{suspending}`),
records `connect_calls` `(pid, from_seq)`; `FakeFilesystemService.WatchDir`
gains `suspend()` (aborts with `UNAVAILABLE suspending`) and
`watch_calls`; `FakeCodeService` gains an execution ring, `Reattach`
(`reattach_calls`, replay by `from_seq`, `NOT_FOUND`/`OUT_OF_RANGE`), a
scripted `slow <s>` cell that ends after `s` seconds, and `suspend()`
(aborts `Execute` with `UNAVAILABLE suspending`, keeps the cell running);
`FakeRayd.suspend_resume(unavailable_calls=N)` orchestrates all four and
makes `Health` answer `UNAVAILABLE` `N` times before returning
`resume_generation + 1` with `clock_offset_ms` and `kernel_state_lost`
scripted. Tests (`test_pty_base.py`, `test_pty_sync.py`, `test_pty_async
.py`, `test_reconnect_sync.py`, `test_reconnect_async.py`, additions to
`test_sandbox_*.py`, `test_process_base.py`, `test_code_*.py`,
`test_files_*.py`, `test_transport.py`): `PtySize` validation;
`build_pty_start_request` (size omitted → no `size` field, envs, user,
cwd, shell, `timeout_ms`); `PtyMessages` adapter (`started`, `data` with
`seq`, `exited`, `keepalive`, `suspending`); `pty.create` → `PtyHandle`
with `pid`, `send_input("echo hola\n")` then iteration yields `(None,
None, bytes)` containing `hola`, `on_data` called with bytes, `stdout`
decoded, `resize` recorded, `stty size` reflects it, `kill` → `True` then
`wait()` → `CommandExitException(137)`, `kill` again → `False`, `connect
(from_seq=last+1)` replays, `send_input` on an unknown pid →
`NotFoundException`, `send_input` on a process pid → `InvalidArgument
Exception` (fake answers `FAILED_PRECONDITION`), streams on the stream
channel, `timeout=None` → `timeout_ms 0` and no deadline, `send_stdin`
alias, async parity; reconnection: `ReconnectPoll` schedule with a fixed
`random` (0.5, 1, 2, 4, 4 within ±25 %), deadline, `should_check_state`;
a background `CommandHandle` iterated across `fake_rayd.suspend_resume(3)`
receives the rest of the output, `reconnects == 1`, the fake saw
`Connect(pid, from_seq == last_seq + 1)`; a handle never read before the
suspend reconnects on its first `wait()`; `OUT_OF_RANGE` → second
`Connect(from_seq=0)` and a warning; `PtyHandle` reconnects via
`Pty.Connect`; `WatchHandle` re-issues `WatchDir` (`watch_calls == 2`),
`is_running` stays `True`, events after the resume arrive, `on_exit` not
called; `run_code("slow 2")` across a suspend after `started` →
`Reattach(ctx, exec_id, from_seq == last_seq + 1)` and the `Execution`
completes; before `started` → `SandboxStateException` and no second
`Execute`; a unary (`commands.kill`) cut with `UNAVAILABLE suspending` →
one reconnect, one retry, success; `get_microvm` `TERMINATED` during the
poll → `SandboxNotFoundException` (Stubber); `SUSPENDED` without
auto-resume → `SandboxStateException`; deadline exhausted →
`SandboxException` and `Health` polled ≥ 5 times; two threads reconnect
once (lock + generation dedup); `resume()` re-mints (Stubber expects
`create_microvm_auth_token`), records the generation and warns on
`clock_offset_ms 6000` (caplog); `pause()` `False` on `ConflictException`;
the refresher thread is alive across `pause()`; `get_health()` fields;
`CHANNEL_OPTIONS` contain the three reconnect keys; `is_reconnectable`
truth table; `disconnect()`ed handles do not reconnect; async parity for
every reconnect case. `uv run pytest tests/unit`, `ruff check`, `ruff
format --check`, `mypy src` all clean.

**e2e (real AWS)** — `clients/python/tests/e2e/test_m5_pty_suspend_resume
.py`; see "Acceptance test list".

### D17. Image, scripts and dev loop

Before writing the adapter (task 0.1) the implementer measures, on the
current 8.0 image through the M4 SDK, whether the guest has pty devices:
`commands.run("test -c /dev/ptmx && grep -c devpts /proc/mounts")` and
`commands.run("python3 -c 'import pty, os; m, s = pty.openpty(); print
(os.ttyname(s))'")`; the result is recorded as `AWS_API_NOTES.md` §16
Q37. If devpts is absent, the milestone **stops** and an ADR is proposed
(mounting it needs `CAP_SYS_ADMIN`, i.e. `additionalOsCapabilities`, an
M6 decision); nothing in this design assumes a workaround.

`image/Dockerfile` (M5 header): no new packages (`bash`, `coreutils`
`stty`/`tty`, `util-linux` already present); the sanity list adds `tty`,
`stty` and `test -c /dev/ptmx` (informative at build time; the runtime
check is e2e block 1); `CMD` unchanged; hooks unchanged in
`scripts/publish_image.py` (`suspend`/`resume` 30 s, `IMAGE_HOOKS`).
Publish as the next `rayito-base` version with the three-state gate;
record zip size, build seconds and `snapshotBuild` sizes next to 8.0's
(925 466 624 / 1 289 846 784 / 36 929 536 B, 195.9 s).

`scripts/hooks-sim.py`: `--only suspend,resume` opens a `Start` stream on
`sleep 60` and a PTY before `/suspend`, asserts the `suspending` ends,
posts `/resume`, asserts `resume_generation` advanced in `Health` and that
`Connect(pid, from_seq)` / `Pty.Connect` replay; prints
`streams_closed`, `kernel_state_lost`. `Makefile` targets unchanged;
`dev-run` keeps working under WSL2/Docker (a PTY needs `/dev/pts`, which
containers have).

### D18. Performance budgets

| Budget | Value | Where |
|---|---|---|
| `/suspend` handler | logged `suspend_ms`; ≤ 5 s design target, 24 s hard | rayd log |
| `/resume` handler | logged `probe_ms`; probe cap 12 s hard, typical < 100 ms (`kernel_info` 1.9 ms measured) | rayd log |
| `pause()` → `SUSPENDED` | asserted ≤ 30 s, logged `pause_s` (1.4 s measured in M0) | e2e |
| `resume()` → `Health` with the new generation | asserted ≤ 30 s, logged `resume_s` (1.2 s measured) | e2e |
| handle re-subscribe after `resume()` (`Connect(from_seq)` first message) | asserted ≤ 10 s, logged | e2e |
| PTY `create` → `started` | asserted ≤ 5 s (first `bash -l` of a VM ≈ 1 s) | e2e |
| PTY `echo hola` round trip | asserted ≤ 5 s, logged (expected ≈ 0.2 s at 93 ms RTT) | e2e |
| auto-resume: first command after `SUSPENDED` | asserted ≤ 30 s, logged `auto_resume_s` (1.1–1.6 s measured for HTTP) | e2e |
| timeout re-arm: `sleep 60` with `timeout=25` paused for 30 s | alive at resume; `TimeoutException` ≤ 30 s after resume | e2e |
| `run_code` across a pause (`Reattach`) | completes ≤ 60 s after resume | e2e |
| execution ring | ≤ 4 MiB per execution, ≤ 32 retained | by construction |
| PTY data message | ≤ 16 KiB | integration |
| client stream close observed at `pause()` | logged: time from `pause()` to the handle's `suspending` end (Q38) | e2e |

### D19. Logging allowlist and secrecy

`logging.rs` doc gains: `cols`, `rows`, `pty_devices`,
`suspend_generation`, `suspended_ms`, `streams_closed`, `streams_pending`,
`suspend_ms`, `probe_ms`, `kernels_alive`, `kernels_lost`,
`kernel_state_lost`, `reattach`, `subscribers`, `replayed`, `from_seq`,
`op_timeout_across_resume`. Never: PTY bytes (input or output), the
shell's argv beyond the program path, envs, `cwd`, execution output. The
SDK never logs PTY bytes either (`on_data` is the user's).

### D20. Docs alignment

`ARCHITECTURE.md`: `PtyService` row (openpty + setsid/TIOCSCTTY, slave
chowned, 16 KiB chunks, shared registry, `seq`, `-i -l`, env), "Suspend /
resume" section rewritten to D6–D10 (running clock instead of "rearma",
the close-form table, `Execute` closes with a trailing status, probe cap
12 s, `kernel_state_lost` latch, reseed in background), reconnection
contract paragraph (lazy per-handle re-subscribe, `Reattach`, unary
retry, `reconnect_timeout`), domain table rows `pty`, `code` (ring,
executions), `hooks`; ports table (`PtyBackend`, `KernelStatus.
kernel_state_lost`); adapters list (`NixPtyBackend`, `SuspendSignal`);
Python layout (`_pty_base.py`, `sandbox_sync/pty.py`). `MILESTONES.md`
M5: snippet aligned with the e2e and the "Estado de aceptación"
paragraph with every D18 number. `SECURITY.md`: T2 updated (`/suspend`
now closes streams but still destroys nothing; a forged `/resume` only
bumps a counter), new row T13 "PTY: slave chowned to uid 1000, shell as
the user with `setsid`+`TIOCSCTTY`, no root shell without
`RAYITO_ALLOW_ROOT`, `Kill` is `SIGKILL` to the group, bytes never
logged". `AWS_API_NOTES.md` §16: Q37 (devpts in the guest), Q38 (what a
client sees at an explicit `pause()` with streams open and how fast),
Q39 (`resume-microvm` → `Health` with the new generation, `Connect(
from_seq)`/`Pty.Connect`/`Reattach` after resume, kernel alive), Q40
(auto-resume through gRPC via the SDK), Q41 (timeout re-arm measured),
plus the §15 clock paragraph pointing at the running-clock decision.
`README.md`: `pty` and `pause/resume` examples marked available, the
`reconnect_timeout` knob, the 0.0.5 ↔ M5-image requirement.

## Risks / Trade-offs

- [devpts absent in the guest] `openpty` would fail on real AWS →
  Mitigation: task 0.1 measures it on the current image before any
  adapter code; if absent the milestone stops with an ADR (`CAP_SYS_
  ADMIN` is an M6 `additionalOsCapabilities` decision).
- [Stream close racing the checkpoint] The in-stream `suspending` end may
  not reach the client before the VM freezes → Mitigation: the SDK treats
  a reset/EOF exactly like the clean end (both trigger the same
  reconnect); the e2e logs how fast the clean end arrives (Q38).
- [Running-clock semantics surprise] A user expecting wall-clock
  timeouts sees a paused command outlive its `timeout` → Mitigation:
  documented in `ARCHITECTURE.md`, the SDK docstrings and `README.md`;
  the e2e asserts the behaviour explicitly.
- [Lost backpressure to the sidecar for `Execute`] The recorder always
  drains, so a slow client no longer slows the kernel → Mitigation: the
  ring is bounded (4 MiB), the subscriber channel is bounded (256) and
  truncation is per subscriber; documented as a deliberate M5 change of
  the M4 rule.
- [Unary retry after reconnect is not idempotency-aware] `send_input`
  could be applied twice if the cut happened after `rayd` acted →
  Mitigation: documented; the retry only happens after a *successful*
  reconnect with the sandbox alive, the same trade-off E2B makes.
- [`setsid` vs `process_group`] Forgetting to drop `process_group(0)`
  makes every PTY spawn fail with `EPERM` → Mitigation: stated in D3 and
  asserted by the `tty`/`stty` integration cases.
- [Slave ownership] Without `fchown` programs that reopen `/dev/tty`
  fail for the user → Mitigation: D3 step 2 and the `tty` case.
- [Probe blocked by a running cell] `kernel_info` goes over the control
  channel, which ipykernel serves on its own thread, so a busy kernel
  still answers → Mitigation: 5 s per kernel, 12 s cap, `kernel_state_
  lost = true` on cap rather than a late 200.
- [Reseed queued behind a long cell] → Mitigation: never awaited by the
  hook (M4 behaviour kept).
- [Sidecar op timeouts firing at resume] → Mitigation: the D6 rule keeps
  them out of the kill switch; the streams involved were closed anyway.
- [grpc channel backoff after the connection dies] → Mitigation: the
  three reconnect-backoff channel options cap it at 2 s; the reconnect
  poll tolerates `UNAVAILABLE`.
- [Auto-resume through gRPC unmeasured] AWS retains the first request
  for HTTP (Q4); whether a gRPC unary/stream is retained the same way is
  measured by the auto-resume e2e; either outcome (retained, or one
  reconnect) passes.
- [Many suspend/resume cycles in one boot] `suspended_total` and the
  generations are `u64`/`Duration`; retention on the running clock never
  goes backwards.

## Migration Plan

1. Merge; CI green (`buf lint`, `buf breaking` against the previous
   proto, `cargo fmt/clippy/test` on host and in the Linux container
   incl. `m5_pty` and `m5_suspend_resume`, sidecar job unchanged, `pytest
   tests/unit`, `ruff`, `mypy`, ARM64 build).
2. `make image-zip` + `image-publish` → new `rayito-base` version;
   three-state gate; sizes recorded.
3. `RAYITO_E2E=1 RAYITO_TEMPLATE=<arn> make test-e2e` → M1–M4 suites plus
   `test_m5_pty_suspend_resume.py` green.
4. Rollback: `update-microvm-image-version --status INACTIVE` on the new
   version. The 0.0.5 SDK works against an M4 image for everything but
   `.pty` (`UNIMPLEMENTED` → `InvalidArgumentException`) and reconnection
   after a pause (the M4 `rayd` never closes streams, so handles see a
   reset and reconnect anyway); pin 0.0.4 to keep the M4 behaviour.
5. Acceptance agent archives the change.

## Open Questions

None blocking. To measure during the e2e/publish and record in
`AWS_API_NOTES.md` §16 and `MILESTONES.md`: devpts presence (Q37, before
implementation); the delay between `pause()` and a client handle seeing
its `suspending` end (Q38); `resume()` → `Health` with the new generation
and the re-subscribe latency of `Connect(from_seq)`, `Pty.Connect` and
`Reattach` (Q39); whether a gRPC call is retained by the proxy during an
auto-resume or fails once, and — D14 amendment — that a `pause()` with a
`run_code`, a live handle, a PTY and a watch in flight reaches and stays
`SUSPENDED` (block 6 and block 13 assert it; the acceptance agent records
the `[m5] Q40 …` lines and whether the auto-resume path or the `Reattach`
path ran) (Q40); the timeout re-arm and the `clock_offset_
ms` value after a 30 s pause (Q41); the snapshot sizes and build time of
the M5 image.

## Acceptance test list

`clients/python/tests/e2e/test_m5_pty_suspend_resume.py`, marker `e2e`,
two test functions. Helpers: `read_until(handle, needle: bytes, timeout)`
iterates a `PtyHandle` accumulating the `pty` bytes until `needle` appears
(returns the buffer; fails after `timeout`); `collect_in_thread(handle)`
iterates a `CommandHandle` in a daemon thread appending stdout chunks to
a list and recording any exception; `wait_for_terminal_state` and the
timing helpers from M1/M2; `m5_sandbox` fixture = `Sandbox.create(
template_arn, timeout=1800, idle=IdlePolicy(max_idle_seconds=600,
auto_resume=True), execution_role_arn=…, ingress=["ALL_INGRESS"],
logging=…, control_plane=…)` killed in teardown, printing
`kernel_ready_s`.

`test_pty_suspend_resume(m5_sandbox)` — numbered blocks:

1. Platform: `sbx.commands.run("test -c /dev/ptmx && grep -c devpts /proc/mounts").stdout.strip() != "0"`; `sbx.commands.run("python3 -c 'import pty, os; m, s = pty.openpty(); print(os.ttyname(s))'").stdout.startswith("/dev/pts/")` (Q37 recorded).
2. CSV + code + plot (`SPEC.md` §6 step 3): `sbx.files.write("/home/user/data.csv", "a,b\n1,3.5\n2,4.5\n")`; `r = sbx.run_code("import pandas as pd; df = pd.read_csv('/home/user/data.csv'); df.describe()")`: `"a" in r.text`; `r = sbx.run_code("df.plot(); import matplotlib.pyplot as plt; plt.show()")`: `base64.b64decode(r.results[0].png)[:8] == b"\x89PNG\r\n\x1a\n"`; `r = sbx.run_code("print(len(df))")`: `"2" in "".join(r.logs.stdout)` and `r.text is None`.
3. State before the pause: `sbx.run_code("x = 42")`; `sbx.run_code("x").text == "42"`; `gen0 = sbx.get_health().resume_generation`.
4. PTY (`SPEC.md` §6 step 4), timed: `chunks = []`; `t = perf_counter()`; `pty = sbx.pty.create(size=PtySize(cols=100, rows=30), on_data=chunks.append, timeout=None)`: `pty.pid > 0`, elapsed ≤ 5 s; `sbx.pty.send_input(pty.pid, "echo hola\n")`; `buf = read_until(pty, b"hola\r\n", 10)` within 5 s (logged); `chunks and all(isinstance(c, bytes) for c in chunks)`; `any(p.pid == pty.pid and p.kind == "pty" for p in sbx.commands.list())`; `sbx.pty.send_input(pty.pid, "stty size\n")` → `b"30 100"` in `read_until(...)`; `sbx.pty.resize(pty.pid, PtySize(cols=120, rows=40))`; `stty size` → `b"40 120"`; `id -u; tty` → `b"1000"` and `b"/dev/pts/"`; `echo $TERM $LANG` → `b"xterm-256color C.UTF-8"`; `with pytest.raises(InvalidArgumentException): sbx.commands.send_stdin(pty.pid, "x")`; `pty.last_seq > 0`; `pty.stdout` contains `hola`.
5. Handles that will cross the pause: `bg = sbx.commands.run("sleep 4000", background=True, timeout=None)`; `bg.disconnect()`; `rearm = sbx.commands.run("sleep 60", background=True, timeout=25)`; `rearm.disconnect()`; `watch = sbx.files.watch_dir("/home/user")`; `live = sbx.commands.run("for i in $(seq 1 300); do echo tick $i; sleep 1; done", background=True, timeout=None)`; `ticks = collect_in_thread(live)`; wait until `len(ticks) >= 2`.
6. Pause: `t = perf_counter()`; `assert sbx.pause() is True`; `pause_s` ≤ 30 s logged; `sbx.get_info().state == "SUSPENDED"`; `assert sbx.pause() is False`; `time.sleep(30)` (longer than `rearm`'s 25 s timeout, so a monotonic deadline would have expired).
7. Resume: `t = perf_counter()`; `sbx.resume()`; `resume_s` ≤ 30 s logged; `h = sbx.get_health()`: `h.resume_generation == gen0 + 1`, `h.kernel_state_lost is False`, `abs(h.clock_offset_ms) <= 5000` (value logged, Q41); `sbx.get_info().state == "RUNNING"`.
8. Kernel alive (`SPEC.md` §6 step 5): `sbx.run_code("x").text == "42"` within 10 s (logged); `sbx.run_code("len(df)").text == "2"`.
9. Processes alive and the timeout re-armed: `pids = [p.pid for p in sbx.commands.list()]`: `bg.pid in pids` and `rearm.pid in pids`; `c = sbx.commands.connect(bg.pid)` (first message ≤ 10 s, logged as the re-subscribe latency); `c.pid == bg.pid`; `sbx.commands.kill(bg.pid) is True`; `with pytest.raises(CommandExitException) as e: c.wait()`: `e.value.exit_code == 137`; `t = perf_counter()`; `with pytest.raises(TimeoutException): sbx.commands.connect(rearm.pid).wait()`; elapsed ≤ 30 s (≈ 22 s expected, logged); `rearm.pid not in [p.pid for p in sbx.commands.list()]`.
10. PTY re-attached: `pty2 = sbx.pty.connect(pty.pid, from_seq=pty.last_seq + 1)`; `sbx.pty.send_input(pty2.pid, "echo resumed-$((6*7))\n")`; `read_until(pty2, b"resumed-42", 10)`; the original handle, unread since the pause, also answers: `read_until(pty, b"resumed-42", 10)` and `pty.reconnects == 1`; `sbx.pty.kill(pty.pid) is True`; `with pytest.raises(CommandExitException) as e: pty2.wait()`: `e.value.exit_code == 137`; `sbx.pty.kill(pty.pid) is False`.
11. Live handle across the pause: within 15 s `len(ticks)` grows past its value at the pause, `live.reconnects == 1`, no exception recorded; `sbx.commands.kill(live.pid)`.
12. Watch re-issued: `sbx.files.write("/home/user/after-resume.txt", "x")`; within 10 s `watch.get_new_events()` contains an event with `name == "after-resume.txt"`; `watch.is_running`; `watch.stop()`.
13. `run_code` across a pause (`Reattach`): a thread runs `res = sbx.run_code("import time; time.sleep(25); 'slept'", timeout=120)`; after 3 s `sbx.pause()` (`wait=True`), `time.sleep(5)`, then `thread.is_alive()` and `sbx.get_info().state == "SUSPENDED"` (the in-flight cell did not wake the VM: D14 amendment, Q40), `sbx.resume()`; the thread joins within 60 s with `res.text == "'slept'"` and `res.error is None`; exactly one `rayito.code` INFO record mentioning `Reattach` was logged during the block (captured with `caplog`; the result came through `Reattach`, not through a re-run); `sbx.get_health().resume_generation == gen0 + 2`; `sbx.commands.run("echo ok").stdout.strip() == "ok"`. Block 6 also asserts `get_info().state == "SUSPENDED"` again after its 30 s sleep: the live handle, the PTY and the watch open across the pause did not wake the VM either.
14. Async parity: `asyncio.run(...)` with `AsyncSandbox.connect(sbx.sandbox_id, access_token=sbx.access_token)`: `p = await a.pty.create(size=PtySize(cols=80, rows=24), timeout=None)`; `await a.pty.send_input(p.pid, "echo async-pty\n")`; async `read_until` finds `b"async-pty"`; `await a.pty.kill(p.pid) is True`; `assert await a.pause() is True`; `await a.resume()`; `(await a.run_code("x")).text == "42"`; `(await a.get_health()).resume_generation == gen0 + 3`; `await a.close()`.
15. Teardown (`SPEC.md` §6 step 6): `sbx.kill() is True`; poll `Sandbox.get_info(sbx.sandbox_id, ...)` until `state == "TERMINATED"` (≤ 60 s); `sbx.sandbox_id not in [i.sandbox_id for i in Sandbox.list(template=template_arn, control_plane=...)]`.

`test_auto_resume(e2e_settings, control_plane, template_arn)`:
`sbx = Sandbox.create(template_arn, timeout=900, idle=IdlePolicy(
max_idle_seconds=60, suspended_duration_seconds=600, auto_resume=True),
…)` killed in `finally`: `sbx.run_code("y = 7")`; `sbx.commands.run("echo
warm")`; poll `sbx.get_info().state` every 5 s until `"SUSPENDED"` (≤
240 s; elapsed logged); `t = perf_counter()`; `sbx.commands.run("echo
back").stdout.strip() == "back"` (`auto_resume_s` ≤ 30 s, logged; Q40
records whether a reconnect happened via the `rayito.sandbox` log);
`sbx.run_code("y").text == "7"`; `sbx.get_health().resume_generation ==
1`; `sbx.get_info().state == "RUNNING"`; `sbx.kill() is True`.

Timings pasted into `MILESTONES.md` M5 "Estado de aceptación" and
`AWS_API_NOTES.md` §16: `kernel_ready_s`, PTY create and echo, `pause_s`,
`resume_s`, re-subscribe latency, kernel-alive cell latency, re-arm
timeout latency, `Reattach` completion, `auto_resume_s`, idle → SUSPENDED
delay, `clock_offset_ms`, plus `suspend_ms`/`probe_ms`/`streams_closed`
from the `rayd` log (CloudWatch, when `RAYITO_EXECUTION_ROLE_ARN` is
set) and the image's `snapshotBuild` sizes and build time.

Cost: one image version (+$0.037), two MicroVMs (≈ 6 min and ≈ 4 min)
and four suspend/resume cycles at 2 GB (≈ $0.011 each) — well under
$0.15.

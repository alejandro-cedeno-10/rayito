## Why

Rayito's differentiator (`SPEC.md` §2) is a `pause()`/`resume()` that keeps
the kernel, the background processes and the terminals alive; M5 is where it
becomes real. After M4 `rayd` moves the phase machine on `/suspend` and
`/resume`, `sync`s and reseeds, but leaves every client stream open across
the checkpoint, never re-arms the deadlines that `CLOCK_MONOTONIC` keeps
advancing during a pause (`AWS_API_NOTES.md` §15, Q19), never probes the
kernels and never reports `kernel_state_lost`; `PtyService` answers
`UNIMPLEMENTED`, `Code.Reattach` too, and the Python SDK has no reconnection
contract: a stream cut by a suspend surfaces as `SandboxStateException` and
`resume()` only re-mints the JWE. Every one of these gaps is on the path of
the `SPEC.md` §6 acceptance test, "the most important test in the repo".

## What Changes

Milestone goal from `MILESTONES.md` ("M5 — PTY, suspend/resume y
reconexión"), decided in full by `design.md`:

- `.proto`: one additive field, `PtyServerMessage.seq` (`uint64`, field 5),
  so a PTY re-attaches gap-free with `Connect(pid, from_seq)` exactly like
  `DataEvent.seq` and `ExecuteEvent.seq`. `pty.proto` is otherwise used as
  written; `buf lint` clean, Python regenerated, Rust regenerates at build.
- `rayd` PTY: `nix::pty::openpty` + `tokio::process::Command` (ADR-005),
  shell `<passwd shell> -i -l` as uid 1000 with the M2 spawn posture,
  `setsid` + `TIOCSCTTY` in `pre_exec`, slave chowned to the user,
  `TERM=xterm-256color`, `LANG`/`LC_ALL=C.UTF-8`, `SHELL`; 16 KiB chunks
  into the shared process registry (kind `PTY`, same live cap, 1 MiB ring,
  `seq`, 30 s retention, stall rule), `Resize` via `TIOCSWINSZ`, `Kill` via
  `killpg(SIGKILL)`, server timeout (SIGTERM, SIGKILL +5 s), `KeepAlive`,
  `PtyExited` aligned with `EndEvent`, `Connect(pid, from_seq)`.
- `rayd` `/suspend` for real: bump the generation, refuse new streams, close
  every live client stream (in-stream `suspending` end for process and PTY
  streams; gRPC `UNAVAILABLE suspending` for `WatchDir`, `Read`, `Execute`,
  `Reattach`, `Write`), abort in-flight `Write`s (temporary removed),
  detach executions without interrupting them, `quiesce` the sidecar,
  `sync`, and **always answer 200 inside the budget** (a non-200 terminates
  the VM, measured). Idempotent.
- `rayd` `/resume` for real: bump `resume_generation`, record
  `clock_offset_ms`, credentials nothing to invalidate (no outbound
  connections by design), `kernel_info` probe per live kernel (5 s each,
  ≤ 8 concurrent, whole probe capped at 12 s), `kernel_state_lost` +
  background restart for kernels that failed, reseed in the background,
  200. **Deadlines measure running time**: every server-side timeout
  (process/PTY `timeout_ms`, SIGKILL grace, execution interrupt/restart,
  30 s retention) is re-validated against a running clock that excludes
  suspended time, so a command paused with 50 s left still has 50 s after
  resume.
- `Code.Reattach` implemented: executions are recorded in a 4 MiB
  per-execution ring with `seq`, outlive their `Execute` stream, keep at
  most 8 subscribers, are retained 30 s (running time) after `end`;
  `Reattach(context_id, execution_id, from_seq)` replays and follows.
- `Health` exposes `resume_generation`, `clock_offset_ms` and a real
  `kernel_state_lost`.
- Python SDK: `sbx.pty.create(size, user, cwd, envs, shell, on_data,
  timeout)` → `PtyHandle` (the `CommandHandle` implementation plus
  `resize`, `send_input`), `pty.send_input`, `pty.resize`, `pty.kill`,
  `pty.connect(pid, from_seq)`; `pause(wait=True)`, `resume(wait=True)`
  with JWE re-mint; the reconnection contract: on `UNAVAILABLE`/RST/EOF/502
  poll `Health` with jittered backoff for `resumeTimeoutInSeconds + 30 s`,
  stop on `TERMINATING|TERMINATED` (or `SUSPENDED` without auto-resume),
  and when `resume_generation` changed re-subscribe transparently:
  `commands.connect(pid, from_seq)`, `pty.connect(pid, from_seq)`,
  `WatchDir` re-issued, `Code.Reattach(context_id, execution_id, from_seq)`;
  one retry of a unary after a successful reconnect; the refresher keeps
  running during the pause; idle policy defaults `max_idle_seconds=300`,
  `auto_resume=True`; a channel-level reconnect backoff cap so a resumed
  sandbox answers within seconds. Sync and async trees, in-process fakes.
- Image: a new `rayito-base` version with the M5 `rayd`; hooks unchanged
  (`suspend`/`resume` 30 s); build sanity adds `tty`/`stty`/`/dev/ptmx`.
- Tests: host tests for every domain rule, `cfg(unix)` suites
  `m5_pty.rs` and `m5_suspend_resume.rs`, SDK unit tests against fakes that
  can "suspend", and the real-AWS `tests/e2e/test_m5_pty_suspend_resume.py`
  (the `SPEC.md` §6 sequence, plus timeout re-arm and auto-resume).

Full acceptance criteria: `design.md` "Acceptance test list".

## Capabilities

### New Capabilities
- `pty`: `PtyService` (Create/Connect/SendInput/Resize/Kill) on the shared
  process registry, the PTY spawn posture, and the Python client's `.pty`
  surface with `PtyHandle`.
- `suspend-resume`: the real `/suspend` and `/resume` checklists,
  stream-close forms, running-time deadlines, `resume_generation` /
  `clock_offset_ms` / `kernel_state_lost` in `Health`, and the client's
  `pause()`/`resume()`/`connect()` plus the reconnection contract.

### Modified Capabilities
- `code-execution`: `Reattach` moves from `UNIMPLEMENTED` to a real
  implementation over a 4 MiB per-execution ring; executions outlive their
  stream; backpressure becomes per-subscriber over the ring; cancel-on-drop
  excludes the suspend detach; execution deadlines run on the running clock.
- `process-lifecycle`: server timeouts and the 30 s retention run on the
  running clock; `List` reports PTYs; process RPCs refuse PTY pids (and vice
  versa) with `FAILED_PRECONDITION`; the SDK stream error contract is
  superseded by the reconnection contract.
- `filesystem`: `WatchDir` closes with `UNAVAILABLE suspending` on
  `/suspend`, `Write` in flight is aborted; the SDK `WatchHandle` re-issues
  the watch after a resume instead of ending.

## Impact

- `proto/rayito/v1/pty.proto` (+`seq`), `clients/python/src/rayito/v1/*`
  regenerated (`python scripts/gen_python.py`), Rust at `cargo build`.
- `crates/rayd-core`: new `pty` module (size, spawn plan, environment) and
  the `PtyBackend` port; `clock`/`lifecycle`/`session` gain the running
  clock and `suspended_total`; `process::timeout` becomes running-time
  based; `process::registry` learns `kind(pid)`; `code::ring` (4 MiB
  execution ring) and `code::executions` (execution registry, retention,
  subscribers); `hooks` checklists as pure plans; `KernelStatus` gains
  `kernel_state_lost`; `ProcessIdentity` gains `shell`.
- `crates/rayd`: `adapters/pty_backend.rs` (`NixPtyBackend`, `cfg(unix)`),
  `pty/{manager,child}.rs`, `grpc/pty.rs` (replaces `PendingPtyService`),
  `lifecycle/suspend.rs` (`SuspendSignal` + the stream wrapper that closes
  streams), `code/{ring,executions,execute}.rs` refactor, `hooks/mod.rs`
  real `/suspend` and `/resume`, `main.rs` wiring, `logging.rs` allowlist,
  every existing integration suite adapted to the new constructor
  signatures.
- `clients/python`: `_models.py` (`PtySize`), `_pty_base.py`,
  `_process_base.py` (handle progress generalised, `suspending` as a
  reconnect signal), `_code_base.py` (`Reattach`), `_sandbox_base.py`
  (`ReconnectPoll`), `_transport.py` (channel options, reconnectable
  classification), `sandbox_sync/{main,commands,pty,filesystem,code}.py`
  and the async mirrors, `__init__.py`, unit fakes (`fake_pty.py`, suspend
  hooks on every fake), e2e `test_m5_pty_suspend_resume.py`; version
  `0.0.5`.
- `image/Dockerfile` (M5 header, sanity list), `scripts/hooks-sim.py`
  (`/suspend` and `/resume` with a live stream), `Makefile`/CI unchanged in
  shape.
- Docs: `ARCHITECTURE.md` (PtyService row, suspend/resume section aligned
  with the decided stream-close forms and the running clock, ports and
  adapters tables), `MILESTONES.md` M5 acceptance state, `SECURITY.md`
  (new row for the PTY posture, T2 updated), `AWS_API_NOTES.md` §16 new
  measurements (devpts in the guest, what a client sees at an explicit
  `pause()`, resume-to-`Health` and re-subscribe timings, auto-resume
  through gRPC, timeout re-arm), `README.md`.
- Governed by `AWS_API_NOTES.md` facts: `/suspend` non-200 terminates the
  VM (§5, Q10); `CLOCK_MONOTONIC` advances during suspend and AWS corrects
  the wall clock (§15, Q19); a stream with no bytes is not traffic (Q15);
  the 8-connection cap (§7); `SuspendMicrovm` 2 TPS (§5, §11); a JWE minted
  before a pause is valid after (Q28); kernels over `ipc://` survive (Q7);
  auto-resume answers in 1.1–1.6 s (Q4). M5 adds no control-plane call and
  no new AWS parameter.

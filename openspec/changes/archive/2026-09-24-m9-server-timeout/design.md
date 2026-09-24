## Context

### Today

`maximumDurationInSeconds` counts running plus suspended time, is at most
28800 s, and is fixed at `run-microvm` because `UpdateMicrovm` does not
exist (`AWS_API_NOTES.md` §1, §2, §11). ADR-007 therefore made it the
sandbox life:

- `rayito.e2b` raises `UnimplementedError` for `set_timeout`.
- `connect()` never extends anything.
- `get_info().expires_at` is `started_at + maximum_duration_seconds`.

### Measured facts this design rests on

All are in `AWS_API_NOTES.md`, in placeholders:

- **Q58**: `rayd` is PID 1. When it exits cleanly, the endpoint answers 502
  within 1 s and `get-microvm` still says `RUNNING` for about 15 s. Then it
  goes `TERMINATING` → `TERMINATED` with `stateReason` "Container Stopped
  with Exit Code: 0". The platform never calls `/terminate` afterwards. A
  uid 1000 process reaches the hooks port over loopback (the T2 residual).
- **§15 / Q19 / Q41**: `CLOCK_MONOTONIC` advances during a suspension and
  AWS corrects the wall clock on resume (`clock_offset_ms` 0). A deadline on
  the raw monotonic clock therefore counts suspended time, which is what
  E2B's wall-clock `end_at` does and what the platform cap does. The
  existing running-clock `Deadline` (monotonic minus suspended) is the
  wrong clock here and is not reused.
- **§5 / Q10**: a non-200 `/suspend` terminates the VM. This design never
  does that: `/suspend` keeps answering 200 whatever happens.
- **§5**: `SuspendMicrovm` 2 TPS. `idlePolicy` has three mandatory fields:
  `maxIdleDurationSeconds` ≥ 60, `suspendedDurationSeconds` ≥ 0 and
  `autoResumeEnabled`. Idle is counted only from bytes that cross the
  endpoint (§7, Q15).
- **Q40**: with `autoResumeEnabled`, the proxy holds the first request
  while the VM resumes (0.67 s).

### No new AWS parameter

Every control-plane call used here already appears in `AWS_API_NOTES.md`:

- `run-microvm` with `maximumDurationInSeconds`, `idlePolicy{…}` and
  `runHookPayload`
- `get-microvm` with `state`, `stateReason` and `terminatedAt`
- `suspend-microvm`, `resume-microvm`, `terminate-microvm`
- `create-microvm-auth-token`

Hard rule 1 is satisfied without additions. The new `AWS_API_NOTES.md`
rows are measurements (Q63–Q65), not parameters.

### Sibling changes

This is M9 change 3 of 6, implemented after `m9-deno-kernels` and
`m9-file-transfer`, and before `m9-sandbox-observability`,
`m9-egress-policy` and `m9-e2b-v2-surface`.

Proto numbering is fixed by the Scope phase:

- `HealthResponse`: 12 is ours; 13, 14 and 15 belong to siblings.
- The new files are `lifecycle.proto` (ours) and `network.proto` (egress).
- ADR numbers: 010 file-transfer, 011 this change, 012 egress, 013 Deno.

## Goals / Non-Goals

**Goals**

- `create(timeout=)` is a server-enforced deadline in `rayd`. It keeps
  working when the client dies.
- It is movable:
  - `set_timeout` sets it exactly and can shorten it.
  - `connect(timeout=)` only extends it (AT_LEAST).
- It is bounded by a platform cap chosen at `create()`: `max_lifetime`, at
  most 28800 s.
- `on_timeout='kill'` ends the VM without IAM. `on_timeout='pause'`
  suspends it: exactly with a live client, and within `max_idle` without
  one.
- `auto_resume` follows E2B, including the 5-minute minimum after an
  auto-resume. `auto_resume=False` is enforced by `rayd`.
- `get_info().expires_at` (native) and `end_at` (shim) are the logical
  deadline.
- A call that hits the deadline raises `TimeoutException`, as in E2B.
- Fail closed on images older than M9 whenever a lifecycle was asked for.
- Full backward compatibility for native callers that pass neither
  `max_lifetime` nor `on_timeout`: identical wire, identical ADR-007
  semantics.

**Non-Goals**

- A cap beyond 28800 s, or a cap that stops counting suspended time.
  Impossible: see "Honest costs".
- `keep_memory=False`, i.e. a filesystem-only pause. `suspend-microvm`
  always snapshots memory (§5), so the shim raises `UnimplementedError`.
  Its sibling row `pause(keep_memory=False)` belongs to
  `m9-e2b-v2-surface`.
- The TypeScript E2B shim (`rayito/e2b`). It does not exist yet and is
  created by `m9-e2b-v2-surface`, which maps this change's native TS
  surface. This change ships the native TS mirror only.
- Mapping the shim's `SandboxInfo.lifecycle` field. That is
  `m9-e2b-v2-surface`'s job. This change fills the **native**
  `SandboxInfo.lifecycle` it will read.
- Peer-uid authentication of hooks (C-01). It stays deferred, and this
  design does not depend on it (D14).
- Any control-plane service, scheduler or execution-role permission.

## Decisions

### D1. ADR-011 replaces ADR-007

`ARCHITECTURE.md` keeps the ADR-007 section with its body intact, and gains
a first line under its heading:

> **Sustituida por ADR-011** (`m9-server-timeout`).

A new section `## ADR-011 — Plazo lógico impuesto por rayd; el tope de la
plataforma se elige en create()` goes after ADR-010, or after ADR-009 if
ADR-010 has not landed yet. Its content, written in Spanish in the style of
ADR-007…009:

- **Contexto**: ADR-007, Q58, and the monotonic clock of §15.
- **Decisión**:
  - D3 (payload), D4 (domain), D6 (adapters) and D7 (the SDK trigger).
  - `max_lifetime` = `maximumDurationInSeconds`; `timeout` = the logical
    deadline.
  - A native `create()` without `max_lifetime`/`on_timeout` behaves exactly
    as ADR-007: phase `UNMANAGED`, cap = timeout.
- **Consecuencias**: the honest costs listed at the end of this design,
  the shim's M9-image requirement, and that `reincarnate()` remains the
  path beyond `max_lifetime`.

ADR-009 consequence (4) becomes: «`reincarnate()` es la respuesta honesta a
lo que `set_timeout` no puede dar: pasar de `max_lifetime` (ADR-011)».

### D2. Proto delta (applied by the Contract agent, never by implementers)

> **Status: applied by the Contract step (2026-09-22), verbatim.** Final numbers: new `lifecycle.proto` with `LifecycleService.SetTimeout`, `TimeoutMode` (0-2), `TimeoutAction` (0-2), `LifecyclePhase` (0-4), `SetTimeoutRequest` (1-2), `LifecycleState` (1-7); `HealthResponse.lifecycle = 12`; `sandbox_timeout` added to the `StreamError` and `EndEvent.status` comments (`pty.proto` untouched). Compile stubs to replace: `LifecycleGrpc` (unit struct in `crates/rayd/src/grpc/lifecycle.rs`, already registered in `grpc/mod.rs` behind the access-token layer) answers `UNIMPLEMENTED`, and `grpc/health.rs` sets `lifecycle: None` until D6 fills it. Regenerated with `buf generate` (Python and TypeScript, byte-identical to the committed gencode for untouched protos) and `crates/rayito-proto/build.rs` (`PROTO_FILES` now lists `common`, `lifecycle`, `network`, `health`, `process`, `filesystem`, `pty`, `code`). `buf lint` passes with the unchanged `buf.yaml`, and `buf breaking --against` a copy of the pre-M9 `proto/` (FILE) reports nothing. Do not regenerate Python with `python scripts/gen_python.py`: grpcio-tools 1.84.0 emits protobuf 7.35.1 gencode plus a gRPC version-check preamble, which differs from the committed 7.36.1 output of `buf generate`.

New file `proto/rayito/v1/lifecycle.proto`:

```proto
syntax = "proto3";

package rayito.v1;

// Plazo lógico del sandbox impuesto por rayd (ADR-011). SetTimeout exige
// x-access-token: el código del sandbox no puede alargarse a sí mismo.
// Más allá del tope (cap) responde INVALID_ARGUMENT con
// "timeout beyond cap; cap_unix_ms=<n>"; sobre un sandbox sin bloque
// lifecycle en el runHookPayload, FAILED_PRECONDITION "lifecycle_unmanaged".
service LifecycleService {
  rpc SetTimeout(SetTimeoutRequest) returns (LifecycleState);
}

enum TimeoutMode {
  TIMEOUT_MODE_UNSPECIFIED = 0;
  // Fija el plazo en ahora + timeout_ms; puede acortarlo (set_timeout).
  TIMEOUT_MODE_EXACT = 1;
  // max(plazo actual, ahora + timeout_ms); nunca acorta (connect(timeout)).
  TIMEOUT_MODE_AT_LEAST = 2;
}

enum TimeoutAction {
  TIMEOUT_ACTION_UNSPECIFIED = 0;
  TIMEOUT_ACTION_KILL = 1;
  TIMEOUT_ACTION_PAUSE = 2;
}

enum LifecyclePhase {
  LIFECYCLE_PHASE_UNSPECIFIED = 0;
  // El runHookPayload no trajo bloque lifecycle: la vida es la de la
  // plataforma (maximumDurationInSeconds, ADR-007).
  LIFECYCLE_PHASE_UNMANAGED = 1;
  LIFECYCLE_PHASE_ACTIVE = 2;
  // Reanudado después del plazo: 30 s para que connect() mande
  // SetTimeout(AT_LEAST); sólo Health y SetTimeout responden.
  LIFECYCLE_PHASE_RESUME_GRACE = 3;
  // Plazo vencido: sólo Health y SetTimeout responden (FAILED_PRECONDITION
  // "sandbox_timeout" al resto); en modo kill rayd está saliendo.
  LIFECYCLE_PHASE_EXPIRED = 4;
}

message SetTimeoutRequest {
  uint64 timeout_ms = 1;
  TimeoutMode mode = 2;
}

message LifecycleState {
  LifecyclePhase phase = 1;
  // Reloj de pared; 0 en UNMANAGED.
  int64 deadline_unix_ms = 2;
  int64 cap_unix_ms = 3;
  uint64 timeout_ms = 4;
  TimeoutAction on_timeout = 5;
  bool auto_resume = 6;
  // SetTimeout que movieron el plazo en este arranque.
  uint32 extensions = 7;
}
```

`health.proto`:

- Add `import "rayito/v1/lifecycle.proto";`.
- Add to `HealthResponse`:

  ```proto
    // Plazo lógico (ADR-011). Ausente en agentes anteriores a M9: es la
    // puerta de capacidad del SDK. Fase UNMANAGED cuando el runHookPayload no
    // pidió lifecycle. No es secreto (lo lee cualquiera que llegue a Health).
    LifecycleState lifecycle = 12;
  ```

Comment-only edits:

- `common.proto` `StreamError`: the closed list gains `"sandbox_timeout"`.
  `m9-file-transfer` adds four other codes to the same comment. The Contract
  agent merges both.
- `process.proto` `EndEvent.status`: the list gains `"sandbox_timeout"`,
  with the sentence «"sandbox_timeout" cierra el stream al vencer el plazo
  lógico (ADR-011); error.code = "sandbox_timeout"».
- `pty.proto` `PtyExited`: the comment stays «Misma forma que EndEvent de
  ProcessService.», which already covers it.

Codegen, all by the Contract agent:

- `crates/rayito-proto/build.rs`: `PROTO_FILES` gains `"lifecycle"`, and
  the array length changes accordingly.
- `buf generate` for TS; `python scripts/gen_python.py` for Python.

Lint and compatibility:

- `buf lint` STANDARD passes: `RPC_RESPONSE_STANDARD_NAME` is excepted in
  `buf.yaml`, the enum prefixes follow the rule, and the zero values end in
  `_UNSPECIFIED`.
- Every edit is additive, so the `FILE` breaking check passes.

### D3. `runHookPayload` lifecycle block (hand-written JSON contract)

This is the only non-proto wire. Shape (`crates/rayd-core/src/run_payload.rs`
module doc updated):

```json
{"v":1,"token_sha256":"…","user":"user","workdir":"/home/user",
 "lifecycle":{"auto_resume":false,"cap_s":900,"on_timeout":"kill","timeout_s":60}}
```

The block adds about 80 characters to the 4096 budget. The SDK serialises
it with `sort_keys`, like the rest of the payload. `v` stays 1: an older
`rayd` ignores the key (serde without `deny_unknown_fields`), and D8's gate
catches exactly that.

`rayd` validation. The first violation is
`RunPayloadError::InvalidLifecycle(&'static str)`, which leaves the agent
token-less, as `InvalidLimits` does today. The SDK enforces the same rules,
so a correct SDK never trips this.

- All four keys are required inside the block.
- `timeout_s` ∈ `1..=28800`.
- `cap_s` ∈ `120..=28800` (`MIN_CAP_SECONDS` = margin 60 + 60 s of usable
  life).
- `timeout_s ≤ cap_s`.
- `on_timeout` is exactly `"kill"` or `"pause"`. It is parsed as a string
  and matched, so an unknown value is `InvalidLifecycle` and never quoted.
- `auto_resume: true` requires `"pause"`.
- Error messages name the violated rule, never a value.

### D4. Domain: `crates/rayd-core/src/sandbox_timeout/`

This is a new module, re-exported from `lib.rs`, whose doc paragraph gains
"the logical sandbox deadline (`sandbox_timeout`)". It has no tokio, tonic
or axum. Files:

- `mod.rs`: re-exports, constants and the module doc (why the raw monotonic
  clock).
- `policy.rs`: `TimeoutAction {Kill, Pause}`, `TimeoutPolicy {on_timeout,
  auto_resume}`, `LifecycleSpec {timeout, cap, policy}` (built by
  `run_payload.rs`), `TimeoutMode {Exact, AtLeast}`.
- `machine.rs`: `SandboxTimeout`, `LifecyclePhase`, `DeadlineAction`,
  `LifecycleView`, `SandboxTimeoutError`.
- `ports.rs`: `SelfTerminator` and `TerminationReason`.

**Constants** (`mod.rs`). The shared ones equal the `limits.json` keys added
in D10, and a unit test compares them.

| Name | Value | `limits.json` key |
|---|---|---|
| `MAX_LIFETIME_SECONDS` | 28 800 | `maxDurationSeconds` (existing) |
| `MIN_CAP_SECONDS` | 120 | `lifecycleMinMaxLifetimeSeconds` |
| `CAP_MARGIN` | 60 s | `lifecycleCapMarginSeconds` |
| `RESUME_GRACE` (default setting) | 30 s | `lifecycleResumeGraceSeconds` |
| `AUTO_RESUME_MIN_TIMEOUT` | 300 s | `lifecycleAutoResumeMinSeconds` |
| `MIN_SET_TIMEOUT` | 1 s | `lifecycleMinTimeoutSeconds` |
| `TIMEOUT_EXIT_CODE` | 124 | `lifecycleTimeoutExitCode` |

`CAP_MARGIN` exists because `/run` lands about 2 s after AWS's `startedAt`
(§2). `rayd` only knows its own `/run` instant, so the effective cap is
`/run + cap_s − 60 s`. That is always before the platform's own kill, so
the logical path (streams closed with `sandbox_timeout`, graceful exit)
always runs first.

**`TimeoutSettings`** holds the timing knobs. The integration tests shrink
them; production uses `Default`. It is added as field `timeout` of the
existing `SessionSettings`.

| Field | Default |
|---|---|
| `tick` | 500 ms |
| `freeze_threshold` | 2 s |
| `resume_grace` | 30 s |
| `suspend_hold` | 20 s (equals `SUSPEND_GATE_TIMEOUT`) |
| `sigterm_grace` | 5 s |
| `exit_drain` | 2 s |
| `force_budget` | 12 s |

**`SandboxTimeout`** state. All instants are raw monotonic `Duration`s on
the `Clock` port.

- `phase`: `Unmanaged`, `Active`, `ResumeGrace{until}`, `Expired`, or
  `Terminating` (reported as `EXPIRED`).
- `timeout: Duration`
- `deadline: Duration`
- `cap: Duration`
- `policy`
- `extensions: u32`
- `hold: HoldState {Unused, Until(Duration), Spent}`: reset to `Unused`
  whenever the deadline moves.
- `grace_spent: bool` (independent review, 2026-09-24): whether the current
  deadline already opened its one resume grace; reset by `install` and
  whenever the deadline moves.

Rules (pure functions; `now` is a monotonic reading):

- **`install(spec, now)`**:
  - `cap = now + spec.cap − CAP_MARGIN`
  - `deadline = min(now + spec.timeout, cap)`
  - `phase = Active`
  - No block means `Unmanaged`: `set_timeout` and `tick` are inert.
- **`set_timeout(now, mode, timeout) -> Result<LifecycleView,
  SandboxTimeoutError>`**:
  - `Unmanaged` → `Unmanaged` error.
  - `Terminating` → `Expired` error.
  - `timeout < MIN_SET_TIMEOUT` → `InvalidTimeout`.
  - `target = now + timeout`; `target > cap` →
    `BeyondCap{cap}`, and nothing changes.
  - `Exact`: `deadline = target`.
  - `AtLeast`: `deadline = max(deadline, target)` while `Active`, or
    `target` from `ResumeGrace`/`Expired` (the old deadline is past).
  - `timeout = timeout` when the deadline moved.
  - `phase = Active`, hold reset, and `extensions += 1` when the deadline
    changed.
  - `Exact` from `Expired` in pause mode reopens: this is E2B's
    `set_timeout` on a timed-out sandbox that is not yet suspended.
- **`tick(now, suspending: bool, thawed: bool) -> Option<DeadlineAction>`**
  (`suspending` = the hook phase is `Suspending`; `thawed` = D6's watcher
  saw a monotonic gap ≥ `freeze_threshold` since its previous tick):
  - `Active` and `now ≥ deadline`:
    - if `thawed` and the grace is available (see *One grace per
      deadline* below): `ResumeGrace{until: min(now + resume_grace, cap)}`
      → `Gate` (a real checkpoint straddled the deadline; the `/resume`
      follows)
    - else if `suspending`:
      - hold `Unused` → `Until(now + suspend_hold)` → `None`
      - `Until(t)` with `now < t` → `None`
      - otherwise the hold is `Spent` and the deadline acts
    - else it acts:
      - `Kill` → `Terminating` → `Terminate`
      - `Pause` → `Expired` → `Expire`
  - `ResumeGrace{until}` and `now ≥ until`:
    - `Kill` → `Terminating` → `Terminate`
    - `Pause` → `Expired` → `ReExpire`
  - Otherwise `None`.
- **`resumed(now, frozen: bool)`**, called by the `/resume` adapter after a
  changed transition. `frozen` is true when `now − watcher.last_tick ≥
  freeze_threshold`, i.e. the VM was really frozen.
  - If `!frozen`, `Unmanaged`, `Terminating`, or `Active` with
    `now < deadline` → no-op.
  - Otherwise the deadline has passed:
    - `Pause` with `auto_resume` → `timeout = max(timeout,
      AUTO_RESUME_MIN_TIMEOUT)`, `deadline = min(now + timeout, cap)`,
      `Active`, hold reset (E2B's rule). If `now ≥ cap`, it stays
      `Expired`.
    - Anything else → `ResumeGrace{until: min(now + resume_grace, cap)}`
      if the grace is available; otherwise nothing changes (a grace in
      course keeps its end, an expiry stays expired, a kill-mode deadline
      acts at the next tick).
- **One grace per deadline, never past the cap** (independent review,
  2026-09-24). The thaw is a ≥ `freeze_threshold` gap between two watcher
  ticks, and starving the `rayd-timeout` thread of CPU from inside the VM
  can produce it; a forged `/suspend` + `/resume` right after it then
  passed `frozen` too, so the grace could be re-entered indefinitely and,
  in kill mode, outlive the cap. Now:
  - a grace ends at `min(now + resume_grace, cap)`, and none opens at or
    past the cap (the policy acts instead);
  - a deadline opens at most one grace, from `tick` or from `resumed`,
    whichever comes first; a real checkpoint needs only one (the thaw tick
    and its `/resume` are the same freeze);
  - eligibility returns only when the deadline moves: `SetTimeout`, which
    requires the access token, or E2B's auto-resume rule, which is not a
    grace and is unaffected (`auto_resume_after_a_freeze_applies_the_five_minute_minimum`
    stays green);
  - the worst a sandbox can extract without the token is therefore one
    `resume_grace` (30 s) per deadline, inside the cap, in kill mode; in
    pause mode a grace admits nothing an expiry does not, and the SDK
    reopens both phases alike (`REOPEN_PHASES`).
  Tests: `a_grace_never_extends_past_the_cap`, `a_deadline_grants_one_grace`
  (red before the change), `a_moved_deadline_restores_the_grace`.
- **`view(reading: ClockReading) -> LifecycleView`**: converts each instant
  to wall milliseconds as `wall_now + (instant − mono_now)`, signed,
  saturating.
  - `Unmanaged` reports zeros and `on_timeout` `None`.
  - `ResumeGrace` and `Expired` report the past deadline.
- **`admits(phase, rpc_path) -> bool`**: during `ResumeGrace`, `Expired` and
  `Terminating`, only `/rayito.v1.HealthService/Health` and
  `/rayito.v1.LifecycleService/SetTimeout` are admitted.

`DeadlineAction` is `Expire`, `ReExpire`, `Gate` or `Terminate`.

`SandboxTimeoutError`: `Unmanaged`, `Expired`, `InvalidTimeout`,
`BeyondCap{cap_unix_ms}`. Messages never contain tokens or payload.

**Port** (`ports.rs`, introduced now because its adapter ships in the same
change):

```rust
pub enum TerminationReason { SandboxTimeout }
pub trait SelfTerminator: Send + Sync {
    /// Starts the graceful end of the workload and of the agent; never blocks.
    fn begin(&self, reason: TerminationReason);
    /// Last resort after `force_budget`: ends the agent process now.
    fn force(&self, reason: TerminationReason);
}
```

### D5. Integration into the existing domain (`lifecycle.rs` untouched)

- `run_payload.rs`: `RunPayloadWire.lifecycle: Option<LifecycleWire>` is
  validated as in D3. `RunPayload` gains `lifecycle: Option<LifecycleSpec>`.
- `session.rs`:
  - `SandboxSession` gains `timeout: Mutex<SandboxTimeout>`, a separate
    lock that is never held together with `state`.
  - `install_payload` calls `install(spec, clock.monotonic())`.
  - New methods:
    - `lifecycle() -> LifecycleView`
    - `set_timeout(mode, timeout) -> Result<LifecycleView,
      SandboxTimeoutError>`
    - `tick_timeout(thawed) -> Option<DeadlineAction>` (reads `phase()`
      for `suspending`)
    - `timeout_resumed(frozen)`
    - `admits_rpc(path) -> bool`
    - `timeout_managed() -> bool`
  - `suspend()`, `resume()`, `terminate()` and
    `recover_from_stale_suspend()` do not change.
- `health.rs`: `HealthSnapshot` gains `lifecycle: LifecycleView`.
  `session.health()` fills it.
- `auth.rs` does not change: `SetTimeout` is not the anonymous path, so it
  requires the token automatically.

### D6. Adapters in `crates/rayd`

**Watcher** (`src/lifecycle/timeout_watcher.rs`):

- `spawn_timeout_watcher(session, terminator: Arc<dyn SelfTerminator>,
  closer: StreamCloser) -> TimeoutWatcher` starts a named `std::thread`
  (`rayd-timeout`), independent of the tokio runtime.
- `TimeoutWatcher { thread: std::thread::Thread, last_tick: Arc<AtomicU64>
  }` has two methods:
  - `wake()` (`unpark`), called after `/run` installs a lifecycle, after a
    changed `/resume`, and after every successful `SetTimeout`.
  - `frozen(now) -> bool` (`now − last_tick ≥ freeze_threshold`).
- The loop:
  - When `!session.timeout_managed()`, it calls `park()`.
  - Otherwise, each iteration:
    - reads `now` from the session clock
    - computes `thawed = now − last_tick ≥ freeze_threshold`
    - stores `last_tick`
    - dispatches `session.tick_timeout(thawed)`
    - calls `park_timeout(tick)`
- Dispatch:
  - `Expire` and `Gate`: `closer.close(StreamCloseReason::SandboxTimeout)`
    plus a log line.
  - `ReExpire`: a log line only.
  - `Terminate`:
    1. `terminator.begin(SandboxTimeout)`
    2. `std::thread::sleep(force_budget)`
    3. `terminator.force(SandboxTimeout)`
    4. the thread returns
- A stalled runtime therefore cannot skip the deadline: the thread ends the
  process itself.

**Stream closer** (`StreamCloser` in the same file) is a small struct over
`Arc<CodeManager>` and `Arc<SuspendSignal>`. `close(reason)` calls
`code.detach_for_suspend()` (an `Execute` origin is not interrupted, as on
`/suspend`) and then `suspend.close_all(reason)`.

**Close reasons** (`src/lifecycle/suspend.rs`):

- `pub enum StreamCloseReason { Suspending, SandboxTimeout }`, with
  `as_str()` returning `"suspending"` / `"sandbox_timeout"`.
- `SuspendSignal`:
  - keeps `broadcast(generation)` (reason `Suspending`)
  - gains `close_all(reason)`, which rotates the token without touching
    the generation
  - stores the reason in the rotated token's shared cell before
    cancelling
- `SuspendWatch::close_reason()` reads it back.
- `SuspendClose` closures take the reason: `F: Fn(StreamCloseReason) ->
  SuspendClose<T>`.
- `suspending_status()` becomes `close_status(reason)`:
  - `Suspending` → `Status::unavailable("suspending")`
  - `SandboxTimeout` → `Status::failed_precondition("sandbox_timeout")`,
    never `UNAVAILABLE`, which would start the SDK's reconnect loop

Close forms, one line per stream kind (`grpc/{process,pty,filesystem,code,
persistence}.rs`):

| Stream | `suspending` (unchanged) | `sandbox_timeout` |
|---|---|---|
| `Process.Start`/`Connect` | `EndEvent{exited:false,status:"suspending",error.code:"suspending"}` | `EndEvent{exited:false,status:"sandbox_timeout",exit_code:0,error:{code:"sandbox_timeout",message:"sandbox timeout"}}` |
| `Pty.Create`/`Connect` | `PtyExited{…"suspending"}` | `PtyExited{exited:false,status:"sandbox_timeout",error.code:"sandbox_timeout"}` |
| `WatchDir`, `Read`, `Execute`, `Reattach`, `Checkpoint`, `Restore` | `UNAVAILABLE suspending` | `FAILED_PRECONDITION sandbox_timeout` |
| `Write` (client stream) | abort, `UNAVAILABLE suspending` | abort, `FAILED_PRECONDITION sandbox_timeout` |

**RPC gate** (`src/grpc/timeout_gate.rs`):

- `SandboxTimeoutGateLayer` is inner to `AccessTokenLayer`: an
  unauthenticated caller gets `UNAUTHENTICATED` and never learns the phase.
- When `!session.admits_rpc(path)`, it drains the request body and answers
  `FAILED_PRECONDITION` with message `sandbox_timeout`. The drain reuses
  the `drain_rejected_body` helper, extracted from `access_token.rs` into
  `src/grpc/reject.rs`, with the same 1 MiB / 2 s bounds (Q29).
- The `GrpcRouter` type alias gains the layer.

**`LifecycleService`** (`src/grpc/lifecycle.rs`, `LifecycleGrpc`):

- Request validation:
  - `mode == UNSPECIFIED` → `INVALID_ARGUMENT` "mode must be EXACT or
    AT_LEAST"
  - `timeout_ms == 0` → `INVALID_ARGUMENT`
- Calls `session.set_timeout`, then `watcher.wake()`, and returns
  `LifecycleState`.
- Error mapping:
  - `BeyondCap` → `INVALID_ARGUMENT` "timeout beyond cap;
    cap_unix_ms=<n>"
  - `InvalidTimeout` → `INVALID_ARGUMENT` "timeout below 1 s"
  - `Unmanaged` → `FAILED_PRECONDITION` "lifecycle_unmanaged"
  - `Expired` (kill mode, terminating) → `FAILED_PRECONDITION`
    "sandbox_timeout"
- Registered in `grpc/mod.rs` (`LifecycleServiceServer`). `Services` gains
  `timeout: Arc<TimeoutWatcher>`.
- `grpc/health.rs` maps `LifecycleView` into
  `HealthResponse.lifecycle = Some(..)` always, with phase `UNMANAGED` when
  there is no block. The tests' `crates/rayd/tests/common/mod.rs` builders
  gain the watcher.

**Hooks** (`src/hooks/mod.rs`):

- `HookServices` gains `timeout: Arc<TimeoutWatcher>`.
- `/run` `Installed` → `watcher.wake()`.
- `/resume` changed → `session.timeout_resumed(watcher.frozen(now))`, then
  `wake()`, and the `resume recorded` log line gains `lifecycle_phase`.
- `/suspend` and `/terminate` are unchanged. `/suspend` still answers 200
  always.

**Exit terminator** (`src/lifecycle/exit_terminator.rs`, `ExitTerminator`).
It is built in `main` with:

- a `tokio::runtime::Handle`
- the shutdown `CancellationToken`
- `Arc<SuspendSignal>`
- the process manager
- `Arc<CodeManager>`
- `Arc<ExitReason>` (an `AtomicU8`)
- the `TimeoutSettings`

`begin` spawns this sequence on the runtime:

1. `close_all(SandboxTimeout)`
2. `processes.signal_all(SIGTERM)`: new `ProcessManager::signal_all(signal)
   -> usize`, which walks the shared registry, so PTYs are covered through
   `send_signal`, which accepts both kinds
3. `code.stop_for_exit(SIGTERM)`
4. sleep `sigterm_grace`
5. `signal_all(SIGKILL)` and `code.stop_for_exit(SIGKILL)`
6. sleep `exit_drain`
7. `exit_reason.set(SandboxTimeout)`
8. `shutdown.cancel()`

`force` logs `timeout_forced_exit` and calls
`std::process::exit(TIMEOUT_EXIT_CODE)`.

**Code manager** (`src/code/manager.rs` and `supervisor.rs`):

- New `CodeManager::stop_for_exit(signal)`:
  - sets a `stopping` flag the supervisor checks before relaunching the
    sidecar
  - signals every kernel process group (`ContextRegistry::kernel_pids`)
  - signals the sidecar's group
- `KernelKiller` becomes `KernelSignaller = Arc<dyn Fn(u32, i32) + Send +
  Sync>`; the existing call site passes `SIGKILL`.
- The `SidecarLink` port gains `fn terminate(&self)` (SIGTERM to the
  group), implemented in `adapters/sidecar_process.rs` next to `kill`.
- The sidecar's own SIGTERM handler (`server.stop`) makes it exit within
  the grace. No sidecar code changes.

**`main.rs`**:

- Builds the watcher and terminator after the managers.
- Passes them to `Services` and `HookServices`.
- `main` returns `anyhow::Result<std::process::ExitCode>`: `SUCCESS` for a
  signal or `/terminate`, and `ExitCode::from(TIMEOUT_EXIT_CODE)` when
  `ExitReason` says `SandboxTimeout`.
- The existing log line `rayd stopped` gains `reason`.

**Exit code: decided, with a measured fallback.** `rayd` exits with 124,
the `timeout(1)` convention.

- Q63 (D12 step 1) measures it on the first published M9 image. The design
  keeps 124 if the VM reaches `TERMINATED` within 45 s with `stateReason`
  "Container Stopped with Exit Code: 124".
- If the platform instead restarts the container, keeps it `RUNNING`
  longer than with exit 0, or hides the code, then:
  - `TIMEOUT_EXIT_CODE` becomes 0 in `rayd-core` and in `limits.json`
  - the SDK helpers `SandboxInfo.timed_out` / `timedOut` and their tests
    are deleted
  - the Q63 row records why
- There is no third option.

**Logging** (T9). Every transition logs one line `sandbox_timeout` with
`phase`, `on_timeout`, `timeout_ms`, `extensions`, `overrun_ms` and
`action`. `SetTimeout` logs `rpc`, `mode`, `timeout_ms`, `outcome` and
`extensions`. Never a token, a payload character, `envs` or `metadata`.

### D7. Pause mode on the SDK side: the deadline trigger

rayd stays the source of truth. The SDK only reacts to a deadline rayd
reported.

- **Pure helper** `pause_trigger_delay(lifecycle, now_unix_ms) ->
  float | None`: `None` unless `on_timeout == "pause"`.
  - `active` → `max(0, deadline − now) + 1 s` (`PAUSE_TRIGGER_SLACK`,
    covers clock skew)
  - `expired` → `0`
  - `resume_grace`/`unmanaged` → `None`
- **Arming**: every `SandboxLifecycle` the SDK records re-arms the trigger:
  from `Health` via `_record_health`, and from the `LifecycleState`
  `SetTimeout` returns.
  - Sync: one `threading.Timer` (daemon) held in `self._deadline_timer`,
    cancelled and replaced.
  - Async: a `loop.call_later` handle that schedules a task.
  - TS: `setTimeout(...).unref()`.
  - `close()`, `kill()` and `__exit__` cancel it.
- **Firing**:
  1. `get-microvm`. If the state is not `RUNNING`, stop: never probe
     `Health` on a suspended VM, because it would wake it.
  2. `Health`. If `lifecycle.phase == expired`, go to step 3. If the phase
     is `active` with a later deadline, re-arm. Otherwise stop.
  3. `_suspend_for_deadline()` calls `suspend-microvm` through the 2 TPS
     bucket. It does **not** set the pending-pause flag `_paused`, so the
     next request auto-resumes exactly as after an idle suspend (Q40).
- All failures in the trigger are logged at `warning` and swallowed: the
  idle policy is the backstop.
- **Reopen after a brief deadline pause** (found by the real-AWS
  regression, 2026-09-23): an accepted `_suspend_for_deadline()` records
  the current `resume_generation`. The first `FAILED_PRECONDITION
  sandbox_timeout` on a unary or on a stream's first message then probes
  `Health` once and, when the generation advanced and `rayd` still reports
  `expired` with `on_timeout="pause"` and `auto_resume`, sends
  `SetTimeout(EXACT, min(max(timeout, 300 s), cap − now − 5 s))`
  (`auto_resume_reopen` / `autoResumeReopenMs`) and retries the call once.
  This covers a real suspension shorter than `freeze_threshold` (the next
  request of the same client resumes the VM ≈ 1-2 s after the trigger
  suspended it), which `rayd` cannot tell from a forged hook pair. One
  attempt per suspension; a failure leaves the original error.

### D8. Python SDK (sync and async identical over shared pure helpers)

**New pure module `clients/python/src/rayito/_lifecycle_base.py`:**

- `LifecycleBlock` (frozen dataclass: `timeout_s`, `cap_s`, `on_timeout`,
  `auto_resume`) with `to_wire()`.
- `LifecyclePlan` (frozen: `block: LifecycleBlock | None`,
  `platform_duration: int`, `idle: IdlePolicy | None`).
- `resolve_lifecycle(*, timeout, max_lifetime, on_timeout, idle,
  default_max_lifetime=None) -> LifecyclePlan`:
  - No `max_lifetime` and no `on_timeout` → `block=None`,
    `platform_duration=timeout`, `idle=resolve_idle_policy(idle, timeout)`.
    This path is byte-for-byte today's.
  - Otherwise `on_timeout = on_timeout or "kill"`, and a value outside
    `{"kill","pause"}` → `InvalidArgumentException("on_timeout debe ser
    'kill' o 'pause', recibido …")`.
  - Resolving `max_lifetime`:
    - explicit: an int (not a bool) in `120..=28800`; above 28800 →
      `SandboxLifetimeException` naming `max_lifetime` and 28800; below
      120 → `InvalidArgumentException`; `timeout > max_lifetime` →
      `InvalidArgumentException`
    - omitted: `default_max_lifetime` when given (the shim), else
      `min(max(timeout + 60, 120), 28800)`
  - `pause`:
    - `idle is None` → `InvalidArgumentException("on_timeout='pause'
      necesita idle=IdlePolicy(...): sin cliente, la suspensión la hace la
      política de idle de la plataforma")`
    - an explicit `idle.suspended_duration_seconds` →
      `InvalidArgumentException`
    - `max_idle_seconds ≥ max_lifetime` → `InvalidArgumentException`
      (existing check)
    - platform idle = `IdlePolicy(max_idle_seconds,
      max_lifetime − max_idle_seconds, auto_resume=True)`
    - `block.auto_resume = idle.auto_resume`
  - `kill`: platform idle = `resolve_idle_policy(idle, max_lifetime)` and
    `block.auto_resume = False`.
- `lifecycle_from_proto(response) -> SandboxLifecycle | None`: `None` when
  `not response.HasField("lifecycle")`, which is the older-agent gate.
- `validate_set_timeout_seconds(timeout) -> int`:
  - int, not bool, `≥ 1`
  - `> 28800` → the beyond-cap `InvalidArgumentException`
- `connect_extension(lifecycle, requested, now_ms) -> tuple[mode, ms] |
  None`, as a table:

  | `lifecycle` | `requested` given | `requested is None` |
  |---|---|---|
  | `None` (older agent) | `LifecycleUnsupportedException` | `None` |
  | `unmanaged` | `InvalidArgumentException` naming `max_lifetime` | `None` |
  | `active` | `("at_least", requested*1000)` | `None` |
  | `resume_grace`/`expired` | `("at_least", requested*1000)` | `("at_least", min(timeout_ms, cap_ms − now_ms − 5000))`; below 1000 → `SandboxLifetimeException` pointing to `reincarnate()` |

- `beyond_cap_error(seconds, cap_unix_ms) -> InvalidArgumentException`,
  message «set_timeout({s}) supera el max_lifetime del sandbox (tope a las
  {cap iso}; max_lifetime se fija en create() y como mucho vale 28800 s):
  la vida de la plataforma no se puede extender (no existe UpdateMicrovm);
  usa reincarnate() para seguir con los ficheros en un sandbox nuevo».
- `older_agent_error(template, agent_version) ->
  LifecycleUnsupportedException`, message «la imagen {template}
  (agent_version {v}) no impone el timeout del servidor: publica una imagen
  M9 o crea el sandbox sin max_lifetime ni on_timeout».
- `pause_trigger_delay` (D7) and `SANDBOX_TIMEOUT_DETAIL =
  "sandbox_timeout"`.

**Models** (`_models.py`, exported from `rayito`):

```python
@dataclass(frozen=True)
class SandboxLifecycle:
    phase: Literal["unmanaged", "active", "resume_grace", "expired"]
    deadline: datetime | None
    cap: datetime | None
    timeout_seconds: float
    on_timeout: Literal["kill", "pause"] | None
    auto_resume: bool
    extensions: int
```

- `SandboxHealth.lifecycle: SandboxLifecycle | None = None`.
- `SandboxInfo`:
  - gains `lifecycle: SandboxLifecycle | None = None`
  - `expires_at` returns `lifecycle.deadline` when the lifecycle is managed
    and has a deadline, else `started_at + maximum_duration_seconds`
  - new `platform_expires_at` (always the platform cap)
  - `remaining_seconds()` follows `expires_at`
  - new `timed_out` property: `state_reason == "Container Stopped with
    Exit Code: 124"`, subject to D6's Q63 fallback
- `exceptions.py`: `LifecycleUnsupportedException(InvalidArgumentException)`,
  exported.

**Payload** (`_payload.py`): `build_run_hook_payload(..., lifecycle:
LifecycleBlock | None = None)` adds `"lifecycle": block.to_wire()` when
given.

**Launch** (`_sandbox_base.py`):

- `build_launch_plan(..., max_lifetime=None, on_timeout=None,
  default_max_lifetime=None)` calls `resolve_lifecycle` and sets
  `maximum_duration_seconds = plan.platform_duration`, `idle = plan.idle`,
  and the payload block.
- `LaunchPlan` gains `lifecycle_requested: bool`.
- `health_from_proto` fills `lifecycle`.
- `terminal_state_error(info)` says «alcanzó su timeout» when
  `info.timed_out`.

**`Sandbox` / `AsyncSandbox`** (`sandbox_sync/main.py`,
`sandbox_async/main.py`):

- **`create(...)`** gains:
  - `max_lifetime: int | None = None`
  - `on_timeout: Literal["kill","pause"] | None = None`
  - A private keyword-only `_default_max_lifetime: int | None = None`,
    used by the shim, not in the public docstring.
  - Docstring rules:
    - `timeout` is the logical deadline.
    - `max_lifetime` is `maximumDurationInSeconds`; the deadline never
      passes `max_lifetime − 60 s` since start.
    - Without both, ADR-007 applies.
- **Older-agent gate**:
  - `_open` gains `require_lifecycle: bool`.
  - After `_wait_until_ready`, a `Health` without `lifecycle` on a launch
    that sent a block raises `older_agent_error` inside the existing `try`.
    The VM is therefore terminated unless `keep_on_failure`, and the
    exception is never `SandboxNotReadyException`.
- **Pool interplay**: `reject_launch_kwargs_with_pool` includes
  `max_lifetime` and `on_timeout`. `LaunchOptions` records both, so
  `reincarnate()` reuses them.
- **`sbx.set_timeout(timeout, *, request_timeout=None) -> None`**:
  1. validate
  2. `LifecycleService.SetTimeout(EXACT, timeout*1000)` on the unary
     channel with `x-access-token`
  3. record the returned lifecycle, which re-arms the trigger
- **`Sandbox.set_timeout(sandbox_id, timeout, *, access_token=None,
  request_timeout=60.0, region=None, session=None, control_plane=None,
  transport=None) -> None`**: class variant through
  `class_method_variant("_class_set_timeout")`.
  1. `require_access_token`
  2. `get-microvm`:
     - terminal → `SandboxNotFoundException`
     - `SUSPENDING|SUSPENDED` → `SandboxStateException("el sandbox está
       suspendido: connect() lo reanuda")`, without waking it
  3. JWE for port 8080
  4. a dedicated channel, `SetTimeout(EXACT)`, close
- **`connect`**:
  - Becomes `class_method_variant("_class_connect")`, with the class form
    `Sandbox.connect(sandbox_id, *, timeout: int | None = None,
    access_token=…, …)` (existing kwargs kept).
  - New instance form `sbx.connect(*, timeout: int | None = None,
    request_timeout: float | None = None) -> Self`:
    1. `get-microvm`
    2. `resume-microvm` when `SUSPENDED` without platform auto-resume
    3. the readiness poll
    4. the extension below
    5. return `self`
  - Both forms, after readiness: apply `connect_extension(lifecycle,
    timeout, now)` and send `SetTimeout(AT_LEAST, ms)` when it returns a
    plan. They send it right after readiness returns, well inside the 30 s
    grace (resume p95 0.4 s, kernel probe ≤ 12 s).
- **`resume()`** (instance): after readiness, `connect_extension(lifecycle,
  None, now)`. It reopens a `resume_grace`/`expired` sandbox with its own
  timeout: an explicit token-holder action is intent.
- **`get_info()`** (instance):
  - `get-microvm`
  - when `RUNNING` and the handle's recorded lifecycle is managed
    (`deadline_may_have_moved`), one `Health` (recorded) to refresh
    `lifecycle`: only a managed deadline can move under the handle
    (another client's `set_timeout`/`connect(timeout=)`, or `rayd` on
    resume)
  - otherwise the last recorded ones with no extra RPC: `metadata` and the
    guest facts are fixed at `/run` (the `sandbox-metadata` and
    `sandbox-observability` "no extra RPC" rule), and a VM that is not
    `RUNNING` is never probed, so it is never woken
  - the docstring changes accordingly
  - decision recorded after review: Python and TypeScript SHALL use this
    same condition (TS `getInfo()` refreshing on every `RUNNING` is the
    deviation to align)
- **`Sandbox.get_info(id)`**: the existing `probe_metadata` returns the
  `HealthResponse`, and both `metadata` and `lifecycle` are filled.
- **Errors** (`_transport.py`):
  - `is_sandbox_timeout(exc)`: `FAILED_PRECONDITION` with details
    `sandbox_timeout`. It maps to `TimeoutException` and is checked before
    the generic `FAILED_PRECONDITION` → `InvalidArgumentException`.
  - `translate_stream_error("sandbox_timeout")` → `TimeoutException`.
  - `INVALID_ARGUMENT` "timeout beyond cap…" on `SetTimeout` →
    `beyond_cap_error`.
  - `FAILED_PRECONDITION` "lifecycle_unmanaged" →
    `InvalidArgumentException` naming `max_lifetime`.
  - `UNIMPLEMENTED` on `SetTimeout` → `LifecycleUnsupportedException`.
  - `is_reconnectable` stays false for all of them.
- **Stream ends** (`_process_base.py`, `_pty_base.py`):
  - `STATUS_SANDBOX_TIMEOUT = "sandbox_timeout"` in `outcome_from_end`
    raises `TimeoutException("el sandbox alcanzó su timeout
    (sandbox_timeout)")`.
  - `consumed_end` treats it as terminal, never as `suspending`.
- **Limits**: `_limits.py` is regenerated from `limits.json` (D10). The
  constants are imported by `_lifecycle_base.py`.

**Async**: the same names and signatures as coroutines. The class-variant
parity test (`test_sandbox_async.py`) lists `set_timeout` and `connect`.

### D9. Python E2B shim (`rayito.e2b`)

**Pure mapping** in `e2b/_compat.py`:

- `E2B_DEFAULT_MAX_LIFETIME_SECONDS = 3600`.
- `map_lifecycle(lifecycle, *, auto_pause=None) -> ShimLifecycle(on_timeout,
  auto_resume)`:
  - `lifecycle is None` and `auto_pause` not `True` → `("kill", False)`.
  - `auto_pause=True` → `("pause", False)`. Given together with
    `lifecycle` → `InvalidArgumentException`.
  - Keys outside `{"on_timeout","auto_resume"}` →
    `InvalidArgumentException` naming the key.
  - `on_timeout`:
    - `None` → `"kill"` (E2B leaves the API default, which is kill)
    - a string must be `"kill"` or `"pause"`
    - a dict must be `{"action": "kill"|"pause"}`, optionally with
      `"keep_memory"`, and is checked in this order:
      1. an invalid action →
         `InvalidArgumentException("on_timeout[\"action\"] debe ser 'pause'
         o 'kill' (recibido …)")`, or `"on_timeout debe ser …"` for the
         string form
      2. `keep_memory` with `"kill"` → `InvalidArgumentException`
      3. `keep_memory: False` with `"pause"` →
         `UnimplementedError("lifecycle.on_timeout.keep_memory=False",
         "suspend-microvm siempre guarda la memoria (AWS_API_NOTES.md §5);
         usa checkpoint_files() + kill()")`
  - `auto_resume: True` with anything but pause →
    `InvalidArgumentException("auto_resume sólo puede ser True con
    on_timeout='pause'")`. `None` is `False`.
- `map_create_kwargs(...)` gains `lifecycle` and `max_lifetime`. It always
  produces:
  - `on_timeout`
  - `timeout` (300 when `None`)
  - `_default_max_lifetime = max(3600, min(timeout + 60, 28800))` when
    `max_lifetime is None`, else `max_lifetime=` as given
  - `idle=None` for kill, and `IdlePolicy(max_idle_seconds=300,
    auto_resume=<auto_resume>)` for pause
  - egress and ingress unchanged

**`Sandbox.__init__` / `create` / `AsyncSandbox.create`** gain:

- `lifecycle: Mapping[str, Any] | None = None` (positional-or-keyword after
  `allow_internet_access`)
- `max_lifetime: int | None = None` (keyword-only, Rayito-only)

The gate: `LifecycleUnsupportedException` from the native call is
re-raised as `UnimplementedError("lifecycle", "la imagen no impone el
timeout del servidor: publica una imagen M9 (ADR-011)")`, chained. The
native call already terminated the VM.

Other methods:

- `beta_create(..., auto_pause=, network=, mcp=)`: `auto_pause` maps
  through `map_lifecycle`; `network`/`mcp` still raise.
- `set_timeout(timeout, request_timeout=None)` → `native.set_timeout`.
  `Sandbox.set_timeout(sandbox_id, timeout, request_timeout=None, **kw)` →
  `NativeSandbox.set_timeout(sandbox_id, timeout, **kw)` (`access_token=`
  or `RAYITO_ACCESS_TOKEN`).
- `Sandbox.connect(sandbox_id, timeout: int | None = None, *, …)`:
  `timeout` is positional after `sandbox_id`, as in E2B, and passes through
  to the native class `connect(timeout=)`. `LifecycleUnsupportedException`
  → `UnimplementedError`. The shim's instance `sbx.connect()` is
  `m9-e2b-v2-surface`'s.
- `info_from_native` does not change: `end_at` now reads the logical
  `expires_at`.
- `SET_TIMEOUT_REASON` and `BETA_CREATE_REASON` lose their timeout and
  `auto_pause` parts.
- The `rayito/e2b/__init__.py` docstring drops `set_timeout` from the
  unimplemented list.

### D10. TypeScript native mirror

**`limits.json` and `scripts/gen_limits.py`**:

- New keys: `lifecycleCapMarginSeconds: 60`,
  `lifecycleMinMaxLifetimeSeconds: 120`, `lifecycleResumeGraceSeconds: 30`,
  `lifecycleAutoResumeMinSeconds: 300`, `lifecycleMinTimeoutSeconds: 1`,
  `lifecycleTimeoutExitCode: 124`.
- A new `GROUPS` entry.
- `python scripts/gen_limits.py` regenerates `_limits.py` and `limits.ts`;
  `--check` is green.

**`src/sandbox/lifecycle.ts`** (new, pure): `resolveLifecycle`,
`lifecycleFromProto`, `validateSetTimeoutMs`, `connectExtension`,
`beyondCapError`, `olderAgentError`, `pauseTriggerDelayMs`,
`SANDBOX_TIMEOUT_DETAIL`. The rules are those of D8, in milliseconds:

- `maxLifetimeMs` must be an integer multiple of 1000, in 120 000..28 800 000.
  `maximumDurationInSeconds = maxLifetimeMs / 1000`.
- Default `maxLifetimeMs = min(max(timeoutMs + 60 000, 120 000),
  28 800 000)`, with `timeoutMs` rounded up to seconds first as today.

**`models.ts`**:

```ts
export interface SandboxLifecycle {
  readonly phase: "unmanaged" | "active" | "resumeGrace" | "expired";
  readonly deadline?: Date;
  readonly cap?: Date;
  readonly timeoutMs: number;
  readonly onTimeout?: "kill" | "pause";
  readonly autoResume: boolean;
  readonly extensions: number;
}
```

- `SandboxHealth.lifecycle?: SandboxLifecycle`.
- `SandboxInfo`:
  - `lifecycle?: SandboxLifecycle`
  - `expiresAt` is logical, as in D8
  - `platformExpiresAt`
  - `timedOut`, subject to the Q63 fallback
- `errors.ts`: `LifecycleUnsupportedError extends InvalidArgumentError`,
  exported from `index.ts` together with `SandboxLifecycle`.

**Options** (`sandbox/sandbox.ts`):

- `timeoutMs` moves from `SandboxCreateOptions` to `SandboxConnectOptions`.
  Its doc: in `create` it is the logical deadline; in `connect` it is
  AT_LEAST.
- `SandboxCreateOptions` gains `maxLifetimeMs?: number` and
  `onTimeout?: "kill" | "pause"`.
- New `SandboxSetTimeoutOptions extends ControlPlaneOptions {
  accessToken?; requestTimeoutMs?; transport?; logger? }`.

**Methods**:

- instance `setTimeout(timeoutMs: number, options: RequestOptions = {}):
  Promise<void>` (EXACT)
- static `setTimeout(sandboxId: string, timeoutMs: number, options:
  SandboxSetTimeoutOptions = {}): Promise<void>`
- instance `connect(options: { timeoutMs?: number; requestTimeoutMs?:
  number } = {}): Promise<Sandbox>` (returns `this`)
- static `connect(sandboxId, { timeoutMs, … })`
- `resume()` reopens as in D8
- `getInfo()` refreshes `Health` when `RUNNING` and the recorded lifecycle
  is managed, as in D8
- The pause trigger uses an unref'd `setTimeout`, cleared on `close()`,
  `kill()` and `[Symbol.asyncDispose]`.

The older-agent gate lives in `#open(..., requireLifecycle)` and raises
`LifecycleUnsupportedError` after terminating, unless `keepOnFailure`.

**Other files**:

- `src/payload.ts`: `buildRunHookPayload({ …, lifecycle })` serialises the
  same sorted JSON as Python. The golden cross-SDK payload test gains a
  lifecycle case.
- `src/sandbox/launch.ts`: `buildLaunchPlan` gains `maxLifetimeMs` and
  `onTimeout`.
- `src/sandbox/core.ts`: a `LifecycleService` client, next to the other
  stubs.
- `src/transport/errors.ts`: `isSandboxTimeout`, and the same table as D8
  → `TimeoutError`, `InvalidArgumentError`, `LifecycleUnsupportedError`.
- `src/sandbox/commands.ts` and `pty.ts`: status `sandbox_timeout` →
  `TimeoutError`, terminal.
- `src/sandbox/readiness.ts`: `healthFromProto` fills `lifecycle`.

### D11. Tests that fail without the change

**Rust, `rayd-core`** (Windows-runnable, `FakeClock`):

`sandbox_timeout::tests` (each asserts a rule of D4):

- `install_clamps_the_deadline_to_the_cap_margin`
- `unmanaged_rejects_set_timeout_and_never_ticks`
- `exact_moves_the_deadline_both_ways`
- `at_least_never_shortens`
- `beyond_cap_is_rejected_and_nothing_changes`
- `kill_mode_terminates_at_the_deadline`
- `pause_mode_expires_at_the_deadline`
- `suspended_time_counts_toward_the_deadline` (a monotonic jump across the
  deadline with `thawed = true` gives `ResumeGrace`, not `Terminate`)
- `grace_without_set_timeout_terminates_in_kill_mode`
- `grace_without_set_timeout_re_expires_in_pause_mode`
- `at_least_during_grace_reactivates`
- `auto_resume_applies_the_five_minute_minimum` (timeout 60 → now+300;
  timeout 900 → now+900; clamped at the cap)
- `an_unfrozen_resume_changes_nothing` (a forged pair)
- `a_forged_suspend_holds_the_deadline_once` (a second hold is never
  granted)
- `exact_reopens_an_expired_pause_sandbox`
- `set_timeout_is_refused_once_terminating`
- `view_converts_instants_with_the_wall_clock`
- `expiry_admits_only_health_and_set_timeout`
- `constants_match_limits_json` (`include_str!("../../../../limits.json")`)

Other `rayd-core` tests:

- `run_payload::tests::lifecycle_is_optional_and_validated` (absent;
  valid kill/pause; each rule of D3 rejected; unknown `on_timeout`;
  messages never quote values)
- `session::tests::run_installs_the_lifecycle_and_health_reports_it`
- `session::tests::health_reports_unmanaged_without_a_block`

**Rust, `rayd` integration test `crates/rayd/tests/m9_timeout.rs`**
(`cfg(unix)`, shrunk `TimeoutSettings`). It uses a test clock
`JumpClock`, a `SystemClock` plus an atomic offset in `tests/common`, to
simulate a freeze.

- `set_timeout_requires_the_access_token`
- `set_timeout_beyond_cap_is_invalid_argument_and_keeps_the_deadline`
- `health_reports_the_lifecycle_after_run_and_unmanaged_without_it`
- `pause_mode_expiry_closes_streams_and_gates_rpcs`:
  - `Start` → `EndEvent{status:"sandbox_timeout"}`
  - `Pty.Create` → `PtyExited{status:"sandbox_timeout"}`
  - `WatchDir` and `Execute` → `FAILED_PRECONDITION sandbox_timeout`
  - a new `Start` → `FAILED_PRECONDITION`
  - `Health` OK
  - `SetTimeout(EXACT)` reopens and `Start` works again
- `kill_mode_signals_the_workload_and_exits`:
  - the real `ExitTerminator` with `force` replaced by a recording closure
  - a `trap 'echo TERM > <tmp>/term' TERM; sleep 30` process writes its
    marker
  - the shutdown token is cancelled
  - `ExitReason` is `SandboxTimeout`
  - the sidecar exited
- `resume_after_a_freeze_opens_the_grace_and_at_least_reactivates`
  (hooks `/suspend`, clock jump, `/resume`)
- `grace_without_set_timeout_ends_the_agent`
- `forged_suspend_holds_the_deadline_only_once` (repeated `/suspend`
  without a jump; the terminator fires within `suspend_hold + tick`)
- `suspend_still_answers_200_after_expiry`

**Python unit tests** (fail without the change):

- `tests/unit/test_lifecycle_base.py`:
  - `test_no_block_without_max_lifetime_or_on_timeout` (payload has no
    `lifecycle`; `maximumDurationInSeconds == timeout`)
  - `test_kill_block_and_platform_duration`
  - `test_default_max_lifetime_is_timeout_plus_margin`
  - `test_pause_requires_idle`
  - `test_pause_forces_platform_auto_resume_and_keeps_the_logical_flag`
  - `test_pause_rejects_an_explicit_suspended_duration`
  - `test_max_lifetime_bounds`
  - `test_connect_extension_table`
  - `test_pause_trigger_delay`
  - `test_beyond_cap_message_names_max_lifetime_and_28800`
  - `test_lifecycle_from_proto_is_none_on_an_older_agent`
- `tests/unit/test_lifecycle_sync.py` and `test_lifecycle_async.py`, over
  the new `tests/unit/fake_lifecycle.py` servicer (records requests;
  scripted `LifecycleState`), registered in the fake server of
  `tests/unit/conftest.py`, with `fake_health` gaining a `lifecycle`
  script:
  - `test_set_timeout_sends_exact`
  - `test_class_set_timeout_uses_the_access_token_and_refuses_suspended`
  - `test_connect_timeout_sends_at_least`
  - `test_connect_reopens_an_expired_sandbox_with_its_own_timeout`
  - `test_resume_reopens_a_grace_sandbox`
  - `test_create_on_an_older_agent_terminates_and_raises`
  - `test_sandbox_timeout_stream_end_raises_timeout_exception` (process
    and PTY)
  - `test_failed_precondition_sandbox_timeout_is_timeout_exception`
  - `test_unmanaged_set_timeout_raises_invalid_argument`
  - `test_pause_trigger_suspends_only_when_expired_and_running`
  - `test_get_info_expires_at_is_the_logical_deadline`
  - `test_pool_rejects_lifecycle_kwargs`
- `test_e2b_compat_base.py`:
  - `test_lifecycle_mapping_table`
  - `test_default_max_lifetime_is_3600_or_timeout_plus_margin`
  - `test_keep_memory_false_is_unimplemented`
- `test_e2b_compat_sync.py`/`_async.py`:
  - `test_e2b_defaults_on_the_wire` (updated: `maximumDurationInSeconds
    == 3600` and the payload carries `lifecycle {timeout_s:300,
    cap_s:3600, on_timeout:"kill", auto_resume:false}`)
  - `test_set_timeout_maps_to_native_exact`
  - `test_class_set_timeout`
  - `test_connect_timeout_is_at_least`
  - `test_beta_create_auto_pause_is_lifecycle_pause`
  - `test_older_image_raises_unimplemented_and_terminates`
  - the old `test_set_timeout_unimplemented` is removed

**TypeScript unit tests**:

- `tests/unit/lifecycle.test.ts` (the pure mirror)
- additions to `sandbox.test.ts` (`setTimeout` static and instance,
  `connect({timeoutMs})` static and instance, the older-agent gate, the
  trigger), `commands.test.ts`/`pty.test.ts` (`sandbox_timeout` →
  `TimeoutError`), `transport-errors.test.ts`, `payload.test.ts`,
  `launch.test.ts`
- the fakes gain `tests/unit/fake/lifecycle.ts` and a `lifecycle` script
  in `fake/health.ts`

**Docs gate** `scripts/tests/test_lifecycle_docs.py`:

- `test_set_timeout_is_no_longer_a_non_goal` (SPEC §4 and `project.md`
  rule 3 do not name `set_timeout`)
- `test_adr_007_is_superseded_by_adr_011`
- `test_t2_names_the_timeout_exit` (T2 contains `m9-server-timeout`,
  `SetTimeout` and `auto-DoS`, and still names `0.0.0.0:9000`)

### D12. Real-AWS acceptance

**Python**, `clients/python/tests/e2e/test_m9_server_timeout.py`:

- Marker `e2e`. Every sandbox has `max_lifetime ≤ 900` (conftest
  guardrail), runs without an execution role, and is terminated in
  teardown.
- The image is the M9 `rayito-base` from `RAYITO_TEMPLATE`.
- The pre-M9 version comes from `RAYITO_E2E_PRE_M9_TEMPLATE_VERSION`.
  Placeholders only in tracked files.
- Each test prints the values its `AWS_API_NOTES.md` row needs.

| # | Test | Asserts | Acceptance item |
|---|---|---|---|
| 1 | `test_kill_mode_ends_the_vm_after_client_death` (child `python -c` script creates with `timeout=60, max_lifetime=900, on_timeout="kill", idle=None`, prints id; parent kills it, polls `get-microvm` at 1 s) | `TERMINATED` by `started + 60 + 45 s`; prints `stateReason`, overrun = `terminatedAt − deadline` (**Q63**) | 1 |
| 2 | `test_set_timeout_extends_and_shortens` | A: `timeout=60`, `set_timeout(150)` at ≈5 s, `commands.run("echo alive")` at 90 s, `TERMINATED` by ≈200 s; B: `set_timeout(10)` → `TERMINATED` within 10 + 45 s | 2 |
| 3 | `test_set_timeout_beyond_cap` | `max_lifetime=900`: `set_timeout(2000)` raises `InvalidArgumentException` with `max_lifetime` and `28800` in the message; `get_health().lifecycle.deadline` unchanged | 3 |
| 4 | `test_connect_timeout_is_at_least` | remaining ≈600 stays ≈600 (±10 s) after `connect(id, timeout=300)`; remaining ≈30 becomes ≈300 | 4 |
| 5 | `test_open_streams_end_with_sandbox_timeout` (kill, `timeout=45`) | a background `sleep 600` handle's `wait()` and a PTY's `wait()` raise `TimeoutException` | 5 |
| 6 | `test_resume_grace_and_stray_auto_resume` | kill mode, `IdlePolicy(auto_resume=False)`: `pause()`, wait past the deadline, `connect(id, timeout=120)` → `run_code` works, lifecycle `active`; second VM with `auto_resume=True`: pause, wait past the deadline, one raw anonymous `Health` through a boto3-minted JWE → `TERMINATED` within 30 + 45 s (**Q65**) | 6 |
| 7 | `test_pause_mode_live_client_auto_resume` (`on_timeout="pause", idle=IdlePolicy(auto_resume=True), timeout=60`, kernel `x = 42`) | `SUSPENDED` within deadline + 15 s; next `files.read` auto-resumes; `run_code("x").text == "42"`; new deadline = resume + max(60, 300) ± 10 s (**Q64** auto-resume timing) | 7 |
| 8 | `test_pause_mode_dead_client` (child creates pause mode, dies) | `SUSPENDED` within deadline + 300 + 60 s (**Q64** idle-after-deadline timing) | 8 |
| 9 | `test_pause_mode_without_auto_resume` | after the deadline pause, a raw `ProcessService.Start` with the access token (not `connect`) gets `FAILED_PRECONDITION sandbox_timeout`; VM `SUSPENDED` again within max_idle + 60 s; `connect(id)` reopens and `run_code("1+1")` works | 9 |
| 10 | `test_lifecycle_tracks_every_set_timeout` | three `set_timeout`; each time `get_health().lifecycle.deadline` and `get_info().expires_at` agree within 2 s with `now + t`; `extensions == 3` | 10 |
| 11 | `test_older_agent_gate` (pre-M9 version) | shim `Sandbox.create(template_version=<pre-M9>)` raises `UnimplementedError`; VM `TERMINATED`; native `create(timeout=300)` on the same version runs `echo ok` | 11 |
| 12 | `test_e2b_shim_timeout` | `Sandbox.create(timeout=30)` → `set_timeout(90)` at ≈5 s → alive at 60 s → gone by ≈140 s; `lifecycle={"on_timeout":"pause","auto_resume":True}` → native lifecycle `pause`/`auto_resume`, platform idle `autoResumeEnabled`; `beta_create(auto_pause=True)` → lifecycle `pause` | 12 |

Measurements, recorded as rows of `AWS_API_NOTES.md` §16 with
placeholders only (account 123456789012, `microvm-<id>`):

- **Q63**, from test 1: the `stateReason` with exit code 124 and whether
  the platform behaves as with 0. It decides D6's fallback.
- **Q64**, from tests 7 and 8: the idle-triggered suspend after a deadline
  and the auto-resume latency after a deadline pause.
- **Q65**, from test 6: a stray auto-resume in kill mode, then grace, then
  `TERMINATED` timing.

**TypeScript**, `clients/typescript/tests/e2e/timeout.e2e.test.ts` (the
native mirror):

- `setTimeout` extends and shortens (the `setTimeoutMs(10 000)` leg only)
- `connect({ timeoutMs })` AT_LEAST
- `onTimeout: "pause"` with a live client suspends and auto-resumes
- a stream at the deadline rejects with `TimeoutError`
- beyond the cap → `InvalidArgumentError` naming `maxLifetime`

The TS E2B shim mirror of test 12 belongs to `m9-e2b-v2-surface` (see
Non-Goals).

Expected cost: about 14 VMs × ≤ 8 min at 2 GB ≈ $0.25, plus one image
version.

### D13. Docs to touch

Each item says the exact change.

- **`ARCHITECTURE.md`**:
  - ADR-007 superseded note, ADR-011 (D1), ADR-009 consequence (4).
  - "Servicios": a `LifecycleService` row, and `HealthService` field 12.
  - "Lifecycle hooks" table:
    - `/run` installs the lifecycle
    - `/resume` calls `timeout_resumed`
    - `/suspend` still 200
  - "Suspend / resume": the close-form table gains a `sandbox_timeout`
    column.
  - Hexagonal tables:
    - the `sandbox_timeout` module and the `SelfTerminator` port
    - the adapters `timeout_watcher`, `exit_terminator`, `timeout_gate`
- **`SPEC.md`**:
  - §1: the paragraph under the example becomes «`timeout` es el plazo
    lógico del sandbox, impuesto por `rayd` y movible con `set_timeout()`
    / `connect(timeout=)`; `max_lifetime` es el tope de la plataforma
    (running + suspendido, ≤ 8 h), fijo tras crear».
  - §3: a row «Ciclo de vida (M9)» with `set_timeout`, `connect(timeout=)`,
    `on_timeout`, `SandboxLifecycle`.
  - §4: the `set_timeout()` bullet is **deleted**.
  - §5: the row «Vida del sandbox» becomes «plazo lógico en `rayd` +
    `maximumDurationInSeconds` ≤ 28800 como tope (ADR-011)».
  - §7: the bullet «Tope duro de 8 h» keeps the cap and drops «sin
    `set_timeout`, `connect()` no extiende la vida».
- **`openspec/project.md`**: hard rule 3's list drops «no `set_timeout`».
- **`SECURITY.md` T2**: append the paragraph of D14 to the mitigation
  cell, keeping every existing sentence.
- **`AWS_API_NOTES.md`**: §16 rows Q63, Q64 and Q65 (D12). No parameter
  table changes.
- **`docs/site/docs/e2b-compat.md`**:
  - The `timeout` mapping row becomes «plazo lógico impuesto por `rayd`
    bajo `max_lifetime` (3600 por defecto, ≤ 28800)».
  - New mapped rows: `set_timeout`, `connect(timeout)`,
    `lifecycle={on_timeout, auto_resume}`, `beta_create(auto_pause=True)`,
    `TimeoutException`, and «exige una imagen M9».
  - Unimplemented table:
    - removes `set_timeout`
    - `beta_create` keeps only `network=, mcp=`
    - adds `lifecycle.on_timeout.keep_memory=False`
  - A "Costes honestos" note.
- **`docs/site/docs/concepts.md`**: «Vida del sandbox vs. política de
  idle» is rewritten for deadline vs cap vs idle.
- **`limits.md`**: the life row gains `max_lifetime` and the 60 s margin.
- **`cost.md`**: an orphan sandbox with `on_timeout='kill'` bills until
  `timeout` + ≈15 s (Q58) instead of `max_lifetime`.
- **`persistence.md`**: the section title becomes «`reincarnate()`: más
  allá de `max_lifetime`», and the sentence about
  `rayito.e2b.Sandbox.set_timeout` is replaced.
- **`README.md`** and **`clients/python/README.md`**: the shim snippet's
  `sbx.set_timeout(600)` line becomes a working call, and the unimplemented
  lists drop `set_timeout` and `beta_create(auto_pause=...)`.
- **`clients/typescript/README.md`**: `setTimeout` and `onTimeout` in the
  lifecycle section.
- **`docs/site/docs/api.md`**: add `SandboxLifecycle` and
  `LifecycleUnsupportedException` if members are listed explicitly.
- **CHANGELOGs**: `clients/python/CHANGELOG.md`,
  `clients/typescript/CHANGELOG.md` and `crates/rayd/CHANGELOG.md` if it
  exists, under `[Unreleased]`: Added, and "Changed (shim)" (M9 image
  required, `maximumDurationInSeconds` = `max_lifetime`).

### D14. Security

**T2 paragraph** to append (Spanish, exact):

> **M9 (`m9-server-timeout`, ADR-011)**: `rayd` sale por su cuenta al vencer
> el plazo lógico con `on_timeout='kill'` y la VM pasa a `TERMINATED` ≈ 15 s
> después (`AWS_API_NOTES.md` Q58). Un `/terminate` forjado desde uid 1000
> tiene sobre el propio sandbox el mismo efecto que ese vencimiento
> —auto-DoS, sin ganancia de acceso— y no alcanza a ningún otro sandbox.
> `SetTimeout` exige `x-access-token`, así que el código del sandbox no puede
> alargar su propio plazo; un `/suspend` forjado al vencer lo retiene como
> mucho 20 s y una sola vez por vencimiento, y un par `/suspend` + `/resume`
> forjado no abre la gracia de 30 s ni aplica la regla de 5 min del
> auto-resume: ambas exigen que el vigilante del plazo haya visto un salto de
> `CLOCK_MONOTONIC` ≥ 2 s, la firma de un checkpoint real. La autenticación
> de `/terminate` y `/validate` por el uid del par sigue pendiente (C-01) y
> M9 no depende de ella.

**Deferred rows** (`docs/SECURITY_AUDIT.md` §8), none regressed:

- C-01: no new hook, and no reliance on hook authentication.
- C-02 and C-03: `/validate` and `/ready` are untouched.
- C-07: no S3.
- T1: no execution role, and no IMDS use.

**Other security points**:

- `Health.lifecycle` is not secret (T4 already accepts that uid 1000 reads
  `Health`).
- Logging follows D6. The SDK never logs the access token or JWE in the
  trigger's warnings.

### D15. Gates

All green, output recorded in `tasks.md`:

- `cargo fmt --all --check`
- `cargo clippy --workspace --all-targets -- -D warnings` (pedantic,
  unwrap/expect/panic denied outside tests)
- `cargo test --workspace --locked`
- `cargo zigbuild --release --target aarch64-unknown-linux-musl -p rayd`
- `cd clients/python && uv run pytest tests/unit && uv run ruff check .
  && uv run ruff format --check . && uv run mypy src tests`
- `cd clients/typescript && pnpm lint && pnpm typecheck && pnpm test &&
  pnpm build`
- `buf lint`
- `python scripts/gen_limits.py --check`
- `python scripts/check_pins.py`
- `python scripts/check_license.py`
- `python scripts/check_hygiene.py`
- `cd scripts && uv run --with pytest pytest tests` (or the repo's runner)
- `openspec validate m9-server-timeout --strict --no-interactive`

### D16. Coordination with sibling changes

- `e2b-compat` requirements "E2B create kwargs map to Rayito or warn" and
  "E2B features without an AWS primitive raise UnimplementedError" are also
  MODIFIED by `m9-deno-kernels`, `m9-file-transfer`, `m9-egress-policy` and
  `m9-sandbox-observability`. This delta changes only its own clauses and
  keeps every other clause verbatim. Whichever change archives later
  re-applies its clauses onto the archived text (a task in `tasks.md`).
- The `StreamError` comment list is shared with `m9-file-transfer`. The
  Contract agent merges both.
- `HealthResponse` field 12 is ours. Fields 13, 14 and 15 belong to the
  siblings.
- `m9-e2b-v2-surface` consumes:
  - the native `SandboxInfo.lifecycle` / `SandboxLifecycle`
  - `LifecycleUnsupportedException` / `LifecycleUnsupportedError`
  - native `connect(timeout=)` / `setTimeout`
  - `map_lifecycle` (for the TS shim's port and for
    `pause(keep_memory=False)`)

## Honest costs

Each of these is written into ADR-011, `e2b-compat.md` and `concepts.md`.

1. The cap still counts suspended time: at most 8 h since start. E2B keeps
   paused sandboxes indefinitely.
2. In pause mode the sandbox's processes keep running between the deadline
   and the suspension: about 3 s with a live client, and up to `max_idle`
   (300 s by default) with none.
3. Pause mode suspends on idle before the deadline, so detached jobs freeze
   after `max_idle` without client traffic.
4. About 15 s of 502 is billed after a kill-mode exit (Q58).
5. The deadline never passes `max_lifetime − 60 s` since start (the `/run`
   margin).
6. In kill mode with an idle policy, a sandbox suspended across its
   deadline without anyone resuming it is removed only by the platform's
   `suspendedDurationSeconds` (= `max_lifetime − max_idle`) or cap, at
   snapshot-storage cost. It ends 30 s after any resume. The shim uses no
   idle in kill mode, so this is native-only.
7. A real suspension shorter than 2 s that straddles the deadline is not
   recognised as a freeze. In kill mode the sandbox then ends as if it had
   not been suspended; in `auto_resume=True` pause mode it stays `EXPIRED`
   until `connect()`, except for the client whose trigger suspended it,
   which reopens it itself (D7).
8. A sandbox that starves the watcher thread of CPU right at its deadline
   can fake a thaw and get the deadline's one resume grace (30 s, never
   past the cap) without a real checkpoint; in `auto_resume=True` pause
   mode, the same fake plus a forged `/suspend` + `/resume` pair applies
   the five-minute rule, once per deadline and never past the cap. Both
   need code inside the sandbox, and neither passes the cap.

## Risks / Trade-offs

- **Exit code 124 could change platform behaviour** (a restart, or a longer
  502). Mitigation: measured first (Q63), with a written fallback to 0 (D6).
- **The watcher thread runs every 500 ms for the life of a managed
  sandbox.** The CPU cost is negligible and it is not endpoint traffic, so
  idle is unaffected (Q15).
- **Forged hooks around the deadline.** They are bounded by D4's hold and
  freeze rules (at most 20 s once, plus a 30 s grace that forged hooks
  alone cannot open). CPU starvation can fake the freeze signature, so the
  grace is limited to one per deadline and clamped to the cap (D4). A
  forged `/terminate` equals the deadline.
- **Shim users on old images now fail closed.** This is intended (never an
  unenforced timeout) and documented as a breaking change. The native API
  stays backward compatible.
- **Several changes MODIFY the same e2b-compat requirements.** Handled by
  D16's re-apply rule.

## Migration Plan

- The M9 `rayd` is published as a new `rayito-base` version. Older
  versions keep working for native callers that do not ask for a
  lifecycle.
- The shim requires the M9 image.
- Rollback: re-activate the previous image version. The SDK then fails
  closed on lifecycle requests, which is safe.

## Open Questions

None. The only measurement-dependent choice, the exit code, is closed in D6
with an explicit fallback.

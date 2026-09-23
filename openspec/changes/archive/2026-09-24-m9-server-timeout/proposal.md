## Why

E2B's `timeout` is a server-enforced deadline that the client can move:
`set_timeout()` sets it exactly (it can also shorten it), `connect(timeout=)`
only extends it, and `lifecycle={"on_timeout": "kill"|"pause",
"auto_resume": bool}` picks what happens when it fires. Rayito 0.2.0 has none
of that. ADR-007 made the MicroVM's `maximumDurationInSeconds` the sandbox
life: it is immutable because Lambda MicroVMs has no `UpdateMicrovm`
(`AWS_API_NOTES.md` §1). So `rayito.e2b` raises `UnimplementedError` for
`set_timeout`, `connect()` does not extend anything, and SPEC §4 lists
`set_timeout` as a non-goal. It is the most visible difference with E2B, and
the README says so.

A new fact makes it possible. Q58 (measured 2026-09-22, `AWS_API_NOTES.md`
§16) showed that when `rayd` (PID 1, the image `CMD`) exits, the MicroVM
answers 502 at once and is `TERMINATED` about 15 s later
(`stateReason` "Container Stopped with Exit Code: 0"), with no control-plane
credentials involved. So an agent that owns a logical deadline can end its
own VM, with no IAM, no idle policy and no change to the "`/suspend` always
answers 200" invariant. The platform cap (`maximumDurationInSeconds`, at most
28800 s, suspended time included) stays: it becomes a ceiling that the
caller picks at `create()`, and the movable deadline lives in `rayd`.

## What Changes

- **ADR-011** "Plazo lógico impuesto por `rayd`; el tope de la plataforma se
  elige en `create()`" supersedes ADR-007. SPEC §4 loses the `set_timeout`
  non-goal, and `openspec/project.md` hard rule 3 loses "no `set_timeout`".
- **rayd-core**: a new `sandbox_timeout` module, a pure state machine on the
  monotonic clock (suspended time counts, like E2B's wall-clock `end_at`).
  - State: timeout, deadline, cap (`cap_s` − 60 s), policy
    `{on_timeout: kill|pause, auto_resume}`, phase
    `UNMANAGED|ACTIVE|RESUME_GRACE|EXPIRED`.
  - Operations: `install`, `set_timeout(EXACT|AT_LEAST)`, `tick`,
    `resumed(frozen)`.
  - A `SelfTerminator` port.
  - The hook phase machine in `lifecycle.rs` is untouched.
- **rayd adapters**:
  - A dedicated `std::thread` watcher that parks between 500 ms ticks and
    does not depend on the tokio runtime.
  - An `ExitTerminator` for kill mode: close the open streams with
    `sandbox_timeout`, SIGTERM the process groups, PTYs, kernels and the
    sidecar, SIGKILL 5 s later, drain 2 s, then exit. The exit code is 124,
    confirmed by the Q63 measurement, with a written fallback to 0.
  - A tower gate that answers `FAILED_PRECONDITION` `sandbox_timeout` to
    every RPC except `Health` and `SetTimeout` while the phase is
    `RESUME_GRACE` or `EXPIRED`.
  - A new `LifecycleService.SetTimeout` handler, which requires
    `x-access-token`.
  - `Health.lifecycle` (field 12).
- **Kill mode at the deadline**: the agent exits. The VM is gone about 15 s
  later (Q58).
- **Pause mode at the deadline**:
  - `rayd` moves to `EXPIRED`.
  - A live SDK re-reads `Health` at the deadline it was told and calls
    `SuspendMicrovm`.
  - With the client gone, the idle policy suspends the VM within
    `max_idle`, which is why pause mode always launches with an idle policy.
  - After a resume that follows the deadline:
    - `auto_resume=True` applies E2B's rule:
      `deadline = now + max(timeout, 300 s)`.
    - `auto_resume=False` keeps the sandbox refused until `connect()`
      sends `SetTimeout(AT_LEAST)`.
- **Resume after the deadline**, any mode except `auto_resume=True` pause:
  a 30 s `RESUME_GRACE` for `connect()`. With no `SetTimeout` in that time,
  kill mode exits and pause mode goes back to `EXPIRED`.
- **Wire**:
  - The `runHookPayload` gains an optional block
    `"lifecycle":{"timeout_s":N,"cap_s":M,"on_timeout":"kill"|"pause","auto_resume":bool}`;
    `v` stays 1.
  - New file `proto/rayito/v1/lifecycle.proto`: `LifecycleService`,
    `SetTimeoutRequest`, `LifecycleState` and three enums.
  - `HealthResponse.lifecycle = 12`.
  - `StreamError` / `EndEvent` comments gain `sandbox_timeout`.
  - The Contract agent applies all proto edits (design D2).
- **Python SDK** (sync and async identical; TS mirrors it in camelCase):
  - `create()` gains `max_lifetime=` and `on_timeout=`.
  - New `sbx.set_timeout()` and `Sandbox.set_timeout(id, t,
    access_token=)`.
  - `connect(id, timeout=)`, plus an instance form `sbx.connect(timeout=)`.
  - New `SandboxLifecycle` model; `get_info().expires_at` becomes the
    logical deadline.
  - `sandbox_timeout` raises `TimeoutException`.
  - Beyond the cap: `InvalidArgumentException` naming `max_lifetime` and
    28800.
  - A fail-closed gate on older agents: `LifecycleUnsupportedException`.
  - Backward compatible: without `max_lifetime` and `on_timeout` nothing
    changes on the wire.
- **E2B shim**:
  - `timeout=300` becomes a real deadline, under a Rayito-only
    `max_lifetime` (default 3600, at most 28800).
  - `lifecycle={...}` with E2B's validation.
  - `set_timeout` and `connect(timeout=)` are mapped.
  - `beta_create(auto_pause=True)` is lifecycle pause.
  - On an image older than M9, the shim terminates the VM and raises
    `UnimplementedError`, instead of running an unenforced timeout.
- **Docs**: ADR-011, SPEC §1/§3/§4/§5/§7, `project.md`, SECURITY T2,
  `AWS_API_NOTES.md` §16 rows Q63–Q65, `e2b-compat.md`, `concepts.md`,
  `limits.md`, `cost.md`, `persistence.md`, both READMEs and the
  CHANGELOGs.

## Capabilities

### New Capabilities

- `sandbox-timeout`: the logical deadline end to end, covering:
  - the payload block, the domain rules and the watcher
  - kill and pause behaviour at the deadline, the resume grace and E2B's
    auto-resume rule
  - `LifecycleService.SetTimeout`, `Health.lifecycle` and the RPC gate
  - the SDK surface in Python and TypeScript, the error mapping and the
    older-agent gate
  - logging hygiene and the real-AWS acceptance

### Modified Capabilities

- `e2b-compat`:
  - "E2B create kwargs map to Rayito or warn": `timeout` becomes the
    deadline under `max_lifetime`, and `lifecycle` is new.
  - "E2B features without an AWS primitive raise UnimplementedError":
    `set_timeout` and `beta_create(auto_pause=)` leave the list, and
    `keep_memory: False` joins it.
  - "E2B-shaped models on the instance": `end_at` is the logical deadline.
- `suspend-resume`: "SDK pause, resume and connect": `connect(timeout=)`
  extends the deadline, and `resume()` reopens an expired sandbox.
- `process-lifecycle`: "SDK stream error contract": `FAILED_PRECONDITION`
  `sandbox_timeout` maps to `TimeoutException`, not
  `InvalidArgumentException`.
- `typescript-sdk`: "Sandbox lifecycle surface": `maxLifetimeMs`,
  `onTimeout`, `setTimeout`, and `connect({ timeoutMs })` static and
  instance.
- `architecture-docs`: ADDED "ADR-011 records the server-enforced logical
  deadline".
- `security-docs`: ADDED "SECURITY.md T2 states the timeout exit and bounds
  forged hooks around the deadline".

## Impact

- **Proto** (Contract agent):
  - new `proto/rayito/v1/lifecycle.proto`
  - `health.proto` field 12
  - comments in `common.proto`, `process.proto` and `pty.proto`
  - regenerated `crates/rayito-proto/build.rs` (`PROTO_FILES` +=
    `"lifecycle"`)
  - regenerated `clients/python/src/rayito/v1/lifecycle_pb2*` and
    `health_pb2*`
  - regenerated `clients/typescript/src/gen/rayito/v1/lifecycle_pb.ts` and
    `health_pb.ts`
- **rayd**:
  - `crates/rayd-core/src/{sandbox_timeout/, run_payload.rs, session.rs,
    health.rs, lib.rs}`
  - `crates/rayd/src/{lifecycle/, grpc/, hooks/mod.rs, process/manager.rs,
    code/, adapters/sidecar_process.rs, main.rs}`
  - new integration test `crates/rayd/tests/m9_timeout.rs`
- **Sidecar**: no code change (its SIGTERM handler already stops it).
- **Python**:
  - new `clients/python/src/rayito/_lifecycle_base.py`
  - `_models.py`, `_payload.py`, `_sandbox_base.py`, `_transport.py`,
    `_process_base.py`, `exceptions.py`, `__init__.py`
  - `sandbox_sync/{main,pool}.py`, `sandbox_async/{main,pool}.py`
  - `e2b/{_compat,_sync,_async,__init__}.py`
  - `_limits.py`, regenerated from `limits.json`
  - unit tests and the e2e `tests/e2e/test_m9_server_timeout.py`
- **TypeScript**:
  - new `src/sandbox/lifecycle.ts`
  - `models.ts`, `payload.ts`, `errors.ts`, `index.ts`, `limits.ts`
  - `sandbox/{launch,sandbox,core,readiness,commands,pty}.ts`,
    `transport/errors.ts`
  - fakes, unit tests and `tests/e2e/timeout.e2e.test.ts`
- **Infra**:
  - one new `rayito-base` version published with the M9 `rayd`
  - no IAM change: the caller already has `SuspendMicrovm`
  - no execution role
- **Scripts**:
  - `limits.json` and `scripts/gen_limits.py` (new keys)
  - `scripts/tests/test_lifecycle_docs.py` (new docs gate)
- **Behaviour change for shim users**: `rayito.e2b` sandboxes now need an
  M9 image (fail closed), and their `maximumDurationInSeconds` becomes
  `max_lifetime` (3600 by default) instead of `timeout`. The logical
  deadline is still `timeout`.

# sandbox-timeout Specification

## Purpose
TBD - created by archiving change m9-server-timeout. Update Purpose after archive.

## Requirements

### Requirement: The run payload carries an optional, validated lifecycle block
The `runHookPayload` SHALL accept an optional object `lifecycle` with exactly the keys `timeout_s`, `cap_s`, `on_timeout` and `auto_resume`, while `v` stays `1`. `rayd` SHALL require all four keys inside the block, `timeout_s` in `1..=28800`, `cap_s` in `120..=28800`, `timeout_s <= cap_s`, `on_timeout` equal to the string `"kill"` or `"pause"`, and `auto_resume == true` only with `"pause"`. Any violation SHALL be a `RunPayloadError::InvalidLifecycle` whose message names the violated rule and never quotes a value, and SHALL leave the agent token-less exactly like an invalid `limits` block does. A payload without the block SHALL install no lifecycle (phase `UNMANAGED`). The SDKs SHALL serialise the block with sorted keys and SHALL enforce the same rules before calling `run-microvm`.

#### Scenario: valid kill and pause blocks
- **WHEN** `parse_run_payload` reads `{"v":1,"token_sha256":…,"lifecycle":{"auto_resume":false,"cap_s":900,"on_timeout":"kill","timeout_s":60}}`, and the same block with `"on_timeout":"pause","auto_resume":true`
- **THEN** both parse with `lifecycle = Some(LifecycleSpec{timeout: 60 s, cap: 900 s, …})` and the policy each asked for

#### Scenario: each rule rejected without quoting
- **WHEN** the block has `timeout_s` 0, `cap_s` 119, `cap_s` 28801, `timeout_s` greater than `cap_s`, `on_timeout` `"freeze"`, `auto_resume` true with `"kill"`, or a missing key
- **THEN** each parse fails with `InvalidLifecycle`, and no error message contains `freeze` or `token`

#### Scenario: absent block
- **WHEN** the payload carries no `lifecycle`
- **THEN** the session's lifecycle phase is `UNMANAGED`

### Requirement: rayd keeps a logical deadline on the monotonic clock, bounded by the cap
`rayd-core` SHALL model the deadline in a module `sandbox_timeout` with no tokio, tonic or axum types. The module SHALL NOT change the hook phase machine of `lifecycle.rs`.

- Instants are raw monotonic readings of the `Clock` port, so time spent suspended counts toward the deadline.
- At the accepted `/run` it SHALL set `cap = now + cap_s − 60 s` and `deadline = min(now + timeout_s, cap)`, with phase `ACTIVE`.
- A dedicated `std::thread` watcher, independent of the tokio runtime, SHALL evaluate the deadline every 500 ms.
- The watcher SHALL tell a thaw from normal time: a gap of at least 2 s between two of its ticks means the VM was frozen.
- When the deadline passes while the hook phase is `Suspending` and no thaw was seen, the deadline SHALL wait at most 20 s. That hold SHALL be granted at most once per deadline.

#### Scenario: suspended time counts
- **WHEN** a unit test installs `timeout 60 s` at monotonic 0 and the fake clock jumps to 90 s with `thawed = true`
- **THEN** the phase becomes `RESUME_GRACE` and no terminate action is returned

#### Scenario: the cap bounds the initial deadline
- **WHEN** a lifecycle with `timeout_s = cap_s = 900` is installed at monotonic 10 s
- **THEN** the deadline is 850 s and the cap is 850 s

#### Scenario: a forged suspend delays the deadline only once
- **WHEN** the deadline passes while the phase is `Suspending` without any thaw, and a second forged `/suspend` arrives after the hold expired
- **THEN** the deadline acts 20 s after it passed (within one tick), not later

### Requirement: LifecycleService.SetTimeout moves the deadline
`proto/rayito/v1/lifecycle.proto` SHALL define `LifecycleService.SetTimeout(SetTimeoutRequest{timeout_ms, mode}) returns (LifecycleState)`. The RPC SHALL require `x-access-token`, so the sandbox's own code cannot extend itself. The two modes SHALL behave as follows:

- `TIMEOUT_MODE_EXACT` SHALL set `deadline = now + timeout`, and MAY shorten it.
- `TIMEOUT_MODE_AT_LEAST` SHALL set `deadline = max(deadline, now + timeout)` while `ACTIVE`, and `now + timeout` from `RESUME_GRACE` or `EXPIRED`.
- Either mode SHALL move the phase to `ACTIVE` and count one extension when the deadline changed.

Errors:

- A target beyond the cap SHALL fail with `INVALID_ARGUMENT` "timeout beyond cap; cap_unix_ms=<n>" and change nothing.
- `timeout_ms` below 1000 SHALL fail with `INVALID_ARGUMENT`.
- `mode` `UNSPECIFIED` SHALL fail with `INVALID_ARGUMENT`.
- A sandbox without a lifecycle SHALL answer `FAILED_PRECONDITION` "lifecycle_unmanaged".
- A kill-mode sandbox already terminating SHALL answer `FAILED_PRECONDITION` "sandbox_timeout".

#### Scenario: exact can shorten, at-least cannot
- **WHEN** a sandbox with 600 s left receives `SetTimeout(EXACT, 10 000)` and later `SetTimeout(AT_LEAST, 5 000)` with 8 s left
- **THEN** the deadline moves to now + 10 s, the second call leaves it unchanged, and `extensions == 1`

#### Scenario: beyond the cap
- **WHEN** a sandbox launched with `cap_s = 900` receives `SetTimeout(EXACT, 2 000 000)`
- **THEN** the RPC fails with `INVALID_ARGUMENT` whose message starts with `timeout beyond cap` and carries `cap_unix_ms`, and `Health.lifecycle.deadline_unix_ms` is unchanged

#### Scenario: token required
- **WHEN** `SetTimeout` arrives without `x-access-token`
- **THEN** it fails with `UNAUTHENTICATED` before the service runs

### Requirement: Health reports the lifecycle
`HealthResponse` SHALL gain `LifecycleState lifecycle = 12`, and every M9 agent SHALL always set it. The message SHALL carry:

- `phase`
- `deadline_unix_ms` and `cap_unix_ms`, both converted to wall time as `wall_now + (instant − monotonic_now)`
- `timeout_ms`
- `on_timeout`
- `auto_resume`
- `extensions`

A sandbox launched without a lifecycle SHALL report phase `UNMANAGED` with zero instants. The absence of the field SHALL be what the SDK uses to recognise an agent older than M9. The field SHALL NOT be treated as secret.

#### Scenario: after run
- **WHEN** `/run` installs `timeout_s 60, cap_s 900, on_timeout kill` and the test reads `Health`
- **THEN** `lifecycle.phase == ACTIVE`, `deadline_unix_ms` is within 2 s of `/run` + 60 s in wall time, and `on_timeout == TIMEOUT_ACTION_KILL`

#### Scenario: unmanaged
- **WHEN** `/run` carries no lifecycle
- **THEN** `Health.lifecycle` is present with `phase == UNMANAGED` and `deadline_unix_ms == 0`

### Requirement: Kill mode ends the agent at the deadline
When a `kill`-mode deadline, or a kill-mode resume grace, expires, `rayd` SHALL run this sequence in order:

1. Close every open client stream with the `sandbox_timeout` form.
2. Refuse new RPCs other than `Health` and `SetTimeout` with `FAILED_PRECONDITION` `sandbox_timeout`.
3. Send `SIGTERM` to every process group, PTY session, kernel process group and the sidecar's group, with the sidecar supervisor prevented from relaunching.
4. Send `SIGKILL` 5 s later.
5. Wait 2 s.
6. Stop both listeners and exit with code 124. If the Q63 measurement shows that a non-zero code changes the platform's behaviour, the code SHALL be 0 instead.

The watcher thread SHALL call `std::process::exit` itself if the process is still alive 12 s after the sequence began. `/suspend` SHALL keep answering 200 in every lifecycle phase.

#### Scenario: workload signalled and agent gone
- **WHEN** an integration test runs a kill-mode sandbox with a shrunk deadline and a process that traps `TERM` into a marker file
- **THEN** the open `Start` stream ends with `EndEvent{exited:false, status:"sandbox_timeout", error.code:"sandbox_timeout"}`, the marker file exists, the shutdown token is cancelled, and the exit reason is `SandboxTimeout`

#### Scenario: stalled runtime cannot skip the deadline
- **WHEN** the terminator's graceful sequence never completes within the force budget
- **THEN** the watcher calls the terminator's `force`

### Requirement: Pause mode expires the sandbox and gates RPCs
When a `pause`-mode deadline expires, `rayd` SHALL move to `EXPIRED`, close every open client stream with the `sandbox_timeout` form without interrupting executions, and answer every RPC other than `/rayito.v1.HealthService/Health` and `/rayito.v1.LifecycleService/SetTimeout` with `FAILED_PRECONDITION` and message `sandbox_timeout`. It SHALL answer that status after the access-token check and after draining the request body. It SHALL NOT use `UNAVAILABLE` for this.

The `sandbox_timeout` close forms SHALL be:

| Stream | Close form |
|---|---|
| `Process.Start`/`Connect` | `EndEvent{exited:false, status:"sandbox_timeout", error:{code:"sandbox_timeout"}}` |
| `Pty.Create`/`Connect` | `PtyExited` with the same status and code |
| `WatchDir`, `Read`, `Execute`, `Reattach`, `Checkpoint`, `Restore` and `Write` | gRPC `FAILED_PRECONDITION` `sandbox_timeout` |

`SetTimeout(EXACT)` or `SetTimeout(AT_LEAST)` on an `EXPIRED` sandbox SHALL reopen it.

#### Scenario: streams and RPCs after expiry
- **WHEN** a pause-mode sandbox holds a `Start` stream, a PTY, a `WatchDir` and an `Execute` at the deadline
- **THEN** the first two end with status `sandbox_timeout`, the other two with `FAILED_PRECONDITION sandbox_timeout`, a new `Start` fails with `FAILED_PRECONDITION sandbox_timeout`, `Health` answers, and after `SetTimeout(EXACT, 60 000)` a new `Start` succeeds

#### Scenario: suspend still answers 200
- **WHEN** `/suspend` is posted while the sandbox is `EXPIRED`
- **THEN** the hook answers 200

### Requirement: Resume after the deadline opens a grace or applies E2B's auto-resume rule
After a changed `/resume` whose watcher saw a freeze of at least 2 s, and whose deadline has passed, `rayd` SHALL apply one of two rules:

- In `pause` mode with `auto_resume`, apply E2B's rule: `timeout = max(timeout, 300 s)`, `deadline = min(now + timeout, cap)`, phase `ACTIVE`.
- Otherwise, enter `RESUME_GRACE` for 30 s, during which only `Health` and `SetTimeout` are admitted.

When the grace expires without a `SetTimeout`, kill mode SHALL end the agent as specified and pause mode SHALL return to `EXPIRED`. A `/resume` without a detected freeze (a forged `/suspend` + `/resume` pair) SHALL change nothing in the lifecycle, and SHALL neither open a grace nor apply the auto-resume rule.

#### Scenario: auto-resume minimum
- **WHEN** a pause-mode sandbox with `timeout 60 s` and `auto_resume` is resumed after its deadline, following a real freeze
- **THEN** the phase is `ACTIVE` and the new deadline is the resume instant + 300 s

#### Scenario: grace honoured by connect
- **WHEN** a kill-mode sandbox is resumed after its deadline and `SetTimeout(AT_LEAST, 120 000)` arrives within 30 s
- **THEN** the phase is `ACTIVE` with the deadline at now + 120 s and the agent keeps running

#### Scenario: forged pair
- **WHEN** `/suspend` and `/resume` are posted from inside the VM after the deadline with no freeze
- **THEN** no grace is opened and the kill-mode deadline acts within the 20 s hold

### Requirement: SDK create accepts max_lifetime and on_timeout, backward compatible
The Python `Sandbox.create`/`AsyncSandbox.create` SHALL accept `max_lifetime: int | None = None` and `on_timeout: Literal["kill","pause"] | None = None`. The TypeScript `Sandbox.create` SHALL accept `maxLifetimeMs` and `onTimeout`.

**Backward compatibility.** When both are omitted, the request SHALL be byte-for-byte today's: no `lifecycle` block, `maximumDurationInSeconds = timeout`, and the idle policy resolved against `timeout`.

**When either is given:**

- The effective `on_timeout` SHALL be `"kill"` when omitted.
- `max_lifetime` SHALL be an integer in `120..=28800`:
  - above 28800 → `SandboxLifetimeException` / `SandboxLifetimeError` naming `max_lifetime` and 28800
  - below 120, or below `timeout` → `InvalidArgumentException` / `InvalidArgumentError`
- When omitted, `max_lifetime` SHALL default to `min(max(timeout + 60, 120), 28800)`.
- `maximumDurationInSeconds` SHALL equal `max_lifetime`.
- The payload SHALL carry `lifecycle {timeout_s: timeout, cap_s: max_lifetime, on_timeout, auto_resume}`.

**Pause mode:**

- `on_timeout="pause"` with `idle=None` SHALL raise `InvalidArgumentException` before any AWS call.
- An explicit `idle.suspended_duration_seconds` SHALL be refused.
- The platform idle policy SHALL be `{maxIdleDurationSeconds: idle.max_idle_seconds, suspendedDurationSeconds: max_lifetime − max_idle_seconds, autoResumeEnabled: true}`.
- The logical `auto_resume` SHALL be `idle.auto_resume`.

**Kill mode:** the idle policy SHALL be resolved against `max_lifetime` and `auto_resume` SHALL be `false`.

**Pool and reincarnate:** `create(pool=)` SHALL refuse both new kwargs, and `reincarnate()` SHALL reuse them.

#### Scenario: no lifecycle unless asked
- **WHEN** a unit test calls `create(timeout=900)` against the stubbed control plane
- **THEN** the `run-microvm` request has `maximumDurationInSeconds == 900` and the payload has no `lifecycle` key

#### Scenario: pause launch
- **WHEN** a unit test calls `create(timeout=60, max_lifetime=900, on_timeout="pause", idle=IdlePolicy(auto_resume=False))`
- **THEN** the request has `maximumDurationInSeconds == 900`, `idlePolicy == {maxIdleDurationSeconds: 300, suspendedDurationSeconds: 600, autoResumeEnabled: true}` and the payload `lifecycle == {auto_resume: false, cap_s: 900, on_timeout: "pause", timeout_s: 60}`

#### Scenario: pause without idle
- **WHEN** `create(on_timeout="pause", idle=None)` is called
- **THEN** it raises `InvalidArgumentException` and no `run-microvm` is recorded

### Requirement: SDK set_timeout and connect(timeout) move the deadline
The deadline SHALL be movable through these methods:

- **Python `sbx.set_timeout(timeout, *, request_timeout=None) -> None`** SHALL send `SetTimeout(EXACT, timeout * 1000)` and record the returned lifecycle.
- **Python `Sandbox.set_timeout(sandbox_id, timeout, *, access_token=None, …)`** SHALL:
  - require the access token (or `RAYITO_ACCESS_TOKEN`)
  - read `get-microvm`, refusing a terminal state with `SandboxNotFoundException` and a suspended one with `SandboxStateException` without waking it
  - otherwise call `SetTimeout(EXACT)` over a dedicated channel
- **Python `connect`**, in both the class form `Sandbox.connect(sandbox_id, *, timeout=None, …)` and the new instance form `sbx.connect(*, timeout=None, request_timeout=None)`, SHALL, after readiness:
  - send `SetTimeout(AT_LEAST, timeout * 1000)` when `timeout` is given
  - when it is not given, and the lifecycle is `resume_grace` or `expired`, reopen the sandbox with `AT_LEAST min(timeout_ms, cap_ms − now_ms − 5000)`
  - raise `SandboxLifetimeException` pointing to `reincarnate()` when fewer than 1000 ms remain
- **Python `resume()`** SHALL reopen a `resume_grace`/`expired` sandbox the same way.
- **On an `UNMANAGED` sandbox**, a `timeout` given to either method SHALL raise `InvalidArgumentException` naming `max_lifetime`.
- **Async** SHALL mirror all of this.
- **TypeScript** SHALL expose instance and static `setTimeout(…, timeoutMs, …)` and instance and static `connect(…, { timeoutMs })` with the same semantics.

#### Scenario: set_timeout is exact
- **WHEN** a unit test calls `sbx.set_timeout(150)` against the fake `rayd`
- **THEN** the fake `LifecycleService` recorded `SetTimeout{timeout_ms: 150000, mode: EXACT}` with the sandbox's `x-access-token`

#### Scenario: connect extends only
- **WHEN** a unit test calls `Sandbox.connect(id, access_token=t, timeout=300)`
- **THEN** the fake recorded `SetTimeout{timeout_ms: 300000, mode: AT_LEAST}` after the readiness `Health`

#### Scenario: connect reopens an expired sandbox
- **WHEN** the fake `Health` reports `phase EXPIRED, timeout_ms 60000` and `connect(id, access_token=t)` runs without `timeout`
- **THEN** the fake recorded `SetTimeout{timeout_ms: 60000, mode: AT_LEAST}`

### Requirement: The SDK exposes the logical deadline
Python SHALL export `SandboxLifecycle(phase, deadline, cap, timeout_seconds, on_timeout, auto_resume, extensions)` with `phase` in `unmanaged`, `active`, `resume_grace`, `expired`. TypeScript SHALL export `SandboxLifecycle` with camelCase fields and `phase` in `unmanaged`, `active`, `resumeGrace`, `expired`. The model SHALL be surfaced as follows:

- `SandboxHealth.lifecycle` SHALL carry it, and SHALL be `None` / `undefined` on an older agent.
- `SandboxInfo.lifecycle` SHALL carry it.
- `SandboxInfo.expires_at` / `expiresAt` SHALL be the logical deadline when the lifecycle is managed, and the platform expiry otherwise.
- The platform expiry SHALL always be available as `platform_expires_at` / `platformExpiresAt`.
- `remaining_seconds()` SHALL follow `expires_at`.
- Instance `get_info()` SHALL refresh `lifecycle` with one `Health` when `get-microvm` says `RUNNING` and the handle's recorded lifecycle is managed, SHALL otherwise return the last recorded `metadata` and `lifecycle` with no extra RPC, and SHALL never probe a suspended sandbox.
- `Sandbox.get_info(sandbox_id)` SHALL fill both from its existing `Health` probe.
- `SandboxInfo.timed_out` / `timedOut` SHALL be true when `state_reason` reports exit code 124, subject to the Q63 fallback.

#### Scenario: expires_at is the logical deadline
- **WHEN** the fake `Health` reports a managed lifecycle whose deadline is 90 s ahead on a sandbox launched with `max_lifetime=900`
- **THEN** `get_info().expires_at` is that deadline and `get_info().platform_expires_at == started_at + 900 s`

### Requirement: A live SDK suspends a pause-mode sandbox at the deadline
For a sandbox whose recorded lifecycle has `on_timeout == pause`, the SDK SHALL keep one timer armed at the reported deadline + 1 s (or immediately when the phase is `expired`), re-armed whenever it records a new lifecycle and cancelled by `close()`, `kill()` and the context-manager exit. The timers SHALL be a daemon `threading.Timer` in sync Python, a `loop.call_later` handle in async Python, and an unref'd `setTimeout` in TypeScript.

When the timer fires the SDK SHALL:

1. Read `get-microvm` and stop unless the state is `RUNNING`. It SHALL never probe `Health` on a suspended VM.
2. Read `Health`.
3. Call `suspend-microvm` only when `lifecycle.phase == expired`, without setting the pending-pause flag, so that the next request auto-resumes.
4. Re-arm when the deadline moved.

Failures SHALL be logged at warning level and swallowed.

#### Scenario: trigger suspends only an expired, running sandbox
- **WHEN** the trigger fires once with the plane reporting `RUNNING` and `Health` reporting `expired`, once with `SUSPENDED`, and once with `Health` reporting `active` and a later deadline
- **THEN** exactly one `suspend_microvm` is recorded (the first), no `Health` probe is made in the second, and the third re-arms the timer

### Requirement: SDK error mapping for the deadline
The SDKs SHALL map the deadline's errors as follows:

| Signal | Python | TypeScript |
|---|---|---|
| In-stream `EndEvent`/`PtyExited` status `sandbox_timeout`, or `StreamError.code` `sandbox_timeout` | `TimeoutException` | `TimeoutError` |
| gRPC `FAILED_PRECONDITION` with details `sandbox_timeout` (unary or stream close) | `TimeoutException` (checked before the generic `FAILED_PRECONDITION` → `InvalidArgumentException` rule) | `TimeoutError` |
| `INVALID_ARGUMENT` starting with `timeout beyond cap` | `InvalidArgumentException` naming `max_lifetime`, 28800 and `reincarnate()` | `InvalidArgumentError` with the same content |
| `FAILED_PRECONDITION` `lifecycle_unmanaged` | `InvalidArgumentException` naming `max_lifetime` | `InvalidArgumentError` |
| `UNIMPLEMENTED` from `SetTimeout` | `LifecycleUnsupportedException` (a subclass of `InvalidArgumentException`) | `LifecycleUnsupportedError` (extends `InvalidArgumentError`) |

None of these SHALL trigger the reconnection contract.

#### Scenario: stream end raises TimeoutException
- **WHEN** the fake `ProcessService` ends a background command with `EndEvent{status:"sandbox_timeout", error.code:"sandbox_timeout"}`
- **THEN** `handle.wait()` raises `TimeoutException`, no `Health` reconnect poll runs, and the same holds for a PTY handle

#### Scenario: gated unary raises TimeoutException
- **WHEN** the fake answers `files.read` with `FAILED_PRECONDITION` and details `sandbox_timeout`
- **THEN** the SDK raises `TimeoutException`, not `InvalidArgumentException`

### Requirement: The SDK fails closed on agents older than M9
When a launch sent a `lifecycle` block and the readiness `Health` has no `lifecycle` field, `create()` SHALL raise `LifecycleUnsupportedException` / `LifecycleUnsupportedError`. The error SHALL name the template, its `agent_version` and "publica una imagen M9". It SHALL be raised inside the launch's failure path, so the VM is terminated unless `keep_on_failure`, and it SHALL never be a `SandboxNotReadyException`. A launch without a block SHALL NOT check the field.

#### Scenario: older agent
- **WHEN** a unit test creates with `on_timeout="kill"` against a fake `Health` that omits `lifecycle`
- **THEN** `create()` raises `LifecycleUnsupportedException` and the stubbed control plane recorded one `terminate_microvm`

#### Scenario: older agent without lifecycle request
- **WHEN** the same fake serves `create(timeout=300)`
- **THEN** the sandbox is returned normally

### Requirement: Deadline logging never exposes secrets
`rayd` SHALL log each lifecycle transition as one `sandbox_timeout` line, with the fields `phase`, `on_timeout`, `timeout_ms`, `extensions`, `overrun_ms` and `action`. It SHALL log each `SetTimeout` with the fields `rpc`, `mode`, `timeout_ms`, `outcome` and `extensions`. Neither line SHALL contain tokens, payload characters, `envs` or `metadata`. The SDKs SHALL never log the access token or a JWE in the trigger's or the gate's warnings.

#### Scenario: log capture
- **WHEN** an integration test captures `rayd`'s tracing output across `/run` with a lifecycle, a `SetTimeout` and an expiry
- **THEN** the output contains `sandbox_timeout` lines with those fields and neither the access token nor its digest

### Requirement: The server-enforced timeout is accepted against real AWS
`clients/python/tests/e2e/test_m9_server_timeout.py` SHALL run against a published M9 `rayito-base`, without an execution role, with every sandbox at `max_lifetime <= 900`. It SHALL cover these behaviours:

- the kill-mode end after the client process dies: `TERMINATED` by deadline + 45 s, with `stateReason` and overrun recorded
- `set_timeout` extending (alive at 90 s after `set_timeout(150)` on `timeout=60`) and shortening (`set_timeout(10)` gone within 55 s)
- the beyond-cap error leaving `Health.lifecycle` unchanged
- `connect(timeout=300)` being AT_LEAST (≈600 s left stays ≈600; ≈30 s becomes ≈300)
- streams open at the deadline raising `TimeoutException`
- a resume grace honoured by `connect()`
- a stray auto-resume in kill mode ending about 30 s later
- pause mode with a live client: `SUSPENDED` within deadline + 15 s, auto-resume keeps a kernel variable, new deadline = resume + max(timeout, 300 s)
- pause mode with a dead client: `SUSPENDED` within deadline + max_idle + 60 s
- pause mode without auto-resume: a stray request gets `sandbox_timeout`, re-suspension within max_idle + 60 s, `connect()` reopens
- `Health.lifecycle` and `get_info().expires_at` tracking every `set_timeout`
- the older-agent gate on the published pre-M9 version (shim `UnimplementedError`, VM terminated), while a native create without a lifecycle still works there
- the E2B shim cookbook

`clients/typescript/tests/e2e/timeout.e2e.test.ts` SHALL mirror the native `setTimeout`, `connect({ timeoutMs })`, pause-mode and `TimeoutError` cases. The measured facts SHALL be recorded as `AWS_API_NOTES.md` §16 rows Q63–Q65 with placeholders only.

#### Scenario: acceptance run
- **WHEN** the e2e runs with `RAYITO_E2E=1`, the M9 image and `RAYITO_E2E_PRE_M9_TEMPLATE_VERSION`
- **THEN** every test passes and the printed `stateReason`, overruns and suspend timings are copied into rows Q63, Q64 and Q65

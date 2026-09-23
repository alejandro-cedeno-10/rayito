## MODIFIED Requirements

### Requirement: SDK pause, resume and connect
`Sandbox.pause(*, wait=True) -> bool` SHALL read `get-microvm` first and return `False` without calling `suspend-microvm` when the state is already `SUSPENDING|SUSPENDED` (AWS's `suspend-microvm` is idempotent: on a `SUSPENDED` VM it answers 200, never `ConflictException`, measured 2026-09-16), otherwise call `suspend-microvm` through the 2 TPS token bucket, return `False` on `ConflictException`, and with `wait` poll `get-microvm` until `SUSPENDED`; `Sandbox.resume(*, wait=True)` SHALL call `resume-microvm` (a conflict because the sandbox is already running is not an error), re-mint the JWE, and with `wait` poll `Health` until `agent_ready and kernel_ready` recording `resume_generation`; `Sandbox.connect(sandbox_id, ...)` on a `SUSPENDED` sandbox SHALL call `resume-microvm` only when the idle policy has `auto_resume` disabled, otherwise the readiness poll itself resumes it. After readiness, `connect(timeout=)` (class form, and the instance form `sbx.connect(timeout=)`) and `resume()` SHALL apply the deadline extension of the `sandbox-timeout` capability: `SetTimeout(AT_LEAST)` when `timeout` is given, and a reopen of a `resume_grace`/`expired` sandbox with its own timeout when it is not; `connect()` therefore extends a managed sandbox's deadline and never shortens it. The JWE refresher SHALL keep running during a pause. `create()` and `connect()` SHALL accept `reconnect_timeout` (default 60 s = the image's `resumeTimeoutInSeconds` 30 + 30). `IdlePolicy` SHALL default to `max_idle_seconds=300`, `suspended_duration_seconds=None` (resolved to `timeout − max_idle_seconds`) and `auto_resume=True`, with `max_idle_seconds >= 60` validated. `AsyncSandbox` SHALL offer the same surface.

#### Scenario: pause and resume timings
- **WHEN** the e2e calls `pause()` and later `resume()` on a running sandbox
- **THEN** `pause()` returns `True` and `get_info().state == "SUSPENDED"` within 30 s, and `resume()` returns with `get_info().state == "RUNNING"` within 30 s

#### Scenario: resume re-mints
- **WHEN** a unit test calls `resume()` against the stubbed control plane and the fake `rayd`
- **THEN** one `resume_microvm` and one `create_microvm_auth_token` are issued, `Health` is polled until `kernel_ready`, and `get_health().resume_generation` reflects the fake's value

#### Scenario: connect extends a managed deadline
- **WHEN** a unit test calls `Sandbox.connect(id, access_token=t, timeout=300)` on a sandbox whose fake `Health.lifecycle` is `ACTIVE`, and `resume()` on one whose lifecycle is `RESUME_GRACE` with `timeout_ms 60000`
- **THEN** the fake `LifecycleService` recorded `SetTimeout{300000, AT_LEAST}` for the first and `SetTimeout{60000, AT_LEAST}` for the second, both after the readiness `Health`

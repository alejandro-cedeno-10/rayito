## MODIFIED Requirements

### Requirement: SDK error mapping for the deadline
The SDKs SHALL map the deadline's errors as follows:

| Signal | Python | TypeScript |
|---|---|---|
| In-stream `EndEvent`/`PtyExited` status `sandbox_timeout`, or `StreamError.code` `sandbox_timeout` | `TimeoutException` | `TimeoutError` |
| gRPC `FAILED_PRECONDITION` with details `sandbox_timeout` (unary or stream close) | `TimeoutException` (checked before the generic `FAILED_PRECONDITION` → `InvalidArgumentException` rule) | `TimeoutError` |
| `INVALID_ARGUMENT` starting with `timeout beyond cap` | `InvalidArgumentException` naming `max_lifetime`, 28800 and `reincarnate()` | `InvalidArgumentError` with the same content |
| `FAILED_PRECONDITION` `lifecycle_unmanaged` | `InvalidArgumentException` naming `max_lifetime` | `InvalidArgumentError` |
| `UNIMPLEMENTED` from `SetTimeout` | `LifecycleUnsupportedException` (a subclass of `UnimplementedError`, not of `InvalidArgumentException`) | `LifecycleUnsupportedError` (extends `UnimplementedError`, not `InvalidArgumentError`) |

None of these SHALL trigger the reconnection contract.

#### Scenario: stream end raises TimeoutException
- **WHEN** the fake `ProcessService` ends a background command with `EndEvent{status:"sandbox_timeout", error.code:"sandbox_timeout"}`
- **THEN** `handle.wait()` raises `TimeoutException`, no `Health` reconnect poll runs, and the same holds for a PTY handle

#### Scenario: gated unary raises TimeoutException
- **WHEN** the fake answers `files.read` with `FAILED_PRECONDITION` and details `sandbox_timeout`
- **THEN** the SDK raises `TimeoutException`, not `InvalidArgumentException`

#### Scenario: an unmanaged agent's UNIMPLEMENTED is caught by the one unimplemented-feature type
- **WHEN** a unit test calls `SetTimeout` against a fake agent that answers `UNIMPLEMENTED` (an agent older than M9, no `LifecycleService`)
- **THEN** the SDK raises `LifecycleUnsupportedException`, `isinstance(error, UnimplementedError)` is `True`, `isinstance(error, InvalidArgumentException)` and `isinstance(error, SandboxException)` are both `False`, and a caller that only wrote `except rayito.UnimplementedError` (or the TypeScript equivalent) still catches it

### Requirement: The SDK fails closed on agents older than M9
When a launch sent a `lifecycle` block and the readiness `Health` has no `lifecycle` field, `create()` SHALL raise `LifecycleUnsupportedException` (Python) / `LifecycleUnsupportedError` (TypeScript) with `reason` naming the template, its `agent_version` and "publica una imagen M9", and `feature` `"create(max_lifetime=, on_timeout=)"` in Python or `"create({ maxLifetimeMs, onTimeout })"` in TypeScript. `LifecycleUnsupportedException`/`LifecycleUnsupportedError` SHALL be a subclass of `UnimplementedError`/`NotImplementedError`, never of `InvalidArgumentException`/`SandboxException`, as `UnimplementedError(feature, reason)`; the same type, with a `feature` naming the call that failed (`"connect(timeout=)"` in Python / `"connect({ timeoutMs })"` in TypeScript for a requested timeout against such an agent, or the `SetTimeout`/`setTimeout` request's own operation when the agent later answers `UNIMPLEMENTED`), SHALL cover every other place this SDK asks an agent older than M9 to manage a deadline it cannot enforce, chained (`from`/`__cause__`, TS `cause`) from the gRPC error when the rejection comes from an RPC rather than from a `Health` already read. The `create()` case SHALL be raised inside the launch's failure path, so the VM is terminated unless `keep_on_failure`, and it SHALL never be a `SandboxNotReadyException`. A launch without a `lifecycle` block SHALL NOT check the field.

#### Scenario: older agent
- **WHEN** a unit test creates with `on_timeout="kill"` against a fake `Health` that omits `lifecycle`
- **THEN** `create()` raises `LifecycleUnsupportedException` with `feature == "create(max_lifetime=, on_timeout=)"` (TypeScript: `LifecycleUnsupportedError` with `feature === "create({ maxLifetimeMs, onTimeout })"`), `isinstance(error, UnimplementedError)` is `True` and `isinstance(error, InvalidArgumentException)` is `False`, and the stubbed control plane recorded one `terminate_microvm`

#### Scenario: older agent without lifecycle request
- **WHEN** the same fake serves `create(timeout=300)`
- **THEN** the sandbox is returned normally

#### Scenario: connect() on an unmanaged agent names its own feature
- **WHEN** a unit test creates a sandbox against a fake `Health` that omits `lifecycle`, then calls `connect(timeout=60)` on it
- **THEN** the SDK raises `LifecycleUnsupportedException` with `feature == "connect(timeout=)"` (TypeScript: `feature === "connect({ timeoutMs })"`)

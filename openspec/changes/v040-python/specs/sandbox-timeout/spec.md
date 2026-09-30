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

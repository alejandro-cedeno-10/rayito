## MODIFIED Requirements

### Requirement: SDK stream error contract
The SDK SHALL retry a stream open exactly once after a proxy 403 (`PERMISSION_DENIED` with `Received http2 header with status: 403`) by re-minting the JWE, only when no message has been consumed. A mid-stream `UNAVAILABLE`, connection reset, `GOAWAY` or EOF, and an in-stream `suspending` end, SHALL trigger the reconnection contract of the `suspend-resume` capability (poll `Health` with backoff for `reconnect_timeout`, re-subscribe with `Connect(pid, from_seq=last_seq + 1)`); only when that poll fails SHALL the failure be classified: `TERMINATING|TERMINATED` → `SandboxNotFoundException`, `SUSPENDED` without auto-resume → `SandboxStateException`, otherwise `SandboxException`. gRPC `FAILED_PRECONDITION` SHALL map to `InvalidArgumentException` except with details `sandbox_timeout`, which SHALL map to `TimeoutException` (as SHALL an in-stream `EndEvent` or `PtyExited` with status `sandbox_timeout`) and SHALL NOT trigger the reconnection contract, `OUT_OF_RANGE` to `NotFoundException`, `DEADLINE_EXCEEDED` on a command stream to `TimeoutException`.

#### Scenario: expired JWE at stream open
- **WHEN** the proxy answers 403 to the `Start` request
- **THEN** the SDK mints a new token and re-issues the `Start` once, and the command runs exactly once

#### Scenario: reset while the sandbox is terminated
- **WHEN** a background stream is reset, `Health` never answers and `get_microvm` reports `TERMINATED`
- **THEN** `wait()` raises `SandboxNotFoundException`

#### Scenario: reset while the sandbox comes back
- **WHEN** a background stream is reset and `Health` answers again with a higher `resume_generation`
- **THEN** the handle re-subscribes with `Connect(pid, from_seq=last_seq + 1)` and `wait()` returns the process's result

#### Scenario: sandbox timeout is a timeout, not a cut
- **WHEN** a background stream ends with `EndEvent{status:"sandbox_timeout"}` and a unary `SendInput` fails with `FAILED_PRECONDITION` `sandbox_timeout`
- **THEN** `wait()` and `send_stdin()` both raise `TimeoutException` and no `Health` reconnect poll runs

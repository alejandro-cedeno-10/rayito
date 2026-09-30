## MODIFIED Requirements

### Requirement: Python SDK exposes the metrics history
`Sandbox.get_metrics_history(*, start: datetime | None = None, end: datetime | None = None, max_points: int | None = None, request_timeout: float | None = None) -> list[SandboxMetrics]` SHALL call `MetricsHistory` with `start`/`end` converted to Unix milliseconds (a naive `datetime` is local time; `None` → 0) and `max_points` (`None` → 0), and SHALL return the samples ascending with `mem_cache_bytes` set. It SHALL raise `InvalidArgumentException` before any RPC for a negative bound, `start > end`, or a `max_points` that is not an integer ≥ 1. An `UNIMPLEMENTED` answer SHALL become `rayito.exceptions.UnimplementedError` (feature `get_metrics_history`, or `Sandbox.get_metrics_history(sandbox_id)` for the class variant; reason `la imagen es anterior a M9 (rayd sin MetricsHistory): publica una imagen M9`), chained from the SDK's own generic `UnimplementedError` for an `UNIMPLEMENTED` RPC (the unary table in `_transport.py`, item 1 of `sandbox-timeout`'s error mapping), which is itself chained from the translated gRPC error, like the transfers and the server timeout. The class variant `Sandbox.get_metrics_history(sandbox_id, *, access_token=None, start, end, max_points, request_timeout, region, session, control_plane, transport)` SHALL resolve the token from `access_token` or `RAYITO_ACCESS_TOKEN` (else `AuthenticationException` before any AWS call), SHALL raise `SandboxNotFoundException` for `TERMINATING`/`TERMINATED` and `SandboxStateException` for any other state but `RUNNING` without minting a JWE, and otherwise SHALL mint one JWE for port 8080, send one `MetricsHistory` with `x-access-token` over a dedicated channel (one retry after a proxy 403) and close it. `SandboxMetrics` SHALL gain `mem_cache_bytes: int = 0`. The zero-argument `get_metrics()` snapshot SHALL be unchanged. `AsyncSandbox` SHALL offer the identical surface as coroutines.

#### Scenario: instance history
- **WHEN** the fake `rayd` holds three samples and the SDK calls `sbx.get_metrics_history(start=t, end=t + timedelta(minutes=5), max_points=2)`
- **THEN** the recorded request has `start_unix_ms == t_ms`, `end_unix_ms == t_ms + 300 000`, `max_points == 2`, and the result is the fake's samples in order with `mem_cache_bytes` mapped

#### Scenario: validation before the RPC
- **WHEN** the SDK calls `get_metrics_history(start=later, end=earlier)` and `get_metrics_history(max_points=0)`
- **THEN** both raise `InvalidArgumentException` and the fake recorded no `MetricsHistory` request

#### Scenario: pre-M9 image
- **WHEN** the fake answers `MetricsHistory` with `UNIMPLEMENTED`
- **THEN** `get_metrics_history()` raises `UnimplementedError` (not `SandboxException`) whose reason mentions M9
- **AND** `error.__cause__` is the SDK's generic `UnimplementedError` for the `UNIMPLEMENTED` table entry, itself not a `SandboxException`
- **AND** `error.__cause__.__cause__` is the underlying `grpc.RpcError` whose `code()` is `grpc.StatusCode.UNIMPLEMENTED`, so the gRPC status still survives the chain even though neither wrapper carries a `grpc_code` attribute

#### Scenario: class variant
- **WHEN** the unit test calls `Sandbox.get_metrics_history(sandbox_id)` with no token and `RAYITO_ACCESS_TOKEN` unset, then with a token while the plane answers `SUSPENDED`, then with a token while it answers `RUNNING`
- **THEN** the first raises `AuthenticationException` with no control-plane call, the second raises `SandboxStateException` with no `create-microvm-auth-token`, and the third mints exactly one token, sends one `MetricsHistory` carrying `x-access-token`, returns the samples and leaves no channel open

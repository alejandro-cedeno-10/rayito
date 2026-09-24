## ADDED Requirements

### Requirement: rayd keeps a 5-second metrics ring from /run
`rayd` SHALL keep, in memory, a ring of at most 5 760 metrics samples (`HISTORY_CAPACITY`, 8 h at one sample per 5 s) fed by a sampler task that ticks every 5 s (`MissedTickBehavior::Skip`) and reads the existing `MetricsProbe` only while the session phase is `running` or `resumed`. Each sample SHALL carry `unix_ms` from the `Clock` port's wall clock, `cpu_used_pct` averaged over the window since the previous sample, `mem_used`, `mem_total`, `mem_cache` (`/proc/meminfo` `Cached`, 0 when absent), `disk_used`, `disk_total` and `cpu_count`. The first tick after the gate opens, after a `resume_generation` change or after a probe error SHALL only arm the CPU baseline and record nothing. Nothing SHALL be recorded before `/run` (so the image-build snapshot carries an empty ring) nor while the phase is `suspending`, which leaves a gap covering the suspension. The ring SHALL append only samples strictly newer than the last one (older ones are dropped), SHALL evict the oldest at capacity, and SHALL stay within 64 bytes per sample. The sampler SHALL perform no network I/O, so it cannot postpone idle suspension, and SHALL log only error reasons, never metric values.

#### Scenario: ring order and capacity
- **WHEN** the unit test pushes samples at 1, 2 and 3 ms into a ring of capacity 2, then a sample at 3 ms again
- **THEN** the pushes return `Appended`, `Appended`, `AppendedEvictingOldest` and `RejectedOutOfOrder`, the ring holds 2 and 3, `oldest_unix_ms()` is 2, `HISTORY_CAPACITY × 5 s == 28 800 s` and `size_of::<MetricsSample>() <= 64`

#### Scenario: sampler across a suspend jump
- **WHEN** the pure sampler observes ticks of generation 0 at 5, 10 and 15 s, then `close_gate()`, then generation 1 at 320 s and 325 s
- **THEN** it returns nothing at 5 s, samples at 10 and 15 s, nothing at 320 s, and a sample at 325 s whose `cpu_used_pct` is computed from the 320 s baseline

#### Scenario: task gated by the session phase
- **WHEN** the adapter test runs the task with a paused tokio clock, a `TestClock` and a fake probe, advancing 30 s before `/run`, 30 s after it, 300 s in `suspending`, then 30 s after `/resume`
- **THEN** the ring is empty before `/run`, gains a sample per interval after the arming tick, gains nothing while suspending, and shows a gap of at least 300 s between the last pre-suspend and the first post-resume sample

### Requirement: HealthService.MetricsHistory serves inclusive ranges with downsampling
`HealthService.MetricsHistory(MetricsHistoryRequest{start_unix_ms, end_unix_ms, max_points})` SHALL require `x-access-token` and return `MetricsHistoryResponse{samples, oldest_unix_ms}` with the retained samples whose timestamp lies in `[start_unix_ms, end_unix_ms]` in ascending order, each as a `MetricsResponse` with all eight fields (`timestamp_unix_ms` = the sample's `unix_ms`, `mem_cache_bytes` filled), and `oldest_unix_ms` = the oldest retained sample of the whole ring (0 when empty). `start_unix_ms = 0` SHALL mean from the oldest sample, `end_unix_ms = 0` no upper bound, `max_points = 0` no reduction. A negative bound, or `start_unix_ms > end_unix_ms` with `end_unix_ms != 0`, SHALL fail with `INVALID_ARGUMENT`. With `max_points = m` smaller than the `n` selected samples, the response SHALL hold exactly `m` points: bucket `i` covers indices `[⌊i·n/m⌋, ⌊(i+1)·n/m⌋)` and its point is the bucket's last sample with `cpu_used_pct` replaced by the bucket mean. The RPC SHALL not be phase-gated. The `Metrics` snapshot RPC SHALL keep its shape.

#### Scenario: range and downsampling
- **WHEN** the integration test seeds the history with ten samples at 1 000 … 10 000 ms and calls `MetricsHistory` with the token and `(start 2 000, end 9 000, max_points 0)`, then `(0, 0, 3)`
- **THEN** the first response has the eight samples 2 000 … 9 000 ascending, and the second has exactly three points at 3 000, 6 000 and 10 000 ms whose `cpu_used_pct` are the means of their buckets, both with `oldest_unix_ms == 1 000`

#### Scenario: authentication and invalid bounds
- **WHEN** `MetricsHistory` is called without `x-access-token`, and with the token and `start_unix_ms 5 end_unix_ms 4`, and with `start_unix_ms -1`
- **THEN** the first fails with `UNAUTHENTICATED` and the other two with `INVALID_ARGUMENT`

#### Scenario: empty ring
- **WHEN** `MetricsHistory` is called with the token before any sample was recorded
- **THEN** it answers `samples` empty and `oldest_unix_ms == 0`

### Requirement: Health reports the guest CPUs and memory
`HealthService.Health` SHALL carry `cpu_count` (field 14, the CPUs `rayd` may use per `available_parallelism`) and `memory_total_bytes` (field 15, `MemTotal` of `/proc/meminfo` in bytes), read on every call from the `MetricsProbe`, 0 when unreadable; reading them SHALL never make `Health` fail. Both are the guest view, not the image size, and are not secret. `Health` SHALL stay the only RPC without `x-access-token`. The Python `SandboxHealth` SHALL expose `cpu_count` and `memory_total_bytes` and the TypeScript `SandboxHealth` `cpuCount` and `memoryTotalBytes`, all 0 on a pre-M9 image.

#### Scenario: guest facts in Health
- **WHEN** the integration test calls `Health` without a token after `/run`
- **THEN** `cpu_count >= 1`, on Linux `memory_total_bytes > 0`, and `get_health()` in both SDKs returns the same numbers

### Requirement: Python SDK exposes the metrics history
`Sandbox.get_metrics_history(*, start: datetime | None = None, end: datetime | None = None, max_points: int | None = None, request_timeout: float | None = None) -> list[SandboxMetrics]` SHALL call `MetricsHistory` with `start`/`end` converted to Unix milliseconds (a naive `datetime` is local time; `None` → 0) and `max_points` (`None` → 0), and SHALL return the samples ascending with `mem_cache_bytes` set. It SHALL raise `InvalidArgumentException` before any RPC for a negative bound, `start > end`, or a `max_points` that is not an integer ≥ 1. An `UNIMPLEMENTED` answer SHALL become `rayito.exceptions.UnimplementedError` (feature `get_metrics_history`, or `Sandbox.get_metrics_history(sandbox_id)` for the class variant; reason `la imagen es anterior a M9 (rayd sin MetricsHistory): publica una imagen M9`) chained from the translated gRPC error, like the transfers and the server timeout. The class variant `Sandbox.get_metrics_history(sandbox_id, *, access_token=None, start, end, max_points, request_timeout, region, session, control_plane, transport)` SHALL resolve the token from `access_token` or `RAYITO_ACCESS_TOKEN` (else `AuthenticationException` before any AWS call), SHALL raise `SandboxNotFoundException` for `TERMINATING`/`TERMINATED` and `SandboxStateException` for any other state but `RUNNING` without minting a JWE, and otherwise SHALL mint one JWE for port 8080, send one `MetricsHistory` with `x-access-token` over a dedicated channel (one retry after a proxy 403) and close it. `SandboxMetrics` SHALL gain `mem_cache_bytes: int = 0`. The zero-argument `get_metrics()` snapshot SHALL be unchanged. `AsyncSandbox` SHALL offer the identical surface as coroutines.

#### Scenario: instance history
- **WHEN** the fake `rayd` holds three samples and the SDK calls `sbx.get_metrics_history(start=t, end=t + timedelta(minutes=5), max_points=2)`
- **THEN** the recorded request has `start_unix_ms == t_ms`, `end_unix_ms == t_ms + 300 000`, `max_points == 2`, and the result is the fake's samples in order with `mem_cache_bytes` mapped

#### Scenario: validation before the RPC
- **WHEN** the SDK calls `get_metrics_history(start=later, end=earlier)` and `get_metrics_history(max_points=0)`
- **THEN** both raise `InvalidArgumentException` and the fake recorded no `MetricsHistory` request

#### Scenario: pre-M9 image
- **WHEN** the fake answers `MetricsHistory` with `UNIMPLEMENTED`
- **THEN** `get_metrics_history()` raises `UnimplementedError` (not `SandboxException`) whose reason mentions M9 and whose `__cause__` carries `grpc_code` `UNIMPLEMENTED`

#### Scenario: class variant
- **WHEN** the unit test calls `Sandbox.get_metrics_history(sandbox_id)` with no token and `RAYITO_ACCESS_TOKEN` unset, then with a token while the plane answers `SUSPENDED`, then with a token while it answers `RUNNING`
- **THEN** the first raises `AuthenticationException` with no control-plane call, the second raises `SandboxStateException` with no `create-microvm-auth-token`, and the third mints exactly one token, sends one `MetricsHistory` carrying `x-access-token`, returns the samples and leaves no channel open

### Requirement: SandboxInfo carries the guest facts and the agent version
The Python `SandboxInfo` SHALL gain `agent_version: str | None`, `cpu_count: int | None` and `memory_mb: int | None` (default `None`, meaning not read from the agent or unknown), with `agent_version` `""` → `None`, `cpu_count` 0 → `None`, and `memory_mb = memory_total_bytes // 1 048 576` with 0 → `None`. The instance `get_info()` SHALL fill them from the last `Health` seen with no extra RPC; the class `get_info(sandbox_id, read_metadata=True)` SHALL fill them from the `Health` it already sends for `metadata` on a `RUNNING` sandbox whose agent is ready, and leave them `None` otherwise; `sbx.info`, `launch_info` and `list()` items SHALL keep `None`. The TypeScript `SandboxInfo` SHALL gain the optional `agentVersion`, `cpuCount` and `memoryMb` with the same rules, filled by the instance `getInfo()` from the last recorded `Health`; the static `Sandbox.getInfo(id)` SHALL leave them `undefined`. The docs SHALL state that `memory_mb` is the guest `MemTotal`, not the image's `minimumMemoryInMiB`.

#### Scenario: facts after connect
- **WHEN** the fake `rayd` answers `Health` with `agent_version "0.3.0"`, `cpu_count 2` and `memory_total_bytes 8 405 385 216`, and the SDK calls `sbx.get_info()`
- **THEN** `info.agent_version == "0.3.0"`, `info.cpu_count == 2`, `info.memory_mb == 8016`, and no RPC beyond `get-microvm` was made

#### Scenario: pre-M9 agent
- **WHEN** the fake answers `Health` with `agent_version "0.2.0"`, `cpu_count 0` and `memory_total_bytes 0`
- **THEN** `get_info()` has `agent_version == "0.2.0"`, `cpu_count is None` and `memory_mb is None`, in both SDKs

### Requirement: TypeScript SDK exposes the metrics history
The TypeScript `Sandbox` SHALL expose `getMetricsHistory({ start?: Date, end?: Date, maxPoints?: number, requestTimeoutMs? }) → Promise<SandboxMetrics[]>` and the static `Sandbox.getMetricsHistory(sandboxId, { accessToken?, start, end, maxPoints, requestTimeoutMs, region, controlPlane, client, transport })` with the Python semantics: the same validation (`InvalidArgumentError` before any RPC), `UNIMPLEMENTED` → `UnimplementedError` with the same M9 reason (feature `getMetricsHistory` or `Sandbox.getMetricsHistory(sandboxId)`) and the gRPC error as `cause`, token from `accessToken` or `RAYITO_ACCESS_TOKEN` (else `AuthenticationError` before any AWS call), `SandboxNotFoundError` for terminal states, `SandboxStateError` for any other state but `RUNNING` without minting a JWE, and one dedicated transport (one JWE for 8080, one retry after a proxy 403, `sessionManager.abort()` afterwards). `SandboxMetrics` SHALL gain `memCacheBytes`.

#### Scenario: static history against the fakes
- **WHEN** a unit test calls `Sandbox.getMetricsHistory(id, { accessToken, controlPlane: fakePlane, transport })` with the plane answering `RUNNING` and the fake `rayd` holding two samples, and again with the plane answering `SUSPENDED`
- **THEN** the first resolves to two samples with `memCacheBytes` after exactly one `createAuthToken` and one `metricsHistory` call carrying `x-access-token`, and the second rejects with `SandboxStateError` with no `createAuthToken`

### Requirement: Metrics history accepted on real AWS
`clients/python/tests/e2e/test_m9_observability.py` SHALL run against a `rayito-base` version built with this `rayd` and SHALL prove: a history over a 5-minute window that includes one `pause()`/`resume()` has at least 45 samples, strictly ascending and inside the window, at least 90 % of the consecutive deltas within 5 ± 1.5 s, exactly one delta of at least 30 s covering the pause, `mem_cache_bytes > 0` after writing 16 MiB, a narrower `start`/`end` honoured, and `max_points=10` giving exactly 10 points; the shim's ranged `get_metrics` and the static shim `get_metrics(id, access_token=)` return lists and the static form without a token raises `UnimplementedError`; a sandbox with `max_idle_seconds=60` and no client traffic still reaches `SUSPENDED` within 180 s while the sampler runs; `get_info()` has a non-empty `agent_version`, `1 <= cpu_count <= nproc` and `memory_mb` equal to the sandbox's `MemTotal` in MiB. `clients/typescript/tests/e2e/m9-observability.e2e.test.ts` SHALL mirror the history, `maxPoints`, window, static and `getInfo` checks. The guest CPU and memory against the image version's `resources[].minimumMemoryInMiB` SHALL be recorded as a new `AWS_API_NOTES.md` §16 row with placeholders only.

#### Scenario: history on AWS
- **WHEN** the e2e runs with `RAYITO_E2E=1` and the M9 image
- **THEN** every assertion above holds, the printed `samples`, `gap_s`, `history_rpc_s`, `idle_suspend_s`, `guest_cpu_count`, `guest_memory_mb` and `image_minimum_memory_mib` are copied into the §16 row, and no MicroVM created by the run stays alive

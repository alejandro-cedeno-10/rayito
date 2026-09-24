## MODIFIED Requirements

### Requirement: HealthService.Metrics reports procfs metrics
`Metrics` SHALL require `x-access-token` and return `cpu_used_pct` (0–100, from two `/proc/stat` samples 100 ms apart), `mem_used_bytes = MemTotal - MemAvailable`, `mem_total_bytes`, `mem_cache_bytes` (`Cached` of `/proc/meminfo` in bytes, 0 when the line is absent), `disk_used_bytes`/`disk_total_bytes` of `/` from `statvfs`, `cpu_count >= 1`, and `timestamp_unix_ms` from the wall clock. The SDK SHALL expose `sbx.get_metrics() -> SandboxMetrics` with `mem_cache_bytes` (Python, default 0 so an image that predates the field reads 0) and `getMetrics()` with `memCacheBytes` (TypeScript).

#### Scenario: metrics from the SDK
- **WHEN** the SDK calls `sbx.get_metrics()`
- **THEN** `cpu_count >= 1`, `mem_total_bytes > 0`, `mem_used_bytes <= mem_total_bytes`, `disk_total_bytes > 0`, `0 <= cpu_used_pct <= 100`, and `timestamp` is within 60 s of now

#### Scenario: metrics without a token
- **WHEN** `Metrics` is called without `x-access-token`
- **THEN** it fails with `UNAUTHENTICATED`

#### Scenario: page cache is reported
- **WHEN** `parse_meminfo` reads a fixture with `Cached: 512000 kB` and another without a `Cached:` line, and the Linux integration test calls `Metrics` with the token
- **THEN** the first yields `cached == 512000 * 1024`, the second `cached == 0` with no error, and the RPC answers `mem_cache_bytes > 0`

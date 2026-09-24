## Why

M9 ("paridad con E2B") closes the E2B read-side gaps that Rayito 0.2.0 still
declares `UnimplementedError` or silently narrows:

1. **Metrics are a single snapshot.** E2B's `sbx.get_metrics(start, end)`
   returns a time series sampled every 5 s since the sandbox started, with
   `mem_cache`, and a static `Sandbox.get_metrics(sandbox_id)`. Rayito's
   `HealthService.Metrics` answers one procfs snapshot (two `/proc/stat`
   reads 100 ms apart), the shim raises `UnimplementedError` for
   `get_metrics(start=, end=)` and for the class variant, and `SandboxMetrics`
   has no `mem_cache` (`docs/site/docs/e2b-compat.md`, rows "no hay
   historial" and "necesita el access token").
2. **`list()` cannot be resumed or ordered.** `list-microvms` has
   `nextToken`/`maxResults` (`AWS_API_NOTES.md` §1, §6, and
   `docs/aws-api/model_summary.md` `ListMicrovms`), but both SDKs drain every
   page internally: the shim's `next_token` is always `None` and passing one
   raises `UnimplementedError`; there is no `order='asc'|'desc'` and no
   `SandboxQuery.state/started_after/template` (E2B 2.45). The TypeScript
   client has no paginator shape and no metadata filter, which Python has had
   since M6.
3. **`SandboxInfo` lacks the guest facts** that E2B 2.x reports
   (`cpu_count`, `memory_mb`, `envd_version`). The data exists inside the VM
   (procfs, `Health.agent_version`) but never reaches `get_info()`. Q61
   (`AWS_API_NOTES.md` §16) saw the guest report 8016 MiB of `MemTotal` for an
   image declared with `minimumMemoryInMiB` 2048, so which number
   `SandboxInfo` reports has to be measured and written down.

All three are feasible without any new AWS API parameter and without a
control-plane service: the history lives in `rayd` (the VM lives at most
28 800 s, so 5 760 samples of 5 s cover a whole life), pagination only uses
the documented `nextToken`/`maxResults`, and the guest facts travel in the
existing anonymous `Health` probe.

## What Changes

- **`rayd` keeps a metrics history** (design D1–D4). `rayd-core` gains a
  pure `MetricsRing` (capacity 5 760, append-only in wall-clock order,
  inclusive range query, bucket downsampling) and a pure `MetricsSampler`
  state machine; a new adapter task samples the existing `MetricsProbe` every
  5 s with wall timestamps from the `Clock` port, only while the session
  phase is `running`/`resumed`. Nothing is sampled before `/run` (so the
  image-build snapshot carries an empty ring) or while the VM is suspended,
  which leaves a gap exactly as E2B does while paused. The sampler never
  touches the network, so it cannot keep a VM awake (idle counts endpoint
  bytes only, `AWS_API_NOTES.md` §7 / Q15).
- **Proto (additive, applied by the Contract agent, exact delta in design
  D5)**: `HealthService.MetricsHistory(MetricsHistoryRequest) returns
  (MetricsHistoryResponse)` (requires `x-access-token` like `Metrics`),
  `MetricsResponse.mem_cache_bytes = 8`, `HealthResponse.cpu_count = 14` and
  `HealthResponse.memory_total_bytes = 15`. `Metrics` keeps its shape and now
  fills `mem_cache_bytes` from `/proc/meminfo` `Cached`.
- **Python SDK (sync and async, identical)**: `sbx.get_metrics_history(*,
  start=None, end=None, max_points=None) -> list[SandboxMetrics]` plus the
  class variant `Sandbox.get_metrics_history(sandbox_id, *, access_token=None,
  ...)` (token or `RAYITO_ACCESS_TOKEN`, `RUNNING` only, dedicated channel);
  `SandboxMetrics.mem_cache_bytes`; `SandboxHealth.cpu_count` /
  `memory_total_bytes`; `SandboxInfo.agent_version` / `cpu_count` /
  `memory_mb` (guest view, `None` when not read from the agent);
  `Sandbox.paginate(...) -> SandboxListPaginator` with `limit`, an opaque
  `next_token`, `order`, `started_after`, `states`, `template`, `metadata`,
  and the same new filters on the streaming `Sandbox.list(...)`; a
  `ControlPlane.list_microvms_page(...)` port method over one
  `list-microvms` page. The zero-argument `get_metrics()` snapshot is
  unchanged.
- **E2B shim**: `sbx.get_metrics(start, end)` returns the history (the
  one-element snapshot only when the history is empty or the image predates
  M9 and no range was asked); static `Sandbox.get_metrics(sandbox_id,
  start, end, access_token=)` works with a token and keeps
  `UnimplementedError` (with the reason) without one; `SandboxMetrics.mem_cache`;
  `Sandbox.list(query, limit, next_token, order)` with real `next_token`
  and `SandboxQuery(metadata, state, started_after, template)`. Metadata over
  `PAUSED` stays `UnimplementedError` (SPEC §4: no client-side store, and
  reading it would wake the VM). The shim's 2.x `SandboxInfo` fields are
  mapped by `m9-e2b-v2-surface`, not here.
- **TypeScript SDK (camelCase mirror)**: `getMetricsHistory({ start, end,
  maxPoints })`, static `Sandbox.getMetricsHistory(sandboxId, { accessToken,
  ... })`, `SandboxMetrics.memCacheBytes`, `SandboxHealth.cpuCount` /
  `memoryTotalBytes`, `SandboxInfo.agentVersion` / `cpuCount` / `memoryMb`,
  `Sandbox.paginate(opts) -> SandboxListPaginator` (`hasNext`, `nextToken`,
  `nextItems()`), `Sandbox.list(opts)` gaining `metadata`, `startedAfter`,
  `order`, and `ControlPlane.listMicrovmsPage(...)`. The `next_token` format
  is byte-identical across both SDKs (golden vector in design D8).
- **Measurement**: a new `AWS_API_NOTES.md` §16 row records the guest
  `cpu_count` / `MemTotal` against `minimumMemoryInMiB` of the image version,
  and the docs state that `SandboxInfo.memory_mb` is the guest view.

## Capabilities

### New Capabilities

- `sandbox-observability`: the `rayd` metrics ring and sampler, the
  `MetricsHistory` RPC, the guest facts in `Health` and `SandboxInfo`, the
  Python and TypeScript history surfaces, and their real-AWS acceptance.
- `sandbox-listing`: the one-page control-plane port, the opaque
  cross-SDK `next_token`, client-side `order`/`started_after`/`states`
  filters, the Python and TypeScript paginators, the TypeScript metadata
  filter, and their real-AWS acceptance.

### Modified Capabilities

- `process-lifecycle`: `HealthService.Metrics reports procfs metrics` adds
  `mem_cache_bytes` and `SandboxMetrics.mem_cache_bytes`.
- `e2b-compat`: `E2B features without an AWS primitive raise
  UnimplementedError` drops `get_metrics(start=, end=)`, `list(next_token=)`
  and the tokened class `get_metrics`; `E2B-shaped models on the instance`
  makes `get_metrics()` a series with `mem_cache`; `E2B-shaped listing`
  gains real `next_token`, `order` and the three new `SandboxQuery` fields.

## Impact

- **Code**: `crates/rayd-core/src/{metrics.rs,metrics_history.rs,health.rs,lib.rs}`,
  `crates/rayd/src/{grpc/health.rs,grpc/mod.rs,lifecycle/metrics_sampler.rs,lifecycle/mod.rs,main.rs}`,
  `crates/rayd/tests/{common/mod.rs,m2_process.rs,m9_metrics_history.rs}`;
  `clients/python/src/rayito/{_models.py,_aws.py,_sandbox_base.py,_process_base.py,_listing_base.py,_metrics_base.py,__init__.py}`,
  `sandbox_sync/main.py`, `sandbox_async/main.py`,
  `e2b/{_models.py,_compat.py,_sync.py,_async.py}`;
  `clients/typescript/src/{models.ts,index.ts,aws/*.ts,sandbox/{sandbox.ts,core.ts,commands.ts,readiness.ts,listing.ts,probe.ts}}`.
- **Proto**: `proto/rayito/v1/health.proto` only, additive, FILE-compatible;
  regenerated Rust/Python/TypeScript code.
- **AWS**: no new parameter. `list-microvms` `maxResults`/`nextToken`/
  `imageIdentifier`/`imageVersion` and `get-microvm-image-version`
  `resources[].minimumMemoryInMiB` are already documented. One new §16 row.
- **Security**: `MetricsHistory` requires the access token; the two new
  `Health` fields are non-secret guest facts the workload can already read
  from `/proc` (`SECURITY.md` T4 gains one sentence). No deferred row of
  `docs/SECURITY_AUDIT.md` §8 is touched. The SDKs never log a `next_token`
  (it carries the image ARN and a fingerprint of the metadata filter).
- **Cost**: the ring is ≤ 64 B × 5 760 ≈ 369 KB of `rayd` RSS at 8 h; the
  sampler reads procfs every 5 s. `order=` costs O(pages) `ListMicrovms`
  calls; a metadata filter keeps its documented O(n) `Health` cost.
- **Docs**: `SPEC.md` §3, `ARCHITECTURE.md` (rayd runtime tasks and
  `HealthService`), `SECURITY.md` T4, `AWS_API_NOTES.md` §16,
  `docs/site/docs/{concepts.md,e2b-compat.md,limits.md}`, both READMEs and
  the three CHANGELOGs.

## Out of scope (named so nobody wonders)

- The shim's 2.x `SandboxInfo` fields (`cpu_count`, `memory_mb`,
  `envd_version`, `sandbox_domain`, …) and the TypeScript `rayito/e2b` entry
  point with its static `getMetrics`/`list`: `m9-e2b-v2-surface` maps what
  this change delivers.
- `rayito sandbox metrics` in the CLI (`m9-e2b-v2-surface`).
- Metadata filtering over `PAUSED` sandboxes, a client-side registry, or
  metrics of a suspended sandbox without waking it (SPEC §4).
- Per-sandbox size parameters (size is an image property, SPEC §4).
- Metric export (OTel/CloudWatch): out of scope by SPEC §4.

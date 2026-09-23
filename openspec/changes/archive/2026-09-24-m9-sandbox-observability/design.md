## Context

Ground truth this design stands on (read in this order: `CLAUDE.md`,
`openspec/project.md`, `AWS_API_NOTES.md`, `ARCHITECTURE.md`, `SECURITY.md`,
`docs/site/docs/e2b-compat.md`, `docs/research/2026-09-m7-oss-readiness.md`
§3, `docs/SECURITY_AUDIT.md` §8):

- `HealthService.Metrics` (`proto/rayito/v1/health.proto`) returns one procfs
  snapshot: `crates/rayd/src/grpc/health.rs` reads `/proc/stat` twice 100 ms
  apart through the `MetricsProbe` port (`crates/rayd-core/src/metrics.rs`,
  Linux adapter `crates/rayd/src/adapters/procfs_metrics.rs`). `Metrics`
  requires `x-access-token`; `Health` is the only anonymous RPC
  (`rayd_core::auth::requires_access_token`), so a new RPC is authenticated
  by default.
- The domain already has a `Clock` port (`crates/rayd-core/src/clock.rs`), a
  session phase machine (`HookPhase`: `booting → ready → running ⇄
  suspending → resumed → terminating`) and `SandboxSession::stream_gate()`,
  which is `Ok` exactly in `running`/`resumed`. `resume_generation` bumps on
  every accepted `/resume`.
- tokio timers run on `CLOCK_MONOTONIC`, which keeps advancing during a
  suspend (M0 Q19, `AWS_API_NOTES.md` §15): a periodic task fires once at
  resume with `MissedTickBehavior::Skip`. AWS corrects the wall clock at
  resume (Q41 `clock_offset_ms` 0), so wall timestamps stay usable.
- Idle is measured on bytes crossing the endpoint only; outbound or local
  activity never counts (`AWS_API_NOTES.md` §7, Q15). A procfs sampler
  therefore cannot keep a VM awake.
- The image-build snapshot is taken after `/ready` and before any `/run`
  (`AWS_API_NOTES.md` §15): anything a background task records before `/run`
  would be cloned into every sandbox.
- `list-microvms` accepts `maxResults` (1–50), `nextToken`,
  `imageIdentifier`, `imageVersion`, and returns `items[]`
  (`microvmId, state, imageArn, imageVersion, startedAt`) plus `nextToken`
  (null on the last page) — `AWS_API_NOTES.md` §1 and §6,
  `docs/aws-api/model_summary.md` `ListMicrovms`. There is no server-side
  order and no state or date filter. `TERMINATED` items linger ≥ 20 min
  (§6), so the default listing already filters client-side.
- `get-microvm-image-version` returns `resources[].minimumMemoryInMiB`
  (`docs/aws-api/model_summary.md` `GetMicrovmImageVersion`); only the e2e
  reads it.
- Python today: `Sandbox.list()` → `Iterator[SandboxListItem]` (sync) /
  `list[SandboxListItem]` (async) over `ControlPlane.list_microvms`, with the
  O(n) metadata probe of M6 (`openspec/specs/sandbox-metadata`); the shim
  `SandboxPaginator` drains it and `next_token` is always `None`. TypeScript
  today: `static async *list()` over `ControlPlane.listMicrovms` with only
  `template`/`templateVersion`/`states`.
- The E2B reference (clone `e2b-dev/E2B@ccaf9fc`): `get_metrics(start, end)`
  instance and static, `SandboxMetrics` with `mem_cache`,
  `SandboxQuery(metadata, state, started_after, template)`,
  `Sandbox.list(query, limit, next_token, order)` →
  `SandboxPaginator(has_next, next_token, next_items())`, where
  `next_items()` raises when `has_next` is `False`.

## Goals / Non-Goals

**Goals**

- A real metrics time series (5 s, whole 8 h life, gap while suspended) with
  `mem_cache`, reachable from an instance and, with the access token, from a
  sandbox id.
- Resumable, orderable, filterable listing in both SDKs with one token format.
- Guest facts (`cpu_count`, `memory_mb`, `agent_version`) on the native
  `SandboxInfo`, with the guest-vs-tier question measured and documented.

**Non-goals**

- The shim's 2.x `SandboxInfo` fields, the TypeScript `rayito/e2b` entry
  point and `rayito sandbox metrics` (`m9-e2b-v2-surface`).
- Metrics or metadata of a suspended sandbox without waking it; any
  client-side store (SPEC §4).
- Changing the snapshot `Metrics` RPC or the anonymous status of `Health`.

## D1 — Domain: `MetricsRing`, `RangeQuery`, `MetricsHistory` (rayd-core)

New module `crates/rayd-core/src/metrics_history.rs` (exported from
`lib.rs` as `pub mod metrics_history;`). Pure, no tokio/tonic types; the
only synchronisation primitive is `std::sync::Mutex`.

```rust
pub const HISTORY_SAMPLE_INTERVAL: Duration = Duration::from_secs(5);
/// 28 800 s (the non-adjustable MicroVM cap, AWS_API_NOTES §11) / 5 s.
pub const HISTORY_CAPACITY: usize = 5_760;

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct MetricsSample {
    pub unix_ms: i64,
    pub cpu_used_pct: f64,
    pub mem_used: u64,
    pub mem_total: u64,
    pub mem_cache: u64,
    pub disk_used: u64,
    pub disk_total: u64,
    pub cpu_count: u32,
}

impl MetricsSample {
    #[must_use]
    pub fn from_snapshot(snapshot: &MetricsSnapshot) -> Self;
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PushOutcome { Appended, AppendedEvictingOldest, RejectedOutOfOrder }

pub struct MetricsRing { samples: VecDeque<MetricsSample>, capacity: usize }

impl MetricsRing {
    #[must_use] pub fn with_capacity(capacity: usize) -> Self; // capacity.max(1)
    pub fn push(&mut self, sample: MetricsSample) -> PushOutcome;
    #[must_use] pub fn range(&self, query: &RangeQuery) -> Vec<MetricsSample>;
    #[must_use] pub fn oldest_unix_ms(&self) -> Option<i64>;
    #[must_use] pub fn len(&self) -> usize;
    #[must_use] pub fn is_empty(&self) -> bool;
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct RangeQuery {
    pub start_unix_ms: Option<i64>,
    pub end_unix_ms: Option<i64>,
    pub max_points: Option<NonZeroUsize>,
}

impl RangeQuery {
    /// Wire form: 0 means "unbounded" / "no reduction".
    pub fn from_wire(start_unix_ms: i64, end_unix_ms: i64, max_points: u32)
        -> Result<Self, MetricsHistoryError>;
}

#[derive(Debug, Error, Clone, PartialEq, Eq)]
pub enum MetricsHistoryError {
    #[error("start_unix_ms and end_unix_ms must be >= 0")]
    NegativeBound,
    #[error("start_unix_ms is after end_unix_ms")]
    InvertedRange,
}

#[must_use]
pub fn downsample(samples: &[MetricsSample], max_points: usize) -> Vec<MetricsSample>;

pub struct HistoryPage { pub samples: Vec<MetricsSample>, pub oldest_unix_ms: Option<i64> }

pub struct MetricsHistory { ring: Mutex<MetricsRing> }
impl MetricsHistory {
    #[must_use] pub fn new(capacity: usize) -> Self;
    pub fn record(&self, sample: MetricsSample) -> PushOutcome;
    #[must_use] pub fn query(&self, query: &RangeQuery) -> HistoryPage;
}
impl Default for MetricsHistory { /* HISTORY_CAPACITY */ }
```

Rules (each has a unit test in D13):

1. **Order invariant.** `push` appends only when `sample.unix_ms` is strictly
   greater than the last stored `unix_ms`; otherwise it returns
   `RejectedOutOfOrder` and stores nothing. The wall clock can step back when
   AWS corrects it at resume; dropping a sample is honest, re-sorting a ring
   is not. At capacity the oldest sample is evicted
   (`AppendedEvictingOldest`).
2. **Range** is inclusive on both ends: `start ≤ unix_ms ≤ end`; `None` is
   unbounded. Bounds are located with `VecDeque::partition_point` (the ring is
   sorted by rule 1).
3. **`from_wire`**: a negative `start` or `end` → `NegativeBound`; `end != 0
   && start > end` → `InvertedRange`; `max_points == 0` → `None`. A
   `max_points` above the number of selected samples is a no-op.
4. **`downsample(samples, m)`** with `n = samples.len() > m`: bucket `i ∈
   [0, m)` covers indices `[⌊i·n/m⌋, ⌊(i+1)·n/m⌋)`; each output point is the
   **last** sample of its bucket (a real timestamp and real gauge values:
   memory and disk are gauges) with `cpu_used_pct` replaced by the arithmetic
   mean of the bucket (each sample's CPU is a rate over one 5 s window, so the
   mean is the rate over the bucket). Output length is exactly `m`,
   timestamps strictly ascending. `n ≤ m` returns the input unchanged.
5. **`oldest_unix_ms`** is the oldest retained sample of the whole ring (not
   of the range), `None` when empty.
6. **Budget.** `size_of::<MetricsSample>() ≤ 64` (asserted by a test), so a
   full ring is ≤ 64 × 5 760 = 368 640 B.
7. **Poisoning.** `MetricsHistory` locks with
   `lock().unwrap_or_else(PoisonError::into_inner)`: no `unwrap`, and a panic
   elsewhere never turns metrics into a crash.

`crates/rayd-core/src/metrics.rs` changes:

- `MemoryInfo` gains `pub cached: u64`; `parse_meminfo` reads the `Cached:`
  line (the prefix match does not catch `SwapCached:`) and treats its absence
  as `0` (`MemTotal`/`MemAvailable` stay mandatory, so existing fixtures and
  behaviour are unchanged).
- `MetricsSnapshot` gains `pub mem_cache: u64`, set by `snapshot()` from
  `memory.cached`.
- New pure `pub fn unix_millis(wall: SystemTime) -> i64` (pre-epoch → 0,
  saturating to `i64::MAX`), used by `MetricsSample::from_snapshot` and by
  `grpc/health.rs` (replacing its private copy of the same arithmetic).

## D2 — Sampler: pure state plus a runtime task

Pure part, same module:

```rust
pub struct SamplerTick {
    pub resume_generation: u64,
    pub cpu: CpuTimes,
    pub memory: MemoryInfo,
    pub disk: DiskUsage,
    pub cpu_count: u32,
    pub wall: SystemTime,
}

#[derive(Debug, Default)]
pub struct MetricsSampler { baseline: Option<SamplerBaseline> }
struct SamplerBaseline { cpu: CpuTimes, resume_generation: u64 }

impl MetricsSampler {
    /// First tick after the gate opened (or after a resume) only arms the
    /// CPU baseline: a window that straddles a freeze would average real
    /// work with frozen jiffies.
    pub fn observe(&mut self, tick: &SamplerTick) -> Option<MetricsSample>;
    pub fn close_gate(&mut self);
}
```

`observe`: with a baseline of the same `resume_generation` it returns
`MetricsSample::from_snapshot(&snapshot(baseline.cpu, tick.cpu, tick.memory,
tick.disk, tick.cpu_count, tick.wall))` and moves the baseline to `tick.cpu`;
otherwise it stores the baseline and returns `None`. `close_gate` drops the
baseline. So `cpu_used_pct` of a history sample is the average over the ~5 s
since the previous sample (E2B-like), while the snapshot RPC keeps its 100 ms
window.

Runtime part, new file `crates/rayd/src/lifecycle/metrics_sampler.rs`
(re-exported from `lifecycle/mod.rs`):

```rust
#[must_use]
pub fn spawn_metrics_sampler(
    session: Arc<SandboxSession>,
    probe: Arc<dyn MetricsProbe>,
    history: Arc<MetricsHistory>,
    interval: Duration,
) -> JoinHandle<()>;
```

Loop: `tokio::time::interval(interval)` with
`MissedTickBehavior::Skip`; on every tick call `sample_once(&session,
probe.as_ref(), &history, &mut sampler)`:

1. `session.stream_gate().is_err()` (any phase but `running`/`resumed`) →
   `sampler.close_gate()` and return. This is what keeps the ring empty
   through `/ready` and the build snapshot, and what makes the suspension a
   gap: during `suspending` nothing is read; the tick that fires at resume
   (still `suspending` until `/resume` arrives) closes the gate; the next one
   re-arms; the one after that is the first post-resume sample.
2. Read `cpu_times()`, `memory()`, `disk_root()`; any `Err` →
   `sampler.close_gate()`, `tracing::debug!(reason = %error, "metrics sample
   skipped")`, return (a non-Linux host answers `Unsupported` every tick and
   the ring stays empty).
3. Build `SamplerTick { resume_generation: session.resume_generation(),
   cpu_count: probe.cpu_count(), wall: session.clock().wall(), .. }`; if
   `sampler.observe(&tick)` returns a sample, `history.record(sample)`.

`crates/rayd/src/main.rs`: after `spawn_reaper`, build `let history =
Arc::new(MetricsHistory::default());` and `let _sampler =
spawn_metrics_sampler(session.clone(), metrics.clone(), history.clone(),
HISTORY_SAMPLE_INTERVAL);`, and pass `history` in `Services`.

The sampler performs no network I/O and no allocation beyond the ring slot,
so it cannot postpone idle suspension (verified on real AWS, D14 test 2).

## D3 — gRPC: `MetricsHistory`, `Health` guest facts, `Metrics.mem_cache_bytes`

`crates/rayd/src/grpc/mod.rs`: `Services` gains `pub metrics_history:
Arc<MetricsHistory>`; `router_with_settings` passes it to
`HealthGrpc::new(session, metrics, metrics_history, kernel_status, imds)`.

`crates/rayd/src/grpc/health.rs`:

- `health`: after the existing kernel/IMDS fields,
  `snapshot.cpu_count = self.probe.cpu_count()` and
  `snapshot.memory_total_bytes = self.probe.memory().map_or(0, |memory|
  memory.total)`. `Health` never fails because of these reads.
  `crates/rayd-core/src/health.rs`: `HealthSnapshot` gains `pub cpu_count:
  u32` and `pub memory_total_bytes: u64` (default 0; `SandboxSession::health`
  leaves them 0, the adapter fills them like `kernel_ready`), and
  `to_response` copies them to fields 14 and 15.
- `metrics`: unchanged flow; `to_metrics_response` also sets
  `mem_cache_bytes: metrics.mem_cache` and uses `unix_millis`.
- `metrics_history` (new):

```rust
async fn metrics_history(
    &self,
    request: Request<MetricsHistoryRequest>,
) -> Result<Response<MetricsHistoryResponse>, Status> {
    let wire = request.into_inner();
    let query = RangeQuery::from_wire(wire.start_unix_ms, wire.end_unix_ms, wire.max_points)
        .map_err(|error| Status::invalid_argument(error.to_string()))?;
    let page = self.history.query(&query);
    tracing::debug!(rpc = "MetricsHistory", samples = page.samples.len(), "metrics history served");
    Ok(Response::new(to_history_response(&page)))
}
```

  `to_history_response` maps each sample with `sample_to_response`
  (`MetricsResponse` with all eight fields, `timestamp_unix_ms = unix_ms`) and
  `oldest_unix_ms = page.oldest_unix_ms.unwrap_or(0)`. It is not phase-gated:
  it serves memory, so it answers during `suspending` too. The access-token
  layer applies unchanged (the path is not `Health`), so a call without the
  token is `UNAUTHENTICATED`.
- Response size: ≤ 5 760 messages of ~60 B ≈ 350 KB, below the 4 MiB gRPC
  default of tonic and of the Python client (TypeScript reads up to 64 MiB).

## D4 — Logging, security and resource invariants

- `rayd` logs only counts (`samples`) and error reasons for this feature;
  metric values are not secret but are not logged either (no value in logs is
  the rule for every RPC). The SDKs never log `next_token` values (they carry
  the image ARN and a fingerprint of the metadata filter) nor metadata keys or
  values (`sandbox-metadata` requirement unchanged).
- `Health` stays the only anonymous RPC. The two new fields are the guest
  view of CPUs and `MemTotal`, which any process in the VM already reads from
  `/proc` and `sysconf`; `SECURITY.md` T4 gains one sentence saying so next
  to the existing `metadata` sentence (C-04 wording kept).
- `MetricsHistory` requires `x-access-token` (T3 defence in depth holds).
- No deferred row of `docs/SECURITY_AUDIT.md` §8 is touched: no hook change
  (C-01, C-02, C-03), no identity or filesystem path (C-05, C-06), no S3
  path (C-07), no packaging or pin change (C-10, C-12), no token-sharing
  change (C-08).

## D5 — Exact proto delta (`proto/rayito/v1/health.proto`, applied by the Contract agent)

> **Status: applied by the Contract step (2026-09-22), verbatim.** Final numbers: `HealthService.MetricsHistory`, `HealthResponse.cpu_count = 14`, `HealthResponse.memory_total_bytes = 15`, `MetricsResponse.mem_cache_bytes = 8`, `MetricsHistoryRequest` (1-3), `MetricsHistoryResponse` (1-2). The compile stubs described below are in place: `HealthGrpc::metrics_history` answers `Status::unimplemented("MetricsHistory")`, and `to_response` / `to_metrics_response` set `cpu_count: 0`, `memory_total_bytes: 0`, `mem_cache_bytes: 0`. Regenerated with `buf generate` (Python and TypeScript, byte-identical to the committed gencode for untouched protos) and `crates/rayito-proto/build.rs` (`PROTO_FILES` now lists `common`, `lifecycle`, `network`, `health`, `process`, `filesystem`, `pty`, `code`). `buf lint` passes with the unchanged `buf.yaml`, and `buf breaking --against` a copy of the pre-M9 `proto/` (FILE) reports nothing. Do not regenerate Python with `python scripts/gen_python.py`: grpcio-tools 1.84.0 emits protobuf 7.35.1 gencode plus a gRPC version-check preamble, which differs from the committed 7.36.1 output of `buf generate`.

Additive and `buf breaking --against` FILE-compatible. Field numbers follow
the M9 numbering plan: `HealthResponse` 12 (`m9-server-timeout`), 13
(`m9-egress-policy`), 14–15 (this change); `MetricsResponse` 8 (this change).

```proto
service HealthService {
  rpc Health(HealthRequest) returns (HealthResponse);
  rpc Metrics(MetricsRequest) returns (MetricsResponse);
  // Serie de métricas que `rayd` muestrea cada 5 s desde `/run`, en un
  // anillo de 5760 muestras (8 h, el techo de vida del MicroVM). Exige
  // `x-access-token`, como `Metrics`. Mientras la VM está suspendida no se
  // muestrea: la serie tiene un hueco, como la de E2B con un sandbox pausado.
  rpc MetricsHistory(MetricsHistoryRequest) returns (MetricsHistoryResponse);
}
```

Appended to `message HealthResponse` after `map<string, string> metadata =
11;` (and after 12/13 if the sibling changes already landed):

```proto
  // Vista del guest, no el tamaño de la imagen: CPUs que `rayd` puede usar
  // (`available_parallelism`) y `MemTotal` de `/proc/meminfo` en bytes. 0 si
  // no se pudieron leer o en un agente anterior a M9. No es secreto: el
  // propio sandbox lo lee de `/proc`.
  uint32 cpu_count = 14;
  uint64 memory_total_bytes = 15;
```

Appended to `message MetricsResponse` after `int64 timestamp_unix_ms = 7;`:

```proto
  // `Cached` de `/proc/meminfo` (page cache) en bytes; 0 en un agente
  // anterior a M9.
  uint64 mem_cache_bytes = 8;
```

New messages at the end of the file:

```proto
// Límites inclusivos en milisegundos Unix del reloj de pared del guest.
// `start_unix_ms = 0`: desde la muestra más antigua; `end_unix_ms = 0`: sin
// límite superior; `max_points = 0`: sin reducción. Un límite negativo, o
// `start_unix_ms > end_unix_ms` con `end_unix_ms != 0`, es INVALID_ARGUMENT.
message MetricsHistoryRequest {
  int64 start_unix_ms = 1;
  int64 end_unix_ms = 2;
  uint32 max_points = 3;
}

// Muestras en orden ascendente de `timestamp_unix_ms`. En cada muestra
// `cpu_used_pct` es la media desde la muestra anterior (≈ 5 s), no la
// ventana de 100 ms de `Metrics`. Con `max_points` cada punto es la última
// muestra de su tramo, con `cpu_used_pct` promediado en el tramo.
// `oldest_unix_ms` es la muestra más antigua retenida (0 si no hay ninguna).
message MetricsHistoryResponse {
  repeated MetricsResponse samples = 1;
  int64 oldest_unix_ms = 2;
}
```

Build consequences the Contract agent must absorb so the workspace keeps
compiling right after regeneration (this change then replaces them):
`HealthGrpc` gets a temporary `metrics_history` returning
`Status::unimplemented("MetricsHistory")` (tonic's generated trait has no
default), and the struct literals in `grpc/health.rs` (`to_response`,
`to_metrics_response`) get `cpu_count: 0, memory_total_bytes: 0` and
`mem_cache_bytes: 0`. Python (`python scripts/gen_python.py`) and TypeScript
(`buf generate`) regenerate without code breakage.

## D6 — Python metrics surface (sync and async identical)

New pure module `clients/python/src/rayito/_metrics_base.py`:

```python
HISTORY_UNIMPLEMENTED_MESSAGE: Final = (
    "la imagen es anterior a M9: rayd no tiene MetricsHistory; publica una imagen M9"
)

def unix_ms_or_zero(moment: datetime | None, *, field: str) -> int: ...
def validate_max_points(max_points: int | None) -> int: ...
def metrics_history_request(
    start: datetime | None, end: datetime | None, max_points: int | None
) -> health_pb2.MetricsHistoryRequest: ...
def metrics_history_from_proto(response: health_pb2.MetricsHistoryResponse) -> list[SandboxMetrics]: ...
def is_history_unimplemented(exc: BaseException) -> bool: ...
def history_unimplemented_error(cause: SandboxException) -> InvalidArgumentException: ...
```

- `unix_ms_or_zero`: `None` → 0; otherwise `round(moment.timestamp() * 1000)`
  (a naive `datetime` is local time, exactly E2B's documented rule); a
  negative result → `InvalidArgumentException` naming `field`.
- `validate_max_points`: `None` → 0; `bool`, non-`int` or `< 1` →
  `InvalidArgumentException("max_points debe ser un entero >= 1")`.
- `metrics_history_request`: both given and `start > end` →
  `InvalidArgumentException("start es posterior a end")` before any RPC.
- `is_history_unimplemented`: `isinstance(exc, SandboxException) and
  exc.grpc_code is grpc.StatusCode.UNIMPLEMENTED` (the existing translation
  maps `UNIMPLEMENTED` to `InvalidArgumentException` with `grpc_code` set).
- `metrics_from_proto` (`_process_base.py`) reads `mem_cache_bytes`.

Models (`_models.py`): `SandboxMetrics` gains `mem_cache_bytes: int = 0` as
its last field (default keeps positional construction compatible; 0 on a
pre-M9 image).

`sandbox_sync/main.py` `Sandbox` (async `AsyncSandbox` identical with
`async def`, `grpc.aio` and `asyncio.to_thread` for boto3):

```python
@class_method_variant("_class_get_metrics_history")
def get_metrics_history(
    self,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    max_points: int | None = None,
    request_timeout: float | None = None,
) -> builtins.list[SandboxMetrics]: ...

@classmethod
def _class_get_metrics_history(
    cls,
    sandbox_id: str,
    *,
    access_token: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    max_points: int | None = None,
    request_timeout: float | None = None,
    region: str | None = None,
    session: boto3.session.Session | None = None,
    control_plane: ControlPlane | None = None,
    transport: TransportSettings | None = None,
) -> builtins.list[SandboxMetrics]: ...
```

Instance flow: build the request (validation first), call
`self._health.MetricsHistory(request, timeout=...)` through
`_translated_unary`; if the translated exception satisfies
`is_history_unimplemented`, raise `history_unimplemented_error(exc)`
(`InvalidArgumentException`, `grpc_code` preserved) instead. Returns samples
ascending by timestamp as the agent sent them.

Class flow, in this order: `validate_sandbox_id`; `require_access_token(
access_token)` (explicit or `RAYITO_ACCESS_TOKEN`, else
`AuthenticationException`, no AWS call); build the request;
`plane.get_microvm(id)`; `TERMINATING|TERMINATED` → `terminal_state_error`
(`SandboxNotFoundException`); any other state but `RUNNING` →
`SandboxStateException(f"el sandbox {id} está {state}: leer su historial lo
despertaría; reanúdalo con connect()")` without minting a token; then a
dedicated call through a new helper next to `probe_health`:

```python
def call_dedicated_health(
    control_plane: ControlPlane,
    info: SandboxInfo,
    transport: TransportSettings,
    access_token: str,
    invoke: Callable[[health_pb2_grpc.HealthServiceStub], T],
) -> T: ...
```

It mints a single-port JWE for 8080 (`TokenRefresher(TokenStore(), ...)
.mint(...)`, no refresh timer), opens one channel with
`ProxyAuthPlugin(store, port=DEFAULT_PORT, access_token=access_token)`,
invokes once, retries once after a proxy 403 (`is_proxy_forbidden` →
`refresher.refresh_all()`), translates `grpc.RpcError` with the existing
unary translation, and closes the channel in `finally`. The async twin is
`call_dedicated_health_async`. The zero-argument `get_metrics()` snapshot and
its docstring stay as they are.

## D7 — Guest facts on `SandboxHealth` and `SandboxInfo` (Python and TypeScript)

Python:

- `SandboxHealth` gains `cpu_count: int = 0` and `memory_total_bytes: int =
  0`; `health_from_proto` copies them.
- `SandboxInfo` gains `agent_version: str | None = None`, `cpu_count: int |
  None = None`, `memory_mb: int | None = None` (docstring: guest view read
  from `Health`; `None` = not read from the agent or unknown; `memory_mb` is
  `MemTotal` of the guest in MiB, which the §16 row compares with the image's
  `minimumMemoryInMiB`).
- New pure helper in `_sandbox_base.py`:

```python
@dataclass(frozen=True)
class GuestFacts:
    agent_version: str | None
    cpu_count: int | None
    memory_mb: int | None

def guest_facts_from_health(response: health_pb2.HealthResponse) -> GuestFacts: ...
```

  `agent_version` `""` → `None`; `cpu_count` `0` → `None`; `memory_mb =
  memory_total_bytes // (1024 * 1024)`, `0` → `None` (a pre-M9 image yields
  `agent_version` set and the other two `None`).
- `_record_health` stores `self._guest = guest_facts_from_health(response)`
  on every `Health` (like `_metadata`). Instance `get_info()` returns
  `dataclasses.replace(get_microvm(...), metadata=self.metadata,
  **asdict(self._guest))` with no extra RPC. Class `get_info(sandbox_id,
  read_metadata=True)` fills the three fields from the same `Health` it
  already sends for `metadata` (only `RUNNING` and `agent_ready`), otherwise
  `None`. `sbx.info`, `launch_info` and `list()` items keep `None`.

TypeScript mirror: `SandboxHealth` gains `cpuCount: number` and
`memoryTotalBytes: number` (`healthFromProto`); `SandboxInfoFields` /
`SandboxInfo` gain optional `agentVersion?: string`, `cpuCount?: number`,
`memoryMb?: number` (same `0`/`""` → `undefined` rule in a pure
`guestFactsFromHealth(response)` in `readiness.ts`); `SandboxCore.recordHealth`
stores the facts on every call (before its generation early-return); instance
`getInfo()` returns `sandboxInfo({ ...plane.getMicrovm fields,
...core.guestFacts })`. Static `Sandbox.getInfo(id)` keeps not probing
`Health` (a pre-existing TypeScript divergence documented in
`typescript-sdk`), so its three fields are `undefined`.

## D8 — Listing core: one page port, cursor, token, filters (pure, both SDKs)

**Port.** Python `ControlPlane` (`_aws.py`) gains

```python
def list_microvms_page(
    self,
    *,
    image_arn: str | None,
    image_version: str | None,
    max_results: int,
    next_token: str | None,
) -> MicrovmListPage: ...
```

with `@dataclass(frozen=True) class MicrovmListPage: items:
tuple[SandboxListItem, ...]; next_token: str | None` in `_models.py`. The
botocore adapter calls `self._invoke("ListMicrovms",
self._client.list_microvms, maxResults=max_results, **optional)` where the
optional keys are `nextToken`, `imageIdentifier`, `imageVersion` only when
given; it does **not** filter by state (the paginator does); `max_results`
outside 1–50 → `ValueError` (programming error, never user input: the
paginator always sends 50). TypeScript `ControlPlane` gains
`listMicrovmsPage(options: { imageArn?: string; imageVersion?: string;
maxResults: number; nextToken?: string }): Promise<MicrovmListPage>` with
`interface MicrovmListPage { readonly items: readonly SandboxListItem[];
readonly nextToken: string | undefined }` over `ListMicrovmsCommand`. The
existing `list_microvms`/`listMicrovms` stay (pool, CLI and MCP use them).

**Page size.** Every page request uses `maxResults = 50`
(`LIST_MAX_RESULTS`), so page boundaries are the same whether a walk is
fresh or resumed.

**Filters** (`clients/python/src/rayito/_listing_base.py`,
`clients/typescript/src/sandbox/listing.ts`):

```python
ListOrder = Literal["asc", "desc"]

@dataclass(frozen=True)
class ListFilters:
    image_arn: str | None
    image_version: str | None
    states: tuple[str, ...] | None          # sorted; None = default
    started_after_ms: int | None
    metadata: tuple[tuple[str, str], ...] | None   # sorted by key
    order: ListOrder | None

    def fingerprint(self) -> str: ...
    def accepts(self, item: SandboxListItem) -> bool: ...
```

- `accepts`: state rule (explicit `states` → membership; `None` → drop
  `TERMINATING|TERMINATED`, the existing `_listed_state_wanted`) and
  `started_after_ms is None or unix_ms(item.started_at) >= started_after_ms`
  (E2B: "at or after"). Metadata is checked separately (it needs I/O).
- `states` and `template` stay what they are today: `template` resolves to
  the image ARN and travels as the server-side `imageIdentifier` filter
  (documented in §6; fewer pages than filtering client-side, same result),
  `states` is client-side. `metadata` requires `states ⊆ {RUNNING}`
  (`list_states_for_metadata`, unchanged).
- `fingerprint()` = first 16 hex chars of `sha256(canonical_json({"image":
  image_arn, "version": image_version, "states": list | null,
  "started_after_ms": int | null, "metadata": {k: v} | null, "order": order |
  null}))`.
- `canonical_json(value)` = Python `json.dumps(value, sort_keys=True,
  separators=(",", ":"), ensure_ascii=False)`, UTF-8 encoded; TypeScript
  `canonicalJson` = the same byte output (recursive key sort,
  `JSON.stringify` for scalars).

**Cursor and token.**

```python
@dataclass(frozen=True)
class PageCursor:
    aws_token: str | None        # the nextToken that fetches the current page; None = first page
    consumed: frozenset[str]     # item_digest of the raw items already consumed from that page

@dataclass(frozen=True)
class KeyCursor:
    started_at_ms: int
    sandbox_id: str              # last item returned (ordered walks)

def item_digest(sandbox_id: str) -> str   # sha256(utf-8).hexdigest()[:12]
def encode_next_token(fingerprint: str, cursor: PageCursor | KeyCursor) -> str
def decode_next_token(token: str) -> DecodedToken   # DecodedToken(fingerprint, cursor)
```

Token = unpadded base64url of `canonical_json(obj)` where `obj` is
`{"v": 1, "f": <fingerprint>, "a": <aws_token | null>, "s": [<sorted
digests>]}` for a page cursor and `{"v": 1, "f": <fingerprint>, "k":
[<started_at_ms>, <sandbox_id>]}` for a key cursor. `decode_next_token`
raises `InvalidArgumentException("next_token inválido")` (never echoing the
token) for: length above 8 192 chars, non-base64url, non-JSON, not an object,
`v != 1`, `f` not 16 lowercase hex, neither or both cursor forms, wrong
types, a digest not 12 lowercase hex, more than 50 digests.

**Golden vectors** (asserted literally by both SDKs' unit tests, so tokens
are interchangeable between Python and TypeScript):

- Filters `{"image": "arn:aws:lambda:us-east-1:123456789012:microvm-image:rayito-base",
  "version": null, "states": null, "started_after_ms": null, "metadata":
  {"run": "ñ1"}, "order": null}` → fingerprint `8a197ac8111983d7`;
  `item_digest("microvm-00000000-0000-0000-0000-000000000001")` =
  `0559e532996f`; page cursor `a=null, s=[that digest]` → token
  `eyJhIjpudWxsLCJmIjoiOGExOTdhYzgxMTE5ODNkNyIsInMiOlsiMDU1OWU1MzI5OTZmIl0sInYiOjF9`.
- Same image, `metadata: null`, `order: "desc"` → fingerprint
  `1f1b123b1b744c3a`; key cursor `[1790000000000,
  "microvm-00000000-0000-0000-0000-000000000002"]` → token
  `eyJmIjoiMWYxYjEyM2IxYjc0NGMzYSIsImsiOlsxNzkwMDAwMDAwMDAwLCJtaWNyb3ZtLTAwMDAwMDAwLTAwMDAtMDAwMC0wMDAwLTAwMDAwMDAwMDAwMiJdLCJ2IjoxfQ`.

**Walks** (pure state, I/O done by the sync/async callers):

```python
class PageWalk:
    def __init__(self, cursor: PageCursor) -> None: ...
    def page_to_fetch(self) -> PageRequest | None: ...   # PageRequest(aws_token) or None
    def accept_page(self, page: MicrovmListPage) -> None: ...
    def next_raw(self) -> SandboxListItem | None: ...     # pops and marks consumed
    def cursor(self) -> PageCursor: ...
    @property
    def has_more(self) -> bool: ...

class OrderedWalk:
    def __init__(self, items: Iterable[SandboxListItem], order: ListOrder, after: KeyCursor | None) -> None: ...
    def take(self, limit: int | None) -> list[SandboxListItem]: ...
    def cursor(self) -> KeyCursor | None: ...
    @property
    def has_more(self) -> bool: ...
```

- `PageWalk`: when its buffer is empty and it is not exhausted,
  `page_to_fetch` asks for the page of the cursor's `aws_token` (first call)
  or of the last page's `nextToken` (later calls, which also resets
  `consumed` to ∅). `accept_page` buffers `page.items` minus the ones whose
  digest is in `consumed` and remembers `page.next_token`. The walk is
  exhausted when the buffer is empty and the last page's `nextToken` was
  `None`. `has_more` = buffer non-empty or last `nextToken` not `None`.
  Resuming from a token therefore re-requests the page the cursor points
  into and skips the items already consumed from it by identity (not by
  position): a sandbox created or gone in between inside that page is
  neither duplicated nor skipped; like any AWS cursor, movement across page
  boundaries between calls is not guarded (documented).
- `OrderedWalk`: sort key `(started_at_ms, sandbox_id)` ascending, reversed
  for `desc`; `after` drops every item not strictly after the key in that
  order (keyset, robust to insertions); `take(limit)` serves the next slice.

## D9 — Python listing surface

`sandbox_sync/main.py`:

```python
@classmethod
def list(
    cls,
    *,
    template: str | None = None,
    template_version: str | None = None,
    states: Iterable[str] | None = None,
    metadata: Mapping[str, str] | None = None,
    started_after: datetime | None = None,
    order: ListOrder | None = None,
    region: str | None = None,
    session: boto3.session.Session | None = None,
    control_plane: ControlPlane | None = None,
    transport: TransportSettings | None = None,
    request_timeout: float = METADATA_PROBE_TIMEOUT_SECONDS,
) -> Iterator[SandboxListItem]: ...

@classmethod
def paginate(
    cls,
    *,
    template: str | None = None,
    template_version: str | None = None,
    states: Iterable[str] | None = None,
    metadata: Mapping[str, str] | None = None,
    started_after: datetime | None = None,
    order: ListOrder | None = None,
    limit: int | None = None,
    next_token: str | None = None,
    region: str | None = None,
    session: boto3.session.Session | None = None,
    control_plane: ControlPlane | None = None,
    transport: TransportSettings | None = None,
    request_timeout: float = METADATA_PROBE_TIMEOUT_SECONDS,
) -> SandboxListPaginator: ...
```

`AsyncSandbox.list(...)` keeps returning `list[SandboxListItem]` (now with
the two new kwargs); `AsyncSandbox.paginate(...)` has the identical
signature and returns `AsyncSandboxListPaginator`. Both `paginate` are plain
(non-`async`) classmethods: construction does no I/O.

`SandboxListPaginator` / `AsyncSandboxListPaginator` (new module
`clients/python/src/rayito/sandbox_sync/listing.py` and
`sandbox_async/listing.py`, exported from `rayito`):

- `has_next: bool` — `True` until a walk reports no more items.
- `next_token: str | None` — before the first call, the token passed in;
  afterwards the encoded cursor while `has_next`, else `None`.
- `next_items() -> list[SandboxListItem]` (`async` on the async class) —
  raises `SandboxException("no quedan páginas: has_next es False")` when
  `has_next` is `False`.

Construction validates, before any AWS call: `limit` (`None` or `int ≥ 1`,
else `InvalidArgumentException`), `order` (`None | "asc" | "desc"`, else
`InvalidArgumentException("order inválido ...: se esperaba 'asc' o
'desc'")`), `metadata` (`validated_metadata`) with `list_states_for_metadata`,
`started_after` (`unix_ms_or_zero`, `None` stays `None`), and
`decode_next_token` (malformed → `InvalidArgumentException`). The cursor
form is also checked here, since it needs no AWS call: a page cursor with
`order` or a key cursor without it raises `InvalidArgumentException("next_token
inválido")` at construction (Python and TypeScript alike). A key token from
the other direction (`asc` used with `desc`) has the right form and fails on
the fingerprint check below.

The first `next_items()` resolves the template ARN
(`plane.resolve_template_arn`, `asyncio.to_thread` in async), builds
`ListFilters`, and when a token was given checks `decoded.fingerprint ==
filters.fingerprint()` (mismatch → `InvalidArgumentException("next_token no
corresponde a estos filtros")`).

`next_items()` without `order`: loop until `limit` matches (or forever when
`limit is None`): if `walk.page_to_fetch()` asks for a page, fetch it with
`list_microvms_page(max_results=50, ...)` and `accept_page`; else take
`walk.next_raw()` (`None` → stop); keep it if `filters.accepts(item)` and, with
`metadata`, if the existing M6 probe (`get_microvm` → skip if not found or not
`RUNNING` → `probe_metadata` → `metadata_matches`) matches, returning the item
with `metadata` filled. `has_next = walk.has_more`.

With `order`: the first call walks every page (same per-item filter and
probe), builds `OrderedWalk(matches, order, after=decoded key or None)`, and
every call serves `take(limit)`; `has_next = ordered.has_more`, and the token
is the `KeyCursor` of the last item served. Docstring and docs state the cost:
O(pages) `ListMicrovms` calls on the first `next_items()` plus, with
`metadata`, the O(n) probe of every `RUNNING` sandbox.

`Sandbox.list(...)` streams the same walk lazily (a private generator shared
with the paginator, so metadata probes still happen as the caller iterates);
with `order` it collects first, then yields in order. Without the new
kwargs its behaviour and request sequence are unchanged.

## D10 — TypeScript surface

`clients/typescript/src/sandbox/sandbox.ts` and friends (camelCase mirror;
`ListOrder = "asc" | "desc"` exported from `index.ts` together with
`SandboxListPaginator`, `MicrovmListPage`, `MetricsHistoryOptions`,
`StaticMetricsHistoryOptions`, `SandboxPaginateOptions`):

```ts
export interface SandboxListOptions extends ControlPlaneOptions {
  readonly template?: string | undefined;
  readonly templateVersion?: string | undefined;
  readonly states?: readonly string[] | undefined;
  readonly metadata?: Readonly<Record<string, string>> | undefined;
  readonly startedAfter?: Date | undefined;
  readonly order?: ListOrder | undefined;
  readonly requestTimeoutMs?: number | undefined; // Health probe deadline, default 5000
  readonly transport?: Partial<TransportSettings> | undefined;
}
export interface SandboxPaginateOptions extends SandboxListOptions {
  readonly limit?: number | undefined;
  readonly nextToken?: string | undefined;
}

static list(options?: SandboxListOptions): AsyncIterable<SandboxListItem>;
static paginate(options?: SandboxPaginateOptions): SandboxListPaginator;

export class SandboxListPaginator {
  get hasNext(): boolean;
  get nextToken(): string | undefined;
  nextItems(): Promise<SandboxListItem[]>;
}

export interface MetricsHistoryOptions extends RequestOptions {
  readonly start?: Date | undefined;
  readonly end?: Date | undefined;
  readonly maxPoints?: number | undefined;
}
export interface StaticMetricsHistoryOptions extends MetricsHistoryOptions, ControlPlaneOptions {
  readonly accessToken?: string | undefined;
  readonly transport?: Partial<TransportSettings> | undefined;
}

getMetricsHistory(options?: MetricsHistoryOptions): Promise<SandboxMetrics[]>;
static getMetricsHistory(sandboxId: string, options?: StaticMetricsHistoryOptions): Promise<SandboxMetrics[]>;
```

- `SandboxListItem` gains `metadata?: Readonly<Record<string, string>>`
  (`undefined` = not read); `SandboxMetrics` gains `memCacheBytes: number`
  (`metricsFromProto` in `commands.ts`).
- `list()` becomes a thin `async *` over the same walks (its current output
  and request sequence are unchanged without the new options). `paginate()`
  validates like Python (`InvalidArgumentError`, thrown synchronously for a
  malformed token, bad `limit` or `order`, or `metadata` with `states` other
  than `RUNNING`).
- Metadata filter (new to TypeScript, same semantics as Python M6): for each
  candidate, sequentially, `getMicrovm` (skip on `SandboxNotFoundError` or a
  state other than `RUNNING`), then `probeHealth` from the new
  `src/sandbox/probe.ts`: a fresh `TokenStore`/`TokenRefresher` mints a
  single-port JWE for 8080 (no timer), `openTransport(info.endpoint,
  settings, proxyAuthInterceptor(store, { port: 8080, accessToken:
  undefined }))`, one `Health` with the deadline, one retry after
  `isProxyForbidden` (`refreshAll()`), `sessionManager.abort()` in
  `finally`. `agentReady === false` → skip; a failing `Health` →
  `SandboxError("no se pudieron leer los metadatos del sandbox <id>: Health no
  respondió (<name>)", { cause })`. No log line carries metadata.
- `getMetricsHistory` (instance): request built by pure helpers in the new
  `src/sandbox/metrics.ts` (`metricsHistoryRequest(options)` with the same
  validation as Python: `start > end`, `maxPoints` not an integer ≥ 1 →
  `InvalidArgumentError`; `Date` → `BigInt(date.getTime())`), `translatedUnary`
  over `clients.health.metricsHistory`, `metricsHistoryFromProto`. A
  translated error whose `grpcCode` is `Code.Unimplemented` becomes
  `InvalidArgumentError(HISTORY_UNIMPLEMENTED_MESSAGE)` (same Spanish text as
  Python).
- Static `getMetricsHistory`: `validateSandboxId`, `requireAccessToken(
  options.accessToken)` (`AuthenticationError`, no AWS call), request
  validation, `getMicrovm`, terminal → `SandboxNotFoundError`, not `RUNNING` →
  `SandboxStateError` (same message), then `withDedicatedHealthClient(plane,
  info, settings, token, (client) => client.metricsHistory(...))` from
  `probe.ts` (same mint/retry/abort as `probeHealth`, with `x-access-token`).

## D11 — E2B shim mapping (Python, `rayito.e2b`)

`e2b/_models.py`:

- `SandboxQuery(metadata: dict[str, str] | None = None, state:
  list[SandboxState] | None = None, started_after: datetime | None = None,
  template: str | None = None)` — E2B's field order.
- `SandboxMetrics` gains `mem_cache: int = 0` as last field.
- `SandboxPaginator(native: SandboxListPaginator)` and
  `AsyncSandboxPaginator(native: AsyncSandboxListPaginator)`: `has_next` and
  `next_token` delegate; `next_items()` maps with `info_from_native`.

`e2b/_compat.py` gains the pure `list_mapping(query, state, template) ->
ListMapping(states, metadata, started_after, template)`: `state` kwarg and
`query.state` both given with different sets → `InvalidArgumentException("state
y query.state difieren: usa sólo query.state")`; `query.template` and the
Rayito `template=` kwarg both given and different → the same kind of error;
states go through the existing `states_for` (so metadata with anything but
`[RUNNING]` still raises `UnimplementedError`).

`e2b/_sync.py` `Sandbox` (async identical as coroutines):

```python
@classmethod
def list(
    cls,
    *,
    api_key: str | None = None,
    query: SandboxQuery | None = None,
    state: Sequence[SandboxState] | None = None,
    limit: int | None = None,
    next_token: str | None = None,
    order: str | None = None,
    domain: str | None = None,
    debug: bool = False,
    request_timeout: float | None = None,
    proxy: str | None = None,
    template: str | None = None,
    template_version: str | None = None,
    region: str | None = None,
    session: Any | None = None,
    control_plane: Any | None = None,
    transport: Any | None = None,
) -> SandboxPaginator: ...

@class_method_variant("_class_get_metrics")
def get_metrics(
    self,
    start: datetime | None = None,
    end: datetime | None = None,
    request_timeout: float | None = None,
) -> builtins.list[SandboxMetrics]: ...

@classmethod
def _class_get_metrics(
    cls,
    sandbox_id: str,
    start: datetime | None = None,
    end: datetime | None = None,
    request_timeout: float | None = None,
    *,
    access_token: str | None = None,
    **kwargs: Any,
) -> builtins.list[SandboxMetrics]: ...
```

- `list` → `SandboxPaginator(NativeSandbox.paginate(template=mapping.template,
  template_version=..., states=mapping.states, metadata=mapping.metadata,
  started_after=mapping.started_after, order=order, limit=limit,
  next_token=next_token, ...))`. The ignored-kwarg warnings stay.
- Instance `get_metrics`: `native.get_metrics_history(start=start, end=end,
  request_timeout=...)` mapped with `metrics_from_native`. If the native call
  fails with `is_history_unimplemented`: without `start`/`end` → return
  `[snapshot]`; with either → `UnimplementedError("get_metrics(start=, end=)",
  METRICS_HISTORY_IMAGE_REASON)`. If the history is empty and neither bound
  was given → `[snapshot]` (keeps the 0.2.0 contract that
  `get_metrics()[-1]` exists; the snapshot is a real sample, not an
  approximation).
- Class `get_metrics`: token = `access_token` or `RAYITO_ACCESS_TOKEN`;
  none → `UnimplementedError("Sandbox.get_metrics(sandbox_id)",
  CLASS_METRICS_REASON)` before any AWS call, with the reason "rayd exige el
  access token del sandbox (x-access-token): pásalo con access_token= o define
  RAYITO_ACCESS_TOKEN"; otherwise `NativeSandbox.get_metrics_history(sandbox_id,
  access_token=token, start=start, end=end, **kwargs)` mapped. A pre-M9 image
  here → `UnimplementedError` with `METRICS_HISTORY_IMAGE_REASON`.
- Constants: `NEXT_TOKEN_REASON` and `METRICS_RANGE_REASON` are deleted;
  `METRICS_HISTORY_IMAGE_REASON` = "la imagen es anterior a M9 (rayd sin
  MetricsHistory): publica una imagen M9".
- `metrics_from_native` fills `mem_cache=native.mem_cache_bytes`.

## D12 — Error mapping

| Condition | rayd | Python | TypeScript |
|---|---|---|---|
| negative `start`/`end`, or `start > end` | `INVALID_ARGUMENT` | `InvalidArgumentException` before the RPC | `InvalidArgumentError` before the RPC |
| `max_points` not an int ≥ 1 | (0 on the wire = none) | `InvalidArgumentException` | `InvalidArgumentError` |
| `MetricsHistory` without / with a wrong token | `UNAUTHENTICATED` | `AuthenticationException` | `AuthenticationError` |
| image predates M9 | `UNIMPLEMENTED` (tonic, unknown method) | `InvalidArgumentException(HISTORY_UNIMPLEMENTED_MESSAGE)`, `grpc_code` kept; shim → `[snapshot]` or `UnimplementedError` | `InvalidArgumentError` (same message) |
| class history without a token | — | `AuthenticationException`, no AWS call; shim → `UnimplementedError` | `AuthenticationError`, no AWS call |
| class history on `PENDING`/`SUSPENDING`/`SUSPENDED` | — | `SandboxStateException`, no JWE minted | `SandboxStateError` |
| class history on `TERMINATING`/`TERMINATED` | — | `SandboxNotFoundException` | `SandboxNotFoundError` |
| malformed `next_token`, bad `limit`/`order` | — | `InvalidArgumentException` at `paginate()` | `InvalidArgumentError` at `paginate()` |
| `next_token` from other filters | — | `InvalidArgumentException` at first `next_items()` | `InvalidArgumentError` at first `nextItems()` |
| AWS rejects a stale `nextToken` | `ValidationException` | `InvalidArgumentException` (existing translation) | `InvalidArgumentError` |
| `next_items()` with `has_next` `False` | — | `SandboxException` | `SandboxError` |
| `metadata` with `states` other than `RUNNING` | — | `InvalidArgumentException`; shim `UnimplementedError` | `InvalidArgumentError` |
| a `Health` probe fails during the metadata filter | — | `SandboxException` naming the id | `SandboxError` naming the id |

## D13 — Tests that fail without the change

Rust, `crates/rayd-core/src/metrics_history.rs`:
`ring_appends_in_order_and_evicts_the_oldest_at_capacity`,
`ring_rejects_out_of_order_samples`,
`range_is_inclusive_and_zero_bounds_are_unbounded`,
`range_query_rejects_negative_and_inverted_bounds`,
`downsample_keeps_the_last_sample_of_each_bucket_and_averages_cpu` (n = 10,
m = 3 → timestamps of indices 2, 5, 9 and the three means),
`downsample_is_identity_under_the_limit`,
`capacity_covers_eight_hours_within_the_memory_budget` (`HISTORY_CAPACITY
× 5 s == 28 800 s`, `size_of::<MetricsSample>() <= 64`),
`sampler_arms_first_then_emits_the_window_average`,
`sampler_rearms_after_a_closed_gate_across_a_suspend_jump` (fake ticks at
5/10/15 s, `close_gate`, generation 1 at 320 s → `None`, 325 s → a sample
whose CPU uses the 320 s baseline),
`sampler_rearms_when_the_generation_changes_without_a_closed_gate`,
`history_query_reports_the_oldest_retained_sample`.
`crates/rayd-core/src/metrics.rs`: `meminfo_reads_cached_and_defaults_it_to_zero`,
`snapshot_carries_mem_cache`, `unix_millis_saturates_and_clamps_pre_epoch`.

Rust, `crates/rayd/src/lifecycle/metrics_sampler.rs` (tokio
`start_paused`, the `TestClock` pattern of `running_sleep.rs`, a fake
`MetricsProbe` with scripted jiffies):
`no_samples_before_run_then_one_every_interval`,
`no_samples_while_suspending_and_a_gap_after_resume`,
`probe_errors_skip_the_tick_and_rearm`.

Rust, new `crates/rayd/tests/m9_metrics_history.rs` (the `Harness` of
`tests/common/mod.rs` gains `pub history: Arc<MetricsHistory>`, which the
tests seed with `record`):
`metrics_history_requires_the_access_token`,
`metrics_history_filters_the_range_and_downsamples`,
`metrics_history_rejects_an_inverted_range`,
`metrics_history_reports_the_oldest_sample_and_empty_ring`,
`health_reports_guest_cpu_and_memory` (`cpu_count >= 1`; on Linux
`memory_total_bytes > 0`), and, `#[cfg(target_os = "linux")]`,
`metrics_snapshot_carries_mem_cache`.

Python (`clients/python/tests/unit`):
`test_listing_base.py` (golden vectors of D8, round trips, every malformed
token case, fingerprint mismatch, `accepts` with default/explicit states and
`started_after` boundary, `PageWalk` resume skipping consumed digests and
tolerating an inserted item, `OrderedWalk` asc/desc/tie-break/keyset);
`test_listing_sync.py` and `test_listing_async.py` over `FakeControlPlane`
pages (limit 1 walks every item exactly once; a fresh `paginate(next_token=)`
re-requests the cursor's page and continues; `order`; `states`;
`started_after`; metadata with `limit` probes only consumed items;
`next_items()` after the end raises; `list()` with the new kwargs; request
sequence unchanged without them); `test_aws.py`
(`list_microvms_page` Stubber: `maxResults 50`, `nextToken`,
`imageIdentifier`, `imageVersion` only when given; items and `nextToken`
mapped, nothing filtered); `test_metrics_history_sync.py` and
`test_metrics_history_async.py` (request fields from `datetime`s,
ascending mapping with `mem_cache_bytes`, validation before any RPC,
`UNIMPLEMENTED` → M9 message, class variant: no token → no AWS call,
`SUSPENDED` → no mint, `RUNNING` → one mint + one `MetricsHistory` with
`x-access-token`, channel closed); `test_sandbox_base.py`
(`health_from_proto` new fields, `guest_facts_from_health` zero rules);
`test_models.py` (`SandboxMetrics.mem_cache_bytes` default);
`test_e2b_compat_sync.py`, `test_e2b_compat_async.py`,
`test_e2b_compat_base.py` (the MODIFIED scenarios of `e2b-compat`). The
fake `rayd` in `tests/unit/conftest.py` gains `history:
list[health_pb2.MetricsResponse]`, `history_requests`,
`history_unimplemented: bool`, `cpu_count`, `memory_total_bytes`; every
fake implementing `ControlPlane` (`tests/unit/fake_control_plane.py` and any
other `def list_microvms` under `clients/python/tests`) gains
`list_microvms_page` with scripted pages.

TypeScript (`clients/typescript/tests/unit`): `listing.test.ts` (the same
golden vectors and malformed cases), `sandbox.test.ts` additions
(`paginate`, `list` with `metadata`/`startedAfter`/`order`, the metadata
filter against one fake `rayd` per sandbox, probe transports closed),
`metrics.test.ts` (instance and static `getMetricsHistory`, the same error
rows), `aws.test.ts` (`listMicrovmsPage` input), `readiness.test.ts`
(`healthFromProto`, `guestFactsFromHealth`). The fakes
(`tests/unit/fake/health.ts`, `fake/control-plane.ts`,
`fake/pool-plane.ts`) gain `metricsHistory`, the Health fields and
`listMicrovmsPage`.

## D14 — Real-AWS acceptance and the measurement

Prerequisite: a `rayito-base` version published from this tree (the shared
M9 image if the sibling changes publish it first), passed as
`RAYITO_TEMPLATE`/`RAYITO_TEMPLATE_VERSION`; placeholders only in tracked
files.

`clients/python/tests/e2e/test_m9_observability.py` (marker `e2e`, the M1
guardrails of `conftest.py`, every sandbox `timeout <= 1800`, swept):

1. `test_metrics_history_across_pause`: create (`idle=None`, `timeout=900`),
   `t0 = now`; `files.write` 16 MiB under `/home/user` (page cache); at
   `t0+90 s` `pause()`; sleep 30 s; `resume()`; sleep until `t0+300 s`;
   `h = get_metrics_history(start=t0, end=t0 + 300 s)`: `len(h) >= 45`,
   timestamps inside `[start, end]` and strictly ascending, ≥ 90 % of the
   consecutive deltas within 5 ± 1.5 s, exactly one delta ≥ 30 s and it
   covers the pause/resume instants, `max(mem_cache_bytes) > 0`;
   `get_metrics_history(start=t0+120 s, end=t0+180 s)` all inside;
   `get_metrics_history(max_points=10)` has exactly 10 ascending points;
   shim `rayito.e2b.Sandbox.get_metrics(start=, end=)` on the same sandbox
   returns the same count with `mem_cache`; static
   `rayito.e2b.Sandbox.get_metrics(id, access_token=token)` returns a list,
   and with `RAYITO_ACCESS_TOKEN` unset and no `access_token` raises
   `UnimplementedError` naming the access token. Prints `samples`,
   `gap_s`, `history_rpc_s`.
2. `test_sampler_does_not_block_idle_suspend`: create
   (`idle=IdlePolicy(max_idle_seconds=60)`, `timeout=900`), no client traffic,
   poll `get-microvm` (control plane, not endpoint traffic) until
   `SUSPENDED` within 60 + 120 s; `connect(id, access_token=...)` resumes it
   and the history shows a gap covering the suspension. Prints
   `idle_suspend_s`.
3. `test_pagination_order_and_filters`: three sandboxes created 2 s apart
   (`idle=None`), `t_before` taken before the first, `t_mid` between the
   first and second; pause the third. `Sandbox.paginate(template=T,
   started_after=t_before, limit=1)`: `has_next` is `True`, walking
   `next_items()` yields each created id exactly once and no id twice;
   `p = paginate(..., limit=1)`, `first = p.next_items()`,
   `rest = walk(paginate(..., limit=1, next_token=p.next_token))`: union has
   each created id once, no duplicates; `order="asc"` yields the three in
   creation order and `"desc"` in reverse; `states=["SUSPENDING",
   "SUSPENDED"]` and the shim `SandboxQuery(state=[SandboxState.PAUSED])`
   contain the third and neither of the other two; `started_after=t_mid`
   excludes the first; every item of `template=T` has `template == ARN(T)`.
   Prints `pages`, `walk_s`.
4. `test_guest_resources_vs_image_tier`: `get_info()` has `agent_version`
   non-empty, `1 <= cpu_count <= int(nproc)` (`commands.run("nproc")`) and
   `memory_mb == MemTotal_kB // 1024` read from `/proc/meminfo` in the
   sandbox; boto3 `get_microvm_image_version(imageIdentifier=ARN,
   imageVersion=V)["resources"][0]["minimumMemoryInMiB"]` is printed next to
   `guest_cpu_count`, `guest_memory_mb`, `nproc` for the §16 row.

`clients/typescript/tests/e2e/m9-observability.e2e.test.ts` (`useE2E`,
`createTestSandbox`, `timeoutMs ≤ 1 800 000`): one sandbox, write 16 MiB,
wait 60 s → `getMetricsHistory()` ≥ 9 samples, ascending, `memCacheBytes >
0`, `maxPoints: 3` → 3, a `start`/`end` window honoured, static
`Sandbox.getMetricsHistory(id, { accessToken })` returns samples;
`getInfo()` has `agentVersion`, `cpuCount`, `memoryMb`; then two sandboxes
with `metadata: { run: <uuid> }` and one with another value →
`paginate({ template, metadata: { run: <uuid> }, limit: 1 })` walks exactly
the two (metadata filled), a fresh `paginate({ ..., nextToken })` continues,
`order` asc/desc sorts them by `startedAt`.

**§16 row** (next free number at the time of writing; placeholders only):
question "¿Qué CPU y memoria ve el guest frente a `minimumMemoryInMiB` de la
versión de imagen? ¿Qué reporta `SandboxInfo`?", from-docs column "§4: el
tamaño se fija en `resources[].minimumMemoryInMiB`; Q61 vio `free -m` 8016
MiB con una imagen de 2048", measured column with the printed numbers of
test 4, the idle-suspend seconds of test 2, the history RPC time of test 1,
and the sentence "`SandboxInfo.memory_mb` es el `MemTotal` del guest, no el
tamaño de la imagen".

Commands (the gates of `CLAUDE.md` plus): `cargo test -p rayd-core
metrics_history`, `cargo test -p rayd --test m9_metrics_history`,
`RAYITO_E2E=1 uv run pytest tests/e2e/test_m9_observability.py -s`,
`RAYITO_E2E=1 pnpm test:e2e -- observability`.

## D15 — Docs to touch

- `SPEC.md` §3: "Salud" row adds `get_metrics_history` (serie de 5 s,
  hueco en pausa); "Ciclo de vida" row adds `paginate(limit, next_token,
  order, started_after)`.
- `ARCHITECTURE.md`: the rayd runtime section lists the metrics sampler next
  to the reaper (5 s, only in `running`/`resumed`, no network) and
  `HealthService` lists `MetricsHistory` (token required).
- `SECURITY.md` T4: one sentence on `cpu_count`/`memory_total_bytes` in the
  anonymous `Health` (guest facts the workload already reads from `/proc`).
- `AWS_API_NOTES.md` §6: one bullet stating that the SDKs page with
  `maxResults 50` and expose an opaque cursor over `nextToken`; §16: the row
  of D14.
- `docs/site/docs/concepts.md`: metrics history (5 s, 8 h, gap while
  suspended, guest view of `memory_mb`) and listing (paginator, token,
  `order` O(pages), filters, metadata O(n)).
- `docs/site/docs/e2b-compat.md`: rows for `get_metrics(start, end)`,
  `SandboxMetrics.mem_cache`, static `get_metrics(id, access_token=)`,
  `list(limit, next_token, order)`, `SandboxQuery.state/started_after/template`
  move to "Se mapea" (with the notes: token required for the static form,
  `order` computed client-side, resumed tokens skip by identity); delete the
  "no hay historial", "no hay cursor reanudable" rows; keep "metadatos con
  PAUSED".
- `docs/site/docs/limits.md`: ring size and memory bound, 5 s cadence,
  `ListMicrovms` page size 50.
- `clients/python/README.md`, `clients/typescript/README.md`: one example
  each (history and paginator); remove `list(next_token=)` and ranged
  `get_metrics` from the unimplemented list.
- `clients/python/CHANGELOG.md`, `clients/typescript/CHANGELOG.md`,
  `crates/rayd/CHANGELOG.md`: `[Unreleased]` entries.

## D16 — Coordination with the sibling M9 changes

- Proto numbers are fixed by the M9 plan (D5). If the Contract agent lands
  all M9 proto edits at once, `HealthResponse` 12 and 13 belong to
  `m9-server-timeout` and `m9-egress-policy`.
- This change and `m9-file-transfer`, `m9-server-timeout`,
  `m9-deno-kernels` all MODIFY the `e2b-compat` requirement "E2B features
  without an AWS primitive raise UnimplementedError". A MODIFIED block
  replaces the whole requirement at archive time, so before archiving this
  change the acceptance agent re-copies that block (and the two other
  `e2b-compat` blocks) from the then-current `openspec/specs/e2b-compat/spec.md`
  and re-applies only this change's edits (tasks.md §7).
- `m9-e2b-v2-surface` consumes: `SandboxInfo.agent_version/cpu_count/memory_mb`,
  `Sandbox.paginate`/`AsyncSandbox.paginate`, the TypeScript
  `Sandbox.paginate` and static `getMetricsHistory`. Their names are frozen by
  this design.

## Risks / Trade-offs

- **Resumed tokens re-request a page.** Reusing an AWS `nextToken` in a new
  call is not documented to be stable; the e2e (D14 test 3) exercises it. The
  identity-based skip removes the in-page duplicate/skip risk; cross-page
  drift is the same caveat any AWS cursor has and is documented.
- **`order` is O(pages).** Documented; without `order` the walk stays
  streaming.
- **First samples.** The first history sample lands ~5–10 s after `/run` and
  ~5–10 s after each `/resume` (the arming tick). The shim returns the
  snapshot when the history is still empty, so `get_metrics()[-1]` keeps
  working right after `create()`.
- **Wall-clock steps.** Out-of-order samples are dropped rather than
  reordered; a backwards correction can cost at most the samples until the
  wall clock passes the last stored timestamp.
- **Guest vs tier memory.** `memory_mb` may not equal the image tier (Q61);
  the field is documented as the guest view and the §16 row records both.

## Open Questions

None. Every decision above is closed; the only unknowns (guest vs tier
numbers, re-use of a `nextToken`, idle suspend with the sampler running) are
measured by D14 and recorded in `AWS_API_NOTES.md` §16.

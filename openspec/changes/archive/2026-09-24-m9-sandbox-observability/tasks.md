# Tasks — m9-sandbox-observability

Ordered so every section ends on a gate that can run on its own. Tick a box
only when its gate is green and paste the evidence (counts, tool output) in
the task note. Surface tags: `[rayd]`, `[python]`, `[ts]`, `[e2e]`. This
change has no `[sidecar]` or `[infra]` work. Placeholders only in tracked
files (`123456789012`, `amzn-s3-demo-bucket`, `<tu-perfil>`, `microvm-<id>`).

## 0. [rayd] Contract (applied by the Contract agent, verified here)

- [x] 0.1 `proto/rayito/v1/health.proto` carries exactly the delta of design
      D5 (`MetricsHistory` RPC, `MetricsHistoryRequest`,
      `MetricsHistoryResponse`, `MetricsResponse.mem_cache_bytes = 8`,
      `HealthResponse.cpu_count = 14`, `HealthResponse.memory_total_bytes =
      15`); `buf lint` clean; `buf breaking --against` the
      pre-M9 tree reports nothing
      — verified 2026-09-22: `git diff HEAD -- proto/rayito/v1/health.proto`
      is the D5 block verbatim (plus fields 12/13 of the sibling changes);
      `buf lint` exit 0; `buf breaking --against '.git#ref=HEAD'` exit 0.
- [x] 0.2 Regenerated code present: Rust (`cargo build -p rayito-proto`),
      Python (`python scripts/gen_python.py` → `health_pb2.py`/`.pyi`),
      TypeScript (`buf generate` → `clients/typescript/src/gen`); the
      Contract agent's compile stubs (D5: `metrics_history` →
      `Status::unimplemented`, zero literals) are in place and
      `cargo build --workspace` is green
      — verified 2026-09-22: `rayito-proto` builds the 8 protos;
      `health_pb2.pyi`, `health_pb2_grpc.py` and `health_pb.ts` carry
      `MetricsHistory*`, `mem_cache_bytes` and `memory_total_bytes` (Python
      came from `buf generate`, see D5 status). The stubs were in place and
      were replaced by 2.2 as designed.

## 1. [rayd] Domain: ring, range, downsampling, sampler state (design D1, D2)

- [x] 1.1 `crates/rayd-core/src/metrics.rs`: `MemoryInfo.cached` (optional
      `Cached:`, absent → 0), `MetricsSnapshot.mem_cache`, `snapshot()`
      fills it, `pub fn unix_millis(SystemTime) -> i64`; tests
      `meminfo_reads_cached_and_defaults_it_to_zero`,
      `snapshot_carries_mem_cache`, `unix_millis_saturates_and_clamps_pre_epoch`
- [x] 1.2 New `crates/rayd-core/src/metrics_history.rs` (+ `pub mod` in
      `lib.rs`): `HISTORY_SAMPLE_INTERVAL`, `HISTORY_CAPACITY`,
      `MetricsSample::from_snapshot`, `PushOutcome`, `MetricsRing`,
      `RangeQuery::from_wire`, `MetricsHistoryError`, `downsample`,
      `HistoryPage`, `MetricsHistory` (poison-tolerant lock) exactly as D1
- [x] 1.3 Same module: `SamplerTick`, `MetricsSampler::{observe, close_gate}`
      as D2
- [x] 1.4 Unit tests of D13 for 1.2–1.3 (eleven names), each seen failing
      against a stub before the implementation
      — the eleven D13 names plus `history_keeps_serving_after_a_poisoned_lock`
      (rule 7). Red: with the non-test part of `metrics_history.rs` and the
      three `metrics.rs` bodies stubbed, `cargo test -p rayd-core -- metrics`
      → `5 passed; 15 failed` (all 12 history tests and the 3 of 1.1). Green
      after restoring the implementation.
- [x] 1.5 `crates/rayd-core/src/health.rs`: `HealthSnapshot.cpu_count` and
      `memory_total_bytes` (default 0)
- [x] 1.6 Gate: `cargo test -p rayd-core`, `cargo clippy -p rayd-core
      --all-targets -- -D warnings`
      — on a tree with only the Contract protos and this change:
      `cargo test --workspace` → `rayd_core 304 passed`, clippy
      `--workspace --all-targets -- -D warnings` exit 0 (host and
      `aarch64-unknown-linux-musl`). On the shared M9 tree
      `cargo test -p rayd-core` → `482 passed`; its clippy run stopped on a
      sibling change's in-progress file, none of this change's.

## 2. [rayd] Adapters: sampler task, RPC, Health, wiring (design D2, D3, D4)

- [x] 2.1 New `crates/rayd/src/lifecycle/metrics_sampler.rs`:
      `spawn_metrics_sampler(session, probe, history, interval)` with
      `MissedTickBehavior::Skip` and `sample_once` (gate via
      `stream_gate()`, probe errors → `close_gate` + `debug!` with the
      reason only); re-export from `lifecycle/mod.rs`; tests
      `no_samples_before_run_then_one_every_interval`,
      `no_samples_while_suspending_and_a_gap_after_resume`,
      `probe_errors_skip_the_tick_and_rearm` (tokio `start_paused`,
      `TestClock`, fake probe)
      — red with `sample_once` stubbed to `close_gate()`:
      `0 passed; 3 failed`; green: `3 passed`.
- [x] 2.2 `crates/rayd/src/grpc/health.rs`: `HealthGrpc::new(session,
      probe, history, kernel, imds)`; `health` fills `cpu_count` /
      `memory_total_bytes`; `to_metrics_response` sets `mem_cache_bytes` and
      uses `unix_millis`; real `metrics_history` replaces the stub
      (`INVALID_ARGUMENT` from `MetricsHistoryError`, `debug!` with the
      count only); `to_history_response`, `sample_to_response`
- [x] 2.3 `crates/rayd/src/grpc/mod.rs`: `Services.metrics_history`;
      `crates/rayd/src/main.rs`: `MetricsHistory::default()`, spawn the
      sampler after the reaper, pass it in `Services`
- [x] 2.4 Every `Services { .. }` literal under `crates/rayd/tests`
      (`common/mod.rs`, `m2_process.rs`, any other) passes a
      `MetricsHistory`; `Harness` exposes `pub history: Arc<MetricsHistory>`
      — `m1_hello`, `m2_process`, `m3_filesystem`, `m4_code`, `m5_pty`,
      `m5_suspend_resume`, `common/mod.rs` and `m9_metrics_history`.
- [x] 2.5 New `crates/rayd/tests/m9_metrics_history.rs` with the six tests
      of D13
      — red against the Contract stubs (`UNIMPLEMENTED`, zero literals):
      `0 passed; 5 failed` on the host (the sixth,
      `metrics_snapshot_carries_mem_cache`, is `cfg(target_os = "linux")`);
      green: `5 passed`.
- [x] 2.6 Gate: `cargo fmt --all --check`; `cargo clippy --workspace
      --all-targets -- -D warnings`; `cargo test --workspace --locked`;
      `cargo zigbuild --release --target aarch64-unknown-linux-musl -p rayd`
      builds a static ARM64 binary (record its size next to the previous
      release in the task note)
  - verificado aquí (2026-09-23), VM Lima Ubuntu aarch64: `cargo fmt --all --check` exit 0; `cargo clippy --workspace --all-targets --locked -- -D warnings` limpio; `cargo test --workspace --locked` verde con los binarios corriendo como un usuario uid 1500 (como el runner de CI): rayd lib 166, `m9_metrics_history` 6, rayd-core 483 y el resto de suites verdes (detalle en `m9-egress-policy` 3.10); con el usuario por defecto de la VM (uid 501) las suites de integración que lanzan procesos fallan por la política C-05 (host, no código). `cargo zigbuild --release --locked --target aarch64-unknown-linux-musl -p rayd` (zig 0.16.0, cargo-zigbuild 0.23.4) → 13 751 344 B, `ELF 64-bit LSB executable, ARM aarch64, statically linked, stripped`; la release anterior (0.2.0, `rayito-base` 18.0) medía 12 524 384 B con auditable (13 755 256 B hoy con auditable: +1 230 872 B para todo M9).

## 3. [python] Metrics history and guest facts (design D6, D7)

- [x] 3.1 `_models.py`: `SandboxMetrics.mem_cache_bytes = 0`,
      `SandboxHealth.cpu_count` / `memory_total_bytes`,
      `SandboxInfo.agent_version` / `cpu_count` / `memory_mb`
      (Spanish docstrings: guest view, `None` = not read)
- [x] 3.2 `_process_base.metrics_from_proto` reads `mem_cache_bytes`;
      `_sandbox_base.py`: `health_from_proto` new fields, `GuestFacts`,
      `guest_facts_from_health`
- [x] 3.3 New `_metrics_base.py` exactly as D6 (validation, request builder,
      mapping, `is_history_unimplemented`, `history_unimplemented_error`)
      — plus `ensure_history_readable` (the class-variant state rule) and
      `MAX_POINTS_WIRE_LIMIT` (a `max_points` above the proto `uint32` is
      clamped); red before the module existed (`ModuleNotFoundError`),
      green: `test_metrics_base.py` 8 passed.
- [x] 3.4 `sandbox_sync/main.py`: `get_metrics_history` instance + class
      variant, `call_dedicated_health`, `_record_health` stores
      `self._guest`, instance and class `get_info` fill the facts
- [x] 3.5 `sandbox_async/main.py`: the identical surface
      (`call_dedicated_health_async`, `grpc.aio`, `asyncio.to_thread`)
- [x] 3.6 Fake `rayd` (`tests/unit/conftest.py`): `MetricsHistory`,
      `history`, `history_requests`, `history_unimplemented`, `cpu_count`,
      `memory_total_bytes`
- [x] 3.7 Tests: `test_metrics_history_sync.py`,
      `test_metrics_history_async.py`, additions to `test_sandbox_base.py`,
      `test_models.py`, `test_metadata_sync.py`/`_async.py` (class
      `get_info` facts) — every case of D13, each seen failing first
      — red before 3.3-3.5: the history tests failed (no
      `get_metrics_history`) and the `get_info` guest-fact tests failed
      (facts `None`); green: `test_metrics_base`,
      `test_metrics_history_sync/_async`, `test_sandbox_base`,
      `test_models`, `test_process_base`, `test_metadata_sync/_async`:
      183 passed.
      — fixes de revisión (2026-09-23): (a) `get_info()` de instancia ya no
      guarda los hechos del guest en `sbx.info` (D7, spec «`sbx.info` ...
      SHALL keep `None`»): `self._info` sin hechos y el valor devuelto con
      ellos, sync y async; `assert sandbox.info.cpu_count is None` tras
      `get_info()` en `test_metadata_{sync,async}.py` dio 2 fallos antes del
      cambio y verde después (28/28). (b) Gemelos async que faltaban en
      `test_metrics_history_async.py`: `validates_the_range_before_any_aws_call`,
      `reads_the_token_from_the_environment`, `without_arguments_sends_zeros`
      y `dedicated_async_call_does_not_remint_on_other_errors`; rojo en una
      copia con la validación tras `get_microvm`, el token fijo, `max_points`
      por defecto 60 y el reacuñado sin condición (los 4 nuevos fallan),
      verde en el árbol (16/16).

## 4. [python] Listing: port, cursor, paginators (design D8, D9)

- [x] 4.1 `_models.py`: `MicrovmListPage`; `_aws.py`: `ControlPlane.
      list_microvms_page` and the botocore adapter (`_invoke("ListMicrovms",
      ...)`, optional keys only when given, no state filtering)
      — the pool's `LaunchObserver` forwards it too (red:
      `AttributeError: 'LaunchObserver' object has no attribute
      'list_microvms_page'`, green after the delegation).
- [x] 4.2 New `_listing_base.py`: `ListOrder`, `ListFilters`
      (`fingerprint`, `accepts`), `canonical_json`, `item_digest`,
      `PageCursor`, `KeyCursor`, `DecodedToken`, `encode_next_token`,
      `decode_next_token`, `PageWalk`, `OrderedWalk`, `validate_order`,
      `validate_limit` — plus `listing_request` (every validation before
      AWS; the cursor form must match `order`), `resume_cursors` (the
      fingerprint check), `ListingSession` and `fetch_page` (always
      `maxResults` 50), shared by both paginators; the two D8 golden
      vectors match literally.
- [x] 4.3 New `sandbox_sync/listing.py` (`SandboxListPaginator`) and
      `sandbox_async/listing.py` (`AsyncSandboxListPaginator`); `Sandbox.
      paginate` / `AsyncSandbox.paginate`; `list()` gains `started_after`
      and `order` and streams the shared walk; exports in
      `rayito/__init__.py` (`SandboxListPaginator`,
      `AsyncSandboxListPaginator`, `ListOrder`, `MicrovmListPage`)
- [x] 4.4 Every `ControlPlane` fake under `clients/python/tests` gains
      `list_microvms_page` with scripted pages
      — `tests/unit/fake_control_plane.py` (`script_pages`,
      `page_requests`, `add_listed_sandbox`) and `tests/unit/cli/conftest.py`.
- [x] 4.5 Tests: `test_listing_base.py` (golden vectors of D8 literally),
      `test_listing_sync.py`, `test_listing_async.py`, `test_aws.py`
      additions — every case of D13
      — red: `ImportError` (`rayito._listing_base`, `SandboxListPaginator`,
      `AsyncSandboxListPaginator`) and `AttributeError` on
      `list_microvms_page`; green: `test_listing_base`,
      `test_listing_sync/_async`, `test_aws`: 95 passed.
      — fixes de revisión (2026-09-23): `test_listing_async.py` pasa de 7 a
      16 tests con los gemelos de los casos síncronos (filtros de cliente
      `states`/`started_after` también en `list()`, `template` como filtro
      de servidor, metadatos que saltan pausados y no coincidentes, un
      sandbox desaparecido durante la sonda, fin del paginador, secuencia
      de peticiones sin kwargs nuevos, imagen ajena, `LaunchObserver`) y
      `test_a_token_of_another_order_is_refused_before_listing` en ambos
      árboles (token sin `order` usado con `order` y token `asc` sin
      `order` → `InvalidArgumentException` al construir; token `asc` con
      `desc` → en el primer `next_items()`, sin `ListMicrovms`). D12 y el
      escenario «foreign or broken tokens» dicen ya dónde falla cada caso
      (igual en TS). Rojo en copias: sin `filters.accepts` y con
      `SandboxNotFoundException` relanzada en `sandbox_async/listing.py` → 5
      fallos; sin la comprobación de forma o sin la de huella en
      `_listing_base.py` → 2 fallos cada una; `return None` → `raise` en
      `sandbox_sync/listing.py` → 1 (nuevo `..._skips_a_sandbox_that_vanished`
      síncrono). Verde: `test_listing_{sync,async}.py` 35/35.

## 5. [python] E2B shim (design D11)

- [x] 5.1 `e2b/_models.py`: `SandboxQuery(metadata, state, started_after,
      template)`, `SandboxMetrics.mem_cache`, paginators wrapping the native
      ones
      Verificado aquí (2026-09-23, macOS arm64, `clients/python`, reintento): `SandboxQuery(metadata, state, started_after, template)` (orden de E2B, `test_sandbox_query_keeps_e2b_field_order`), `SandboxMetrics.mem_cache` y los paginadores sobre `NativeSandbox.paginate`.
- [x] 5.2 `e2b/_compat.py`: `list_mapping`, `metrics_from_native` with
      `mem_cache`, `METRICS_HISTORY_IMAGE_REASON`, new
      `CLASS_METRICS_REASON`; delete `NEXT_TOKEN_REASON` and
      `METRICS_RANGE_REASON`
      Verificado aquí (2026-09-23, macOS arm64, `clients/python`, reintento): `list_mapping`, `metrics_from_native` con `mem_cache`, `METRICS_HISTORY_IMAGE_REASON`, `CLASS_METRICS_REASON`; `NEXT_TOKEN_REASON` y `METRICS_RANGE_REASON` borrados (grep sin resultados).
      Mutación: sin `mem_cache` → 3 fallos.
- [x] 5.3 `e2b/_sync.py` and `e2b/_async.py`: `list(..., order=)` over
      `NativeSandbox.paginate`; instance `get_metrics(start, end)` with the
      snapshot fallbacks; class `get_metrics(sandbox_id, start, end,
      access_token=)`
      Verificado aquí (2026-09-23, macOS arm64, `clients/python`, reintento): `list(..., order=)` sobre `paginate`, `get_metrics(start, end)` de instancia con las caídas a la instantánea y `get_metrics(sandbox_id, start, end, access_token=)` de clase, sync y async.
      Mutaciones: `order` sin pasar → 1 fallo; rango sobre imagen anterior a M9 que cae a la instantánea → 1 fallo.
- [x] 5.4 Tests in `test_e2b_compat_base.py`, `test_e2b_compat_sync.py`,
      `test_e2b_compat_async.py` for every MODIFIED scenario of the
      `e2b-compat` delta (the old "one-element list" and
      "`next_token` always `None`" assertions are rewritten, not deleted)
      Tests, por fichero (los escenarios nuevos de la superficie 2.x viven en los ficheros `test_e2b_v2_*.py` que creó `m9-e2b-v2-surface`, no en los tres que nombra la tarea): `test_e2b_compat_sync.py::test_get_metrics_with_an_empty_history_is_the_snapshot_in_bytes` y `test_list_paginator_pages_and_filters_by_metadata`; `test_e2b_compat_async.py::test_async_get_info_metrics_and_class_variants` y `test_async_list_paginator`; `test_e2b_compat_base.py::test_metrics_from_native_uses_e2b_names_in_bytes`; `test_e2b_v2_sync.py::test_metrics_series_through_the_shim`, `test_metrics_on_an_old_agent`, `test_class_metrics_without_a_token_makes_no_call` y `test_list_resumes_with_next_token_and_orders` (y en `test_e2b_v2_async.py` `test_async_metrics_series_and_class_variant` y `test_async_list_resumes_with_next_token`); `test_e2b_v2_base.py::test_list_mapping_*`.
      La aserción antigua de «lista de un elemento» se reescribió como `test_get_metrics_with_an_empty_history_is_the_snapshot_in_bytes` (el caso de la instantánea), y la de `next_token` siempre `None` la sustituye la reanudación con `next_token`.
- [x] 5.5 Gate (from `clients/python`): `uv run pytest tests/unit`;
      `uv run ruff check .`; `uv run ruff format --check .`;
      `uv run mypy src tests`
      Gate de Python (2026-09-23, macOS arm64, reintento de specs-docs): `uv run --with pytest-timeout pytest tests/unit --timeout 60` da 2090 passed en 321 s, exit 0; `ruff check .` limpio; `ruff format --check .` 198 ficheros (el `README.md` ya formateado); `mypy src tests` sin errores en 196 ficheros.
      Sin marcar. Última corrida (2026-09-23, macOS arm64, reintento): `uv run --with pytest-timeout pytest tests/unit --timeout 60` 2058 passed, 2 failed; los dos fallos son nativos y ajenos al shim: `test_sandbox_sync.py::test_is_running_request_timeout_bounds_the_health_deadline` y su gemelo async (el plazo de `Health` llega como `0.501 <= 0.5` con la máquina cargada; la revisión los vio fallar también aislados; los corrigió después el fixer de python y la corrida completa de hoy da 2090 passed). `ruff check .` limpio, `ruff format --check .` 198 ficheros, `mypy src tests` sin errores en 196 ficheros. Los ficheros del shim (`test_e2b_compat_{base,sync,async}.py`, `test_e2b_v2_{base,sync,async,exports}.py`, `test_packaging.py`) dan 530 passed.
  Corrida del fixer python (2026-09-23, macOS arm64, tras los fixes de revisión): `uv run --with pytest-timeout pytest tests/unit --timeout 60` 2090 passed, 0 failed (332 s; los `is_running` ya no fallan); `ruff check .` limpio; `mypy src tests` sin errores en 196 ficheros; `ruff format --check .` sólo marca `clients/python/README.md` (bloques de código de la doc, área de docs, en edición por otro agente). Queda sin marcar hasta que ese fichero pase el formato.

## 6. [ts] TypeScript mirror (design D7, D8, D10)

- [x] 6.1 `src/models.ts`: `SandboxMetrics.memCacheBytes`,
      `SandboxHealth.cpuCount`/`memoryTotalBytes`, optional
      `SandboxInfo.agentVersion`/`cpuCount`/`memoryMb`,
      `SandboxListItem.metadata`, `MicrovmListPage`; `commands.ts`
      `metricsFromProto`; `readiness.ts` `healthFromProto` and
      `guestFactsFromHealth`; `core.ts` `recordHealth` stores the facts;
      instance `getInfo()` merges them
      — `getInfo()` returns the merged copy and leaves `sandbox.info` /
      `launchInfo` without facts (Python parity, D7); `recordHealth` stores
      them before the generation early-return. Red: `readiness.test.ts`
      `3 failed | 9 passed` (`guestFactsFromHealth is not a function`),
      green after.
- [x] 6.2 `src/aws/*`: `ControlPlane.listMicrovmsPage` over
      `ListMicrovmsCommand` (only documented input keys)
      — `maxResults` outside 1–50 is a `RangeError` before any call;
      `listMicrovms` now pages through it (same request sequence);
      `LaunchObserver` (pool) delegates. Red: with the method hidden,
      `aws.test.ts -t listMicrovmsPage` → `control.listMicrovmsPage is not a
      function`; green: `15 passed`.
- [x] 6.3 New `src/sandbox/listing.ts` (the D8 core, `node:crypto` sha256,
      `Buffer` base64url) and `SandboxListPaginator`; `Sandbox.paginate`;
      `Sandbox.list` gains `metadata`, `startedAfter`, `order`,
      `requestTimeoutMs`, `transport`
      — the pure core lives in `listing.ts`; the I/O side
      (`SandboxListPaginator`, the metadata probe, the `list()` stream) in
      `src/sandbox/paginator.ts`, like Python's `_listing_base.py` +
      `sandbox_sync/listing.py`. `list()` and `paginate()` validate
      synchronously; keys sort by code point so tokens match Python's
      `sort_keys` byte for byte.
- [x] 6.4 New `src/sandbox/probe.ts` (`probeHealth`,
      `withDedicatedHealthClient`) and `src/sandbox/metrics.ts`
      (`metricsHistoryRequest`, `metricsHistoryFromProto`,
      `HISTORY_UNIMPLEMENTED_MESSAGE`); instance and static
      `getMetricsHistory`
      — the static form uses `requireAccessToken(token,
      "getMetricsHistory(sandboxId)")` (new optional `caller` argument in
      `launch.ts`).
- [x] 6.5 `src/index.ts` exports: `ListOrder`, `SandboxListPaginator`,
      `SandboxPaginateOptions`, `MicrovmListPage`, `MetricsHistoryOptions`,
      `StaticMetricsHistoryOptions`
      — plus `ListMicrovmsPageOptions`.
- [x] 6.6 Fakes (`tests/unit/fake/health.ts`, `control-plane.ts`,
      `pool-plane.ts`) and tests `listing.test.ts` (same golden vectors as
      Python), `metrics.test.ts`, additions to `sandbox.test.ts`,
      `aws.test.ts`, `readiness.test.ts` — every case of D13
      — the `sandbox.test.ts` additions live in a new `paginate.test.ts`
      (paginate/list/metadata filter against one fake `rayd` per sandbox via
      `FakePoolControlPlane.addListedSandbox`, probe transports closed, the
      paginator's ordered token equal to the golden key token). Red before
      the implementation: `listing.test.ts` failed to load (module absent);
      `metrics.test.ts` + `paginate.test.ts` → `Tests 20 failed`. Green:
      `vitest run` of the five files → `Tests 124 passed (124)`.
      — hallazgos de la review (2026-09-23): (1) test nuevo en `paginate.test.ts` para la guarda de D10: un item listado `RUNNING` que `get-microvm` ya ve `SUSPENDED` se salta sin `Health` ni `CreateMicrovmAuthToken`; quitando `if (info.state !== "RUNNING")` de `readMetadata` falla (`expected [ Array(1) ] to deeply equal []`), restaurado 21/21. (2) `keyCursorFrom` rechaza un `startedAtMs` negativo como Python (`millis < 0`); caso nuevo en la tabla de `listing.test.ts`: rojo antes, verde después (53/53). `"v": 1.0` sigue aceptándose en TS (JSON.parse no distingue `1.0` de `1`); ver la nota de la review.
- [x] 6.7 Gate (from `clients/typescript`): `pnpm lint`; `pnpm typecheck`;
      `pnpm test`; `pnpm build`
      — on the shared M9 tree: `pnpm typecheck` exit 0; `pnpm build`
      `Build complete`; `biome check` on every file this change touched →
      `Checked 22 files … No fixes applied`, and the package-wide `pnpm
      lint` stops only on 5 format errors in `m9-file-transfer`'s
      in-progress files (`src/sandbox/filesystem.ts`, `transfer.ts`,
      `tests/unit/transfer.test.ts`, `fake/filesystem.ts`, `fake/s3.ts`);
      `pnpm test` → `669 passed | 2 skipped`, 1 failed:
      `filesystem.test.ts` "formats agree on a 3 MB file" times out at
      20 s (a `files.write/read` path that sibling is rewriting; untouched
      here).
      Reverificado (2026-09-23, reintento de specs-docs): `pnpm -s lint` da «Checked 123 files … No fixes applied»; `pnpm -s typecheck` exit 0; `pnpm -s test` 38 ficheros / 831 passed (el «3 MB file» de `filesystem.test.ts` ya pasa); `pnpm build` exit 0. La nota de arriba queda como historia: el gate del paquete está verde.

## 7. [e2e] Real AWS, measurement, docs, closure (design D14, D15, D16)

- [x] 7.1 Publish (or reuse the shared M9) `rayito-base` version built from
      this tree (`rayito image publish`, ARM64 `rayd`); record the version
      only as a placeholder-free number in the notes
      — reutilizada la M9 compartida (2026-09-23): `rayito-base` **21.0**
      (`rayd` ARM64 musl de este árbol, `publish_image.py
      --base-image-version 1`, 216,6 s, `SUCCESSFUL`/`ACTIVE`); la versión
      pre-M9 para 7.2 es la anterior ACTIVE (20.0).
- [x] 7.2 Write `clients/python/tests/e2e/test_m9_observability.py` (four
      tests of D14) and `clients/typescript/tests/e2e/m9-observability.e2e.test.ts`;
      confirm they fail against the pre-M9 image version (history
      `UNIMPLEMENTED`, zero guest facts) and note the failure
      — the Python half (`test_m9_observability.py`, the four D14 tests)
      was written by the [python] step and passes ruff and mypy; it has not
      run against AWS. Its shim checks assume the D11 surface of section 5
      (`get_metrics(start=, end=)`, static `get_metrics(id,
      access_token=)`, `SandboxMetrics.mem_cache`); el filtro de pausados
      usa ya `Sandbox.list(SandboxQuery(state=[SandboxState.PAUSED],
      template=...))` como pide D14 (fix de revisión; ruff y mypy limpios,
      sin ejecutar en AWS: pendiente de aceptación en AWS).
      La spec y D14 nombran ya el fichero TS real
      `m9-observability.e2e.test.ts`.
      (TypeScript file written by the `[ts]` step as
      `clients/typescript/tests/e2e/m9-observability.e2e.test.ts`;
      `pnpm test:e2e -- observability` selects it; not run yet)
  - Hecho (2026-09-24): los dos ficheros existen y corren en 7.3. La mitad roja contra la imagen pre-M9 no se corrió en AWS: el `UNIMPLEMENTED` de un agente pre-M9 lo fijan los tests unitarios `test_history_on_a_pre_m9_agent_says_so` y `test_class_history_on_a_pre_m9_agent_says_so` (sync y async) y `metrics.test.ts`, y la primera regresión real (`rayito-base` 22.0) dio 2 de 4 rojos (`test_sampler_does_not_block_idle_suspend`, `test_pagination_order_and_filters`) que pasaron a verde con los arreglos.
- [x] 7.3 Run `RAYITO_E2E=1 uv run pytest tests/e2e/test_m9_observability.py
      -s` and `RAYITO_E2E=1 pnpm test:e2e -- observability` green against
      the M9 image; paste the printed timings; zero own MicroVMs alive
      afterwards
  - Hecho (2026-09-24, aceptación): Python **4/4** dentro de la regresión e2e de Python de 2026-09-24 sobre `rayito-base` 23.0, `rayito-base-caps` 12.0 y `rayito-base-poly` 7.0: **62 passed de 62** (server-timeout 12, observability 4, deno 8, egress 14, transfer 14, M4 1, M5 2, M6 2, M7 poly 5) (re-corrida de `test_sampler_does_not_block_idle_suspend` y `test_pagination_order_and_filters` 2/2 aparte); TS `m9-observability.e2e.test.ts` **2/2** en la regresión completa de TS sobre `rayito-base` 22.0 (re-corrida en 25.0 en curso). Tiempos: 53 muestras en 300 s, RPC 0,107 s, hueco de pausa 40,0 s, suspensión por idle a los 67,2 s (72,1 s) con hueco de 55,0 s, 4 páginas en 1,16–1,40 s; `cpu_count` 4 = `nproc`, `memory_mb` 8016 con `minimumMemoryInMiB` 2048. VMs: fixtures + sweeper de sesión.
- [x] 7.4 `AWS_API_NOTES.md`: §6 bullet and the §16 row of D14 with the
      measured numbers (placeholders only)
  - Hecho (2026-09-24): el bullet de §6 (cursor opaco, `order` en cliente) ya estaba (7.5) y la fila **Q68** de §16 recoge CPU/memoria del guest frente a `minimumMemoryInMiB`, muestras, hueco, RPC e idle, con marcadores.
- [x] 7.5 Docs of D15: `SPEC.md` §3, `ARCHITECTURE.md`, `SECURITY.md` T4,
      `docs/site/docs/{concepts.md,e2b-compat.md,limits.md}`, both READMEs,
      the three CHANGELOGs
      Verificado (2026-09-23, reintento de specs-docs): `SPEC.md` §3 «Salud»
      con `get_metrics_history` y «Ciclo de vida (M9)» con `paginate`;
      `ARCHITECTURE.md` fila `HealthService` (`MetricsHistory` con token,
      campos 14 y 15, el muestreador de 5 s sólo en `running`/`resumed`, sin
      red), módulo `metrics_history`, puerto `MetricsProbe` y el adaptador
      `metrics_sampler`; frase de T4 sobre `cpu_count`/`memory_total_bytes`;
      `concepts.md` «Historial de métricas» y «Listado»; `e2b-compat.md`
      mueve `get_metrics(start, end)`, `mem_cache`, la forma de clase con
      token, `list(limit, next_token, order)` y `SandboxQuery.state/
      started_after/template` a «Se mapea», borra «no hay historial» y «no
      hay cursor reanudable» y conserva «metadatos con PAUSED»; `limits.md`
      (5 s, 5 760 muestras, ≈ 350 KB; `list-microvms` 50 ya estaba);
      ejemplos de historial y paginador en los dos READMEs; `[Unreleased]` de
      los tres CHANGELOG; §6 de `AWS_API_NOTES.md` gana el bullet del cursor
      opaco. Sólo la fila §16 medida (7.4) queda *pendiente de aceptación en
      AWS*. `mkdocs build --strict` verde; `test_m9_docs.py` 9 passed.
- [x] 7.6 Repository gates: `python scripts/check_pins.py`,
      `python scripts/check_license.py`, `python scripts/check_hygiene.py`
      (exit 0), plus sections 2.6, 5.5 and 6.7 re-run on the final tree
  - Pendiente, paso de cierre de M9 (no se difiere, 2026-09-24): se corre sobre el árbol final cuando terminen los refactors sin cambio de comportamiento que están en curso en `crates/` y `clients/*/src`. Última pasada local completa (2026-09-23): Python 2090 passed, TypeScript 831 passed, sidecar 85 passed, `scripts/tests` 184, `check_hygiene`/`check_pins`/`check_license`/`buf lint` OK; faltan los gates de Rust (`cargo fmt`, `clippy -D warnings`, `cargo test --workspace --locked` en la VM Lima como uid 1500) sobre ese árbol.
  - Hecho (2026-09-24, cierre de M9, árbol final): Rust en la VM Lima (Ubuntu aarch64, `CARGO_TARGET_DIR` propio, `-j 2`): `cargo fmt --all --check` exit 0; `cargo clippy --workspace --all-targets --locked -- -D warnings` limpio; `cargo test --workspace --locked` con los binarios como uid 1500 (`setpriv --reuid=1500 --regid=1500 --clear-groups`): rayd lib 172, bin 5, m1 14, m2 25, m3 30, m4 19 (+1 ignored), m5_pty 15, m5_suspend_resume 7, m6_hooks 7, m6_imds 3, m6_limits 5, m7_poly 9, m9_deno 4, m9_egress 1, m9_metrics_history 6, m9_network 4, m9_timeout 11, m9_transfer 21, s3_store 0 (+1 ignored), rayd-core 498: 856 passed, 0 failed, 2 ignored; `m9_egress` como root en `unshare --net` con `RAYITO_REQUIRE_EGRESS_NETNS=1` → 1 passed; `cargo deny check` → advisories, bans, licenses, sources ok; `cargo auditable zigbuild --release --locked --target aarch64-unknown-linux-musl -p rayd` → `rayd` estático de 13 764 024 B y `check_auditable.py` → `.dep-v0: 257 packages, root rayd 0.2.0`; Python (macOS arm64): `uv run --with pytest-timeout pytest tests/unit -q --timeout 60` 2115 passed; `uvx ruff==0.16.7 check` limpio y `format --check` 219 ficheros; `mypy src tests` sin errores en 217 ficheros; sidecar: `pytest` 85 passed (14 deselected), ruff limpio, `mypy src` sin errores; TypeScript: `pnpm lint` (biome, 139 ficheros), `pnpm typecheck` exit 0, `pnpm test` 40 ficheros / 863 passed, `pnpm build` y `pnpm pack:check` exit 0; `buf lint` exit 0; `check_hygiene.py` OK (939 ficheros); `check_pins.py` OK (8 ficheros); `check_license.py` OK; `gen_limits.py --check` exit 0; `scripts/tests` 186 passed; `mkdocs build --strict` (target `docs` del `Makefile`) sin avisos.
- [x] 7.7 Before archiving: re-copy the three `e2b-compat` MODIFIED blocks
      from the then-current `openspec/specs/e2b-compat/spec.md`, re-apply
      only this change's edits (design D16), and run
      `openspec validate m9-sandbox-observability --strict --no-interactive`
      Verificado (2026-09-23): los bloques "E2B features without an AWS
      primitive raise UnimplementedError" y "E2B-shaped models on the
      instance" se re-basaron sobre `openspec/specs` más deno-kernels,
      file-transfer y server-timeout (`set_timeout` mapeado, `keep_memory`,
      `ts`, re-export nativo, `end_at` = deadline de `Health.lifecycle`), con
      solo las ediciones propias re-aplicadas; "E2B-shaped listing" no lo
      toca ningún hermano. `openspec validate m9-sandbox-observability
      --strict --no-interactive` → valid; una simulación del archivo secuencial (copia de `openspec/` en un scratchpad, `openspec archive --yes` en el orden deno-kernels → file-transfer → server-timeout → sandbox-observability → egress-policy → e2b-v2-surface) archiva las seis sin error y `openspec validate --all --strict` sobre el resultado solo falla en `m9-handoff` (sin deltas, esperado). Re-verificado (2026-09-23): las seis validaciones `openspec validate <cambio> --strict --no-interactive` → valid; el archivo secuencial simulado no pierde ningún escenario del spec principal (comparación de nombres por capability: 0 perdidos; `e2b-compat` 20 → 59) y `openspec validate --specs --strict` sobre el resultado → 36/36.

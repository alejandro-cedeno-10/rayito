# sandbox-pool Specification

## Purpose
TBD - created by archiving change m7-suspended-pool. Update Purpose after archive.
## Requirements
### Requirement: PoolConfig is the immutable launch configuration shared by every slot
The Python SDK SHALL export a frozen dataclass `PoolConfig(size, template=None, template_version=None, timeout=28800, idle=IdlePolicy(), envs=None, metadata=None, cpu_time_limit=None, execution_role_arn=None, ingress=None, egress=None, logging="disabled", min_remaining_seconds=3600, fill_concurrency=4, sweep_interval_seconds=30.0, ready_timeout=90.0)` whose fields map one-to-one onto the `Sandbox.create()` kwargs of the same name; every slot of a pool SHALL be launched with these values, so `envs`, `metadata`, `cpu_time_limit`, the idle policy, the wall and the connectors are per pool, never per take. `__post_init__` SHALL raise `InvalidArgumentException` naming the field when `size` is outside `1..=64`, `timeout` fails `validate_timeout`, `idle` is `None` or has `auto_resume=False` or `max_idle_seconds >= timeout`, `min_remaining_seconds` is outside `60..=timeout-60`, `fill_concurrency` is outside `1..=8`, `sweep_interval_seconds < 5`, `ready_timeout <= 0`, or `envs`/`metadata`/`cpu_time_limit` fail the `_payload` validators. `PoolConfig` SHALL have no `access_token` field (tokens are per slot) and no `allowed_ports` field (`get_host(port)` mints per port after the take).

#### Scenario: idle policy without auto-resume is rejected
- **WHEN** a unit test builds `PoolConfig(size=2, idle=IdlePolicy(auto_resume=False))`
- **THEN** `InvalidArgumentException` is raised and its message contains `idle` and `auto_resume`

#### Scenario: size cap
- **WHEN** a unit test builds `PoolConfig(size=65)` and `PoolConfig(size=0)`
- **THEN** both raise `InvalidArgumentException` naming `size` and the range `1..=64`

#### Scenario: defaults park for the whole wall
- **WHEN** a unit test builds `PoolConfig(size=1)`
- **THEN** `timeout == 28800`, `min_remaining_seconds == 3600`, `idle == IdlePolicy()` and the launch kwargs derived from it resolve `suspended_duration_seconds` to `28800 - 300`

### Requirement: A slot is warmed with create(), parked with pause() and kept as data
`SandboxPool` SHALL warm a slot by calling `Sandbox.create()` with the pool's launch kwargs, a fresh `generate_access_token()` for that slot and `keep_on_failure=False` (readiness is therefore `agent_ready and kernel_ready`), then `pause(wait=True)` (`suspend-microvm` through the shared 2 TPS bucket, `get-microvm` polled until `SUSPENDED`), then `close()` on the local handle. A `SlotRecord(sandbox_id, access_token, endpoint, template, template_version, started_at, maximum_duration_seconds, idle, execution_role_arn, ingress, egress, region, state, parked_at)` SHALL be saved to the backend with `state="warming"` immediately after `run-microvm` returns and re-saved with `state="ready"` and `parked_at` after the park; the pool SHALL hold no live `Sandbox` handle, channel, refresher or JWE for a parked slot. `SlotRecord.__repr__` SHALL redact `access_token`.

#### Scenario: warm-up sequence against the fakes
- **WHEN** a unit test starts a `SandboxPool(PoolConfig(size=2, template=IMAGE_ARN))` over the in-memory fake control plane and the fake `rayd`
- **THEN** for each slot the fake logged `run_microvm`, `create_auth_token`, `Health` until `kernel_ready`, `get_microvm`, `suspend_microvm`, `get_microvm` until `SUSPENDED`; the two records have different `access_token`s; each launch's `runHookPayload` carries `token_sha256` equal to `access_token_sha256` of its slot's token; both records are `ready` with `parked_at` set; no fake channel remains open

#### Scenario: record saved before the park
- **WHEN** the fake control plane makes `suspend_microvm` raise on the first warm-up
- **THEN** the backend held that slot's record with `state="warming"` before the failure, the record is deleted afterwards, the VM was terminated and `stats().failed == 1`

### Requirement: take() hands out the oldest ready slot with its own token and no get-microvm
`SandboxPool.take(*, wait=0.0, ready_timeout=90.0, request_timeout=60.0, reconnect_timeout=60.0) -> Sandbox` SHALL, under the pool lock, select the `ready` record with the earliest `expires_at` whose `remaining_seconds(now) >= min_remaining_seconds`, delete it from the backend before any network call; SHALL call `resume-microvm` (5 TPS bucket) and, if it returns `False` or raises `SandboxNotFoundException`/`SandboxStateException`, terminate the VM quietly, count `lost` and fall back; SHALL open the handle with `Sandbox._open(info=sandbox_info_from_record(record), access_token=record.access_token, proxy_ports=(PortSpec.single(8080),), terminate_on_failure=True, readiness=TakePoll)` where `TakePoll` polls `Health` from 100 ms doubling to a 500 ms cap without jitter, and if the open fails count `failed` and fall back, counting `hits` only when the open succeeds; and SHALL NOT call `get-microvm` on the take path. The returned `Sandbox` SHALL report `access_token == record.access_token` and `sandbox_id == record.sandbox_id`. The readiness schedule of `create()`, `connect()` and `resume()` SHALL be unchanged.

#### Scenario: take pops the oldest and resumes explicitly
- **WHEN** a unit test parks two slots with `started_at` one minute apart and calls `pool.take()`
- **THEN** the returned sandbox is the older one, the fake logged `resume_microvm` for it and no `get_microvm` between the take and the first `Health`, the record is gone from the backend, `run_code("1+1").text == "2"`, a `get_metrics()` call presented the slot's token (`x-access-token` sha256 matches), `stats().hits == 1` and `stats().ready == 1`

#### Scenario: slot lost at take falls back
- **WHEN** the fake control plane answers `resume_microvm` for the picked slot with a conflict (`False`)
- **THEN** `take()` still returns a usable sandbox created by a plain `create()`, the fake logged `terminate_microvm` for the lost id, and `stats()` shows `lost == 1`, `misses == 1`, `hits == 0`, `takes == 1` and, once the filler is idle again, `ready == size`

#### Scenario: the take poll is fast and bounded
- **WHEN** a unit test drives `TakePoll` with a fake clock
- **THEN** the delays are 0.1, 0.2, 0.4, 0.5, 0.5 s and `create()` still uses `ReadinessPoll`'s 0.25 → 2 s schedule

### Requirement: An empty pool never blocks and never denies: wait, then fall back
When no `ready` slot with enough life exists, `take(wait=w)` SHALL wait at most `w` seconds (default 0) for the filler to park one, then SHALL run a plain `Sandbox.create()` with the pool's launch kwargs and a fresh token, count `misses` and `launched`, and return it; `takes` SHALL always equal `hits + misses`. `take()` SHALL raise `PoolClosedException` on a pool that was not started or was closed. Every `take()` SHALL signal the filler so the deficit is refilled in the background.

#### Scenario: fallback on an empty pool
- **WHEN** a unit test calls `take()` three times in a row on a started pool of `size=2` before any refill completes
- **THEN** the third call returns a sandbox whose id is not one of the two parked ids, the fake logged a third `run_microvm`, and `stats()` shows `hits == 2`, `misses == 1`, `takes == 3`

#### Scenario: wait is served by a refill
- **WHEN** the pool is empty and a unit test calls `take(wait=5)` while the fake lets a warm-up finish 1 s later
- **THEN** the call returns the freshly parked slot with `misses == 0`

#### Scenario: not started
- **WHEN** a unit test calls `take()` on a `SandboxPool` that was never started
- **THEN** `PoolClosedException` is raised

### Requirement: The filler refills in the background through the shared quotas with backoff
`start()` SHALL spawn one daemon thread that keeps `ready + warming == size` by submitting warm-ups to a `ThreadPoolExecutor(max_workers=fill_concurrency)`, waking on a warm-up end, a take, the sweep interval or `close()`. Every AWS call of the pool SHALL go through the pool's `ControlPlane` — by default the process-wide shared `LambdaMicrovmsControlPlane` — so warm-ups, takes and drains are paced by the same `RunMicrovm` 5 TPS, `SuspendMicrovm` 2 TPS, `ResumeMicrovm` 5 TPS, `TerminateMicrovm` 10 TPS and `CreateMicrovmAuthToken` 50 TPS buckets as the application's own `create()` calls. After any warm-up failure the filler SHALL sleep a `FillBackoff` delay starting at 1 s, doubling to 60 s with ±25 % jitter, reset on success, count `failed` and log one warning with the exception class and the id when known; it SHALL never stop on failures.

#### Scenario: bucket pacing and concurrency bound
- **WHEN** a unit test starts a pool of `size=6, fill_concurrency=2` over the fake control plane with real token buckets on a fake clock
- **THEN** `run_microvm` timestamps are never closer than 0.2 s and `suspend_microvm` timestamps never closer than 0.5 s, and the fake `rayd` never saw more than two channels open at once

#### Scenario: backoff after failures
- **WHEN** the fake makes the first two `run_microvm` calls raise `QuotaExceededException`
- **THEN** the filler slept 1 s then 2 s (fake sleep, jitter fixed to 0) before the third attempt, which succeeds, `stats().failed == 2`, and the log holds two warnings naming `QuotaExceededException` and no token

### Requirement: Slots are recycled before the wall and reconciled with list-microvms
Every `sweep_interval_seconds` (and once at `start()`), the pool SHALL terminate and delete every `ready` record with `remaining_seconds(now) < min_remaining_seconds` (`recycled += 1`) and refill, so that no parked slot reaches `maximumDurationInSeconds` (which counts suspended time) and every slot handed out has at least `min_remaining_seconds` of life; SHALL issue one paginated `list-microvms` filtered by the pool's image ARN (and `image_version` when set) and, per `ready` record: keep it when listed `SUSPENDED|SUSPENDING|PENDING`; call `suspend-microvm` and log a warning when listed `RUNNING`; call `get-microvm` when absent from the listing and delete the record with `lost += 1` and a warning carrying `stateReason` when the state is `TERMINATING|TERMINATED` or the VM is not found. A listing failure SHALL abort that sweep with one warning. The slot's idle policy (`suspended_duration_seconds` resolved to `timeout − max_idle_seconds`) SHALL remain the self-termination net for a slot the pool forgets.

#### Scenario: recycle with a fake clock
- **WHEN** a unit test parks one slot with `timeout=7200, min_remaining_seconds=3600`, advances the fake clock 3 700 s and lets a sweep run
- **THEN** the fake logged `terminate_microvm` for the old id, a new slot was launched and parked, and `stats()` shows `recycled == 1`, `ready == 1`, `launched == 2`

#### Scenario: terminated out of band
- **WHEN** the fake control plane marks a parked slot `TERMINATED` (so it drops out of the default listing) and a sweep runs
- **THEN** the fake logged one `get_microvm` for it, the record is deleted, `stats().lost == 1`, a replacement is warmed, and the warning names the id and the `stateReason`

#### Scenario: resumed out of band is re-parked
- **WHEN** the fake control plane marks a parked slot `RUNNING` and a sweep runs
- **THEN** the fake logged `suspend_microvm` for it, the record stays `ready`, and one warning says it was re-parked

#### Scenario: real AWS recycle before the wall
- **WHEN** the e2e starts a pool with `size=1, timeout=720, min_remaining_seconds=600, sweep_interval_seconds=10` and polls `stats()`
- **THEN** within 240 s `recycled == 1`, within 60 s more a `ready` slot with a different id exists, and `Sandbox.get_info(<first id>).state` is `TERMINATING` or `TERMINATED`

### Requirement: stats() reports counters and slots without secrets
`SandboxPool.stats()` SHALL return, synchronously and without network calls, `PoolStats(size, ready, warming, takes, hits, misses, launched, recycled, lost, failed, slots)` where `slots` is a tuple of `PoolSlotInfo(sandbox_id, state, started_at, expires_at, parked_at)`; neither type SHALL carry an access token or a JWE. Counters SHALL be cumulative since `start()`: `hits` takes served from a slot, `misses` fallbacks, `launched` every accepted `run-microvm` (warm-ups and fallbacks), `recycled` sweep recycles, `lost` slots dropped at reconciliation, at take or at recovery, `failed` warm-ups and takes that raised.

#### Scenario: scripted sequence
- **WHEN** a unit test runs, on a pool of `size=2`: fill, one take, one fallback take, one recycle, one out-of-band loss
- **THEN** `stats()` equals `size=2, ready=2, warming=0, takes=2, hits=1, misses=1, launched=6, recycled=1, lost=1, failed=0` once the filler is idle, and `slots` has two entries with `state == "ready"` and no attribute named `access_token`

### Requirement: close() drains by default and only a persistent backend may keep slots
`SandboxPool.close(*, drain=True)` SHALL stop and join the filler (≤ 30 s; a warm-up in flight completes its `create()` and terminates its VM instead of parking it), and with `drain=True` SHALL terminate every `ready` slot through the 10 TPS bucket and delete its record; `drain=False` SHALL leave `ready` records and their VMs parked and SHALL raise `InvalidArgumentException` when `backend.persistent` is `False`. `close()` SHALL be idempotent, `take()` after it SHALL raise `PoolClosedException`, `__enter__` SHALL call `start()` and `__exit__` SHALL call `close(drain=True)`. The pool SHALL define no `__del__` finaliser; the docs SHALL state that a pool never closed leaves `size` suspended VMs that self-terminate at their `timeout`.

#### Scenario: drain terminates everything
- **WHEN** a unit test uses `with SandboxPool(PoolConfig(size=3, ...)) as pool:` and lets it fill
- **THEN** on exit the fake logged `terminate_microvm` for the three ids, the backend is empty, the filler thread is gone and a later `take()` raises `PoolClosedException`

#### Scenario: no-drain needs persistence
- **WHEN** a unit test calls `close(drain=False)` on a pool with the in-memory backend and on a pool with a `JsonFilePoolBackend`
- **THEN** the first raises `InvalidArgumentException` mentioning `drain` and `persistent`, and the second returns with the records still in the file and no `terminate_microvm` logged

### Requirement: PoolBackend is the pluggable seam, with an in-memory default and a 0600 JSON file backend
The SDK SHALL define `PoolBackend` as a `Protocol` with `persistent: bool`, `load() -> tuple[SlotRecord, ...]`, `save(record)` (upsert by `sandbox_id`) and `delete(sandbox_id)` (missing id is not an error); the pool SHALL serialise every backend call under its own lock and SHALL NOT require the backend to be thread-safe or multi-process safe. `InMemoryPoolBackend` (`persistent=False`) SHALL be the default. `JsonFilePoolBackend(path)` (`persistent=True`) SHALL store `{"schema": "rayito.pool/1", "slots": [...]}` with the access tokens in clear, dates as ISO-8601 UTC and `idle` as its three fields, SHALL return `()` for a missing file and raise `InvalidArgumentException` for another `schema`. Because that file holds every parked secret in clear, the write SHALL be atomic **and** unhijackable: the document SHALL be written to a temporary whose name is unpredictable (a random component in the same directory as the target, so `os.replace`/`rename` stays atomic), created exclusively so an existing file or symlink of that name is never reused (`O_CREAT|O_EXCL|O_WRONLY` with `O_NOFOLLOW` where the platform has it, `tempfile.mkstemp` in Python, flag `wx` in Node), with mode `0o600` applied to the open descriptor before the first byte is written (`os.fchmod` / `FileHandle.chmod`), and promoted with `os.replace`/`rename`; any failure SHALL remove the temporary and re-raise, so the directory holds only the state file afterwards. Reads SHALL refuse to follow a symlink at the state file's own path where the platform offers `O_NOFOLLOW`, raising `InvalidArgumentException`/`InvalidArgumentError` saying the state file must be a regular file. The TypeScript `JsonFilePoolBackend` SHALL apply the same rules and write the same schema so a file is interchangeable between SDKs. The docs SHALL state the file backend is single-process, for tests and same-host recovery, and as sensitive as `RAYITO_ACCESS_TOKEN`.

#### Scenario: JSON round-trip and permissions
- **WHEN** a unit test saves two records through `JsonFilePoolBackend`, reloads them with a new instance, and inspects the file on POSIX
- **THEN** the loaded records equal the saved ones, no temporary remains beside it, the mode is `0o600`, and the file text contains both tokens and the `schema` key

#### Scenario: a pre-created temporary is never reused
- **WHEN** another user creates `<path>.tmp` with mode `0o666` and known content, and the pool then saves a record (in either SDK)
- **THEN** the state file is a new inode with mode `0o600` holding the pool document, and the pre-created file still holds its original bytes

#### Scenario: a symlinked temporary truncates nothing
- **WHEN** `<path>.tmp` is a symlink to another file the process can write and the pool saves a record
- **THEN** the target of the symlink is untouched

#### Scenario: a symlinked state file is refused on read
- **WHEN** the state file's own path is a symlink to a valid `rayito.pool/1` document and `load()` runs on POSIX
- **THEN** it raises `InvalidArgumentException` (`InvalidArgumentError` in TypeScript) saying the state file must be a regular file, and no record is trusted

#### Scenario: foreign schema
- **WHEN** the file holds `{"schema": "other/1", "slots": []}`
- **THEN** `load()` raises `InvalidArgumentException` naming the schema

### Requirement: start() recovers records from a persistent backend before serving them
`start()` SHALL `load()` the backend; every `warming` record SHALL be treated as an orphan (its VM terminated, the record deleted, `lost += 1`); every `ready` record SHALL go through one sweep (recycle + reconcile) before it can be taken, after which it counts as `ready` with `launched` unchanged. The async pool SHALL call a persistent backend through `asyncio.to_thread`.

#### Scenario: recovery after a simulated crash
- **WHEN** a unit test writes one `warming` and one `ready` record (both alive in the fake control plane) into a `JsonFilePoolBackend` file and starts a pool of `size=1` on it
- **THEN** the fake logged `terminate_microvm` for the `warming` id only, `stats()` shows `ready == 1`, `lost == 1`, `launched == 0`, and `take()` returns the recovered id with `run_code("1+1").text == "2"`

#### Scenario: real AWS recovery
- **WHEN** the e2e parks one slot on a `JsonFilePoolBackend`, closes the pool with `drain=False`, and starts a new pool on the same file
- **THEN** the new pool reports `ready == 1` with the same id and `launched == 0`, `take()` returns it and `run_code("1+1").text == "2"`

### Requirement: Sandbox.create(pool=) is sugar for take() and rejects launch kwargs
`Sandbox.create(pool: SandboxPool | None = None, ...)` and `AsyncSandbox.create(pool: AsyncSandboxPool | None = None, ...)` SHALL, when `pool` is given, return `pool.take(ready_timeout=..., request_timeout=..., reconnect_timeout=...)` and SHALL raise `InvalidArgumentException` naming the first of `template`, `template_version`, `timeout`, `idle`, `envs`, `metadata`, `cpu_time_limit`, `execution_role_arn`, `allowed_ports`, `ingress`, `egress`, `logging`, `region`, `session`, `access_token`, `keep_on_failure`, `control_plane`, `transport` that differs from its default. `pool` SHALL accept a pool object, not a `PoolConfig`, and `create()` SHALL NOT start the pool.

#### Scenario: sugar and rejection
- **WHEN** a unit test calls `Sandbox.create(pool=pool)` on a started, filled pool, and then `Sandbox.create(pool=pool, envs={"A": "1"})`
- **THEN** the first returns a sandbox from a slot (`stats().hits == 1`) and the second raises `InvalidArgumentException` whose message contains `envs`

#### Scenario: knobs pass through
- **WHEN** a unit test calls `Sandbox.create(pool=pool, request_timeout=7.5)`
- **THEN** the returned sandbox uses a 7.5 s request timeout (asserted through a unary deadline seen by the fake `rayd`)

### Requirement: Async parity of the pool
`AsyncSandboxPool` SHALL offer `start()`, `take()`, `close()`, `__aenter__`/`__aexit__` as coroutines and `stats()` synchronously, with the same configuration, slot life cycle, take path, fallback, refill (an `asyncio.Task` bounded by `asyncio.Semaphore(fill_concurrency)`), sweep, recovery, counters, logging and secret rules as `SandboxPool`, using `AsyncSandbox.create()`, `await pause(wait=True)` and `await close()` for warm-ups. `tests/unit/test_pool_async.py` SHALL mirror `test_pool_sync.py` case by case.

#### Scenario: async take
- **WHEN** an async unit test does `async with AsyncSandboxPool(PoolConfig(size=1, ...)) as pool:` over the fakes, awaits `stats().ready == 1`, and `sbx = await pool.take()`
- **THEN** `(await sbx.run_code("1+1")).text == "2"`, `stats().hits == 1`, and on exit the slot is terminated

### Requirement: The pool never logs or exposes a secret
The pool SHALL log slot transitions at `INFO` (id, new state, milliseconds) and failures, recycles, losses and out-of-band resumes at `WARNING` (id, exception class, `stateReason`), and SHALL NOT log or expose through `stats()`, `PoolSlotInfo`, `SlotRecord.__repr__` or exception messages any access token, JWE, `envs`, `metadata` value or `runHookPayload`.

#### Scenario: caplog holds no secret
- **WHEN** a unit test fills a pool, takes a slot, loses one and drains, with `caplog` at `DEBUG`
- **THEN** no log record contains any slot's access token, the fake JWE, or the string `token_sha256`

### Requirement: Custody of the slot secrets is documented as SECURITY.md T14
`SECURITY.md` SHALL gain row **T14** ("custodia de secretos del pool", milestone M7) stating: the token that opens a slot is the one the pool minted, for the whole life of that VM, because its sha256 is fixed in `runHookPayload` at `/run` and `/run` is accepted once per boot (no rotation at `take()`); one fresh 32-byte secret per slot; the secret lives in the pool process or in the `0600` JSON file until `take()` deletes the record; the pool operator can read every parked secret, which is the same trust level as the SDK operator; the JWE is minted at take and never stored; residuals (process dump or file exposure, parked ids visible to `ListMicrovms`, out-of-band `ResumeMicrovm` re-parked within a sweep). `ARCHITECTURE.md` ADR-008 consequence (2) SHALL be amended with one sentence pointing at `m7-suspended-pool` D4/D14 and T14, and `docs/site/docs/pool.md` SHALL carry the same section.

#### Scenario: the three documents agree
- **WHEN** a reviewer opens `SECURITY.md`, `ARCHITECTURE.md` ADR-008 and `docs/site/docs/pool.md`
- **THEN** T14 exists with the M7 column, ADR-008 (2) no longer calls the token hand-off an open point and cites D4/D14 and T14, and `pool.md` has a "Custodia del secreto" section stating one secret per slot, deletion at take and no rotation

### Requirement: pool.md, the cost model and the docs carry the measured numbers
`docs/site/docs/pool.md` SHALL exist (Spanish, in the mkdocs nav as "Pool de sandboxes" after "Modelo de costes") with: why suspended and not running (ADR-008 numbers), when it pays, the sync/async/TypeScript API, `take()` and fallback semantics, refill and quotas, recycling and `list()` visibility, backends and their limits, custody, a sizing rule (`size` ≈ peak sandboxes needed inside one ≈ 8 s refill window; larger bursts fall back to `create()` at 5–6 s), the regional-quota note (2 GB per slot against 1 024 GB) and a cost table per slot for `rayito-base` (0.92 GB): storage ≈ $0.074/month, park ≈ $0.0035, take ≈ $0.0014, warm-up ≈ $0.0017, recycle every ≈ 7 h ≈ $0.005 ⇒ ≈ $0.52/month, total idle slot ≈ $0.6/month versus $91/month for a `RUNNING` VM, every number traceable to `AWS_API_NOTES.md` §12 and `docs/benchmarks/2026-09-cold-start.md`, plus the measured `T_take` and `T_create`. `docs/site/docs/cost.md` SHALL gain one row linking `pool.md`; `README.md` a six-line pool snippet; `clients/python/CHANGELOG.md` and `clients/typescript/CHANGELOG.md` an `Unreleased / Added` entry; `SPEC.md` §4 SHALL say the suspended pool ships in `m7-suspended-pool` and the pre-warmed one stays out; `MILESTONES.md` M7 row 3 SHALL carry `T_take`/`T_create` p50/p95, the run cost and the date; `AWS_API_NOTES.md` §16 SHALL gain row Q53 with the same numbers and the client RTT; `ARCHITECTURE.md` Capa 3 SHALL gain a "Pool de suspendidos (SDK)" paragraph. `mkdocs build --strict` SHALL pass.

#### Scenario: strict build with the new page
- **WHEN** CI runs `cd clients/python && uv run --group docs mkdocs build -f ../../docs/site/mkdocs.yml --strict`
- **THEN** the build succeeds, `pool.md` is in the nav after `cost.md`, and the API page renders `rayito.SandboxPool.take`

#### Scenario: numbers agree
- **WHEN** a reviewer compares `pool.md`, `MILESTONES.md` M7 row 3 and `AWS_API_NOTES.md` Q53
- **THEN** the three quote the same `T_take` and `T_create` p50/p95 and the same run date

### Requirement: The pool is accepted against real AWS with the rule fixed before measuring
`clients/python/tests/e2e/test_m7_pool.py` (marker `e2e`, guarded by `RAYITO_E2E=1` and `RAYITO_TEMPLATE`, every VM `timeout <= 1800`, swept by the e2e conftest) SHALL contain `test_pool_take_latency`: a pool of `size=3, timeout=900, min_remaining_seconds=300, sweep_interval_seconds=10, fill_concurrency=3`; twenty times, after polling `stats().ready >= 1`, time `take()` → `run_code("1+1", timeout=60)` with `text == "2"` and `kill()`; then twenty sequential `Sandbox.create(...)` → the same cell → `kill()`; compute nearest-rank p95 `T_take` and `T_create`, print both with p50/min/max, the client RTT and `stats()`; **assert `T_take < 1.5`**, `hits == 20`, `misses == 0`; `close(drain=True)`; assert `Sandbox.list(template=...)` shows none of the 43 ids in `PENDING|RUNNING|SUSPENDING|SUSPENDED`. The change SHALL NOT be accepted on a run where `T_take >= 1.5 s`; the rule SHALL NOT be changed, only the design amended and re-run.

#### Scenario: acceptance run
- **WHEN** `make test-e2e` runs the three pool tests in one sitting against the current `rayito-base` from the usual client (RTT ≈ 85–95 ms)
- **THEN** `T_take < 1.5 s` with `hits == 20` and `misses == 0`, `T_create` is printed next to it (expected ≈ 5–6 s), the recycle and recovery tests pass, and `list-microvms` shows no live VM of the template afterwards


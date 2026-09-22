## Context

State after M6 and the two M7 hygiene tracks (2026-09-16): `rayito` 0.1.0
(Python, sync + async over shared pure helpers), the TypeScript SDK
(Connect-ES v2), `rayd` 0.1.0 in `rayito-base` 17.0, ADR-008 decided. What
the pool builds on, all of it measured (`docs/benchmarks/2026-09-cold-start.md`,
`AWS_API_NOTES.md`):

- **Cold start**: `create()` → `kernel_ready` p50 5.2 s / p95 6.1 s
  sequential, p50 5.7 s / p95 8.6 s in an SDK burst of 20 (≈ 3 s of it the
  5 TPS `RunMicrovm` bucket). The first cell costs one RTT (0.10 s) in every
  state.
- **Resume**: explicit `resume()` (`resume-microvm` + re-mint + `Health`
  with a new generation) p50 0.376 s / p95 0.400 s; `resume(wait=False)`
  alone 0.27 s; auto-resume through the proxy p50 0.666 s / p95 0.682 s;
  `pause()` → `SUSPENDED` 1.01–1.04 s. Kernel state survives 20/20 cycles.
- **Cost**: a suspended VM bills snapshot storage only ($0.08/GB-month,
  0.92 GB for `rayito-base` ⇒ $0.074/slot/month), a suspend writes ≈ the
  memory snapshot ($0.0038/GB ⇒ $0.0035), a launch or a resume reads it
  ($0.00155/GB ⇒ $0.0014); a `RUNNING` 2 GB VM costs $0.126/h ⇒ $91/month.
- **Wall**: `maximumDurationInSeconds` (≤ 28 800 s) counts running **and**
  suspended time from `startedAt`; there is no `UpdateMicrovm` (ADR-007). A
  VM `SUSPENDED` longer than `idlePolicy.suspendedDurationSeconds`
  terminates itself; without an `idlePolicy` the behaviour of an
  API-suspended VM over time is not documented, so the pool never runs
  without one.
- **Auth**: the access token's sha256 travels in `runHookPayload`, `/run` is
  accepted once per boot and the hash cannot change afterwards (ADR-004,
  `SECURITY.md` T2/T4). A JWE minted before a suspension is still valid
  after the resume; expiry is wall-clock (60 min, refresh at 45).
- **Quotas** (§11, applied in the account): `RunMicrovm` 5, `ResumeMicrovm`
  5, `SuspendMicrovm` **2**, `TerminateMicrovm` 10, `GetMicrovm` 100,
  `CreateMicrovmAuthToken` 50 TPS; regional memory 1 024 GB
  (`RUNNING + SUSPENDED`). The SDK already serialises every operation
  through per-process `TokenBucket`s in the shared `LambdaMicrovmsControlPlane`.
- **Consistency**: `get-microvm` is eventually consistent; readiness is
  `Health`; `list-microvms` items carry `microvmId, state, imageArn,
  imageVersion, startedAt` (no endpoint) and keep `TERMINATED` entries for
  ≈ 20 min.
- **SDK internals the pool reuses**: `build_launch_plan` (one access token
  per plan, `runHookPayload` with its hash), `Sandbox._open` (mint JWE, open
  channel, `_wait_until_ready` with `ReadinessPoll` 0.25 s doubling to 2 s),
  `pause(wait=True)`, `TokenRefresher`, `terminate_quietly`, the
  `ControlPlane` protocol (`run_microvm`, `get_microvm`, `list_microvms`,
  `suspend_microvm`, `resume_microvm`, `terminate_microvm`,
  `create_auth_token`), `shared_control_plane`. The TypeScript SDK mirrors
  every one of them.

Constraints: `openspec/project.md` hard rules (no invented AWS parameter:
every call below is one of the seven already used; no `.proto` change; one
change at a time; a milestone never closes on mocks; English identifiers;
no inline comments; sync/async parity; strict TypeScript; never log tokens),
the ADR-008 consequences (8 h wall including suspended time; token minted
by the pool owner and inherited by the taker; `envs` per pool, not per
sandbox; slots consume the memory quota and appear as `SUSPENDED` in
`list()`; a slot parked longer than `suspended_duration_seconds`
terminates itself), the research report's acceptance (p95 take → first cell
< 1.5 s over 20 takes; slots recycled before the wall) and the owner's rule
of keeping every test VM short and every e2e run under a dollar.

Coordination with the other M7 tracks being written in parallel
(`m7-s3-persistence`, `m7-mcp-server`, `m7-cli`, `m7-poly-kernels`): this
design names the new `AWS_API_NOTES.md` §16 row **Q53** and the new
`SECURITY.md` row **T14** on the assumption that it lands first —
`m7-s3-persistence` claims the same two numbers for its throughput
measurement and its S3 row; whoever lands second renumbers (the M6 rule).
The `python-release` "Docs site skeleton" requirement is modified here
(`pool.md`) and by `m7-cli` (`cli.md`), and `m7-s3-persistence`,
`m7-mcp-server` and `m7-poly-kernels` add pages of their own: whoever
archives second merges the nav list, keeping every page in the order
"after `cost.md`" for `pool.md`. `Sandbox.create()` gains `pool=` here
and `persist=` in `m7-s3-persistence`; the two kwargs are independent and
`pool=` rejects `persist=` like any other launch kwarg once both exist.
Nothing in the pool depends on the other tracks.

## Goals / Non-Goals

**Goals:**

- A sandbox usable in well under 1.5 s (p95) from `take()` for anyone who
  keeps N slots parked, at a monthly cost per idle slot that is storage plus
  the recycle churn, not compute.
- The token hand-off closed in writing: who mints, where the secret lives,
  when it is forgotten, what a leak exposes, and why no rotation exists.
- Quotas respected by construction (the pool goes through the same buckets
  as everything else) and never a tight loop on failure.
- No slot ever handed out past its useful life; no slot leaked knowingly
  (drain on close, recovery from a persistent backend, self-termination at
  the wall as the last resort).
- Python sync and async with one pure core; TypeScript with the same
  semantics; the `rayito.e2b` shim untouched.
- A measured acceptance against real AWS with the rule fixed here.

**Non-Goals:**

- Any server-side component (Lambda, DynamoDB, EventBridge): a later
  shared store plugs into the `PoolBackend` interface; it is not designed
  here beyond that seam.
- Multi-process or multi-host coordination over the JSON backend (no
  locking; single process by contract).
- Rotating the access token at `take()` (impossible without a second `/run`,
  which `rayd` rejects by design, T2) or storing the JWE in the backend.
- Changing the 5 TPS `RunMicrovm` bucket, the readiness schedule of
  `create()`/`connect()`, the image warm-up, `rayd`, the `.proto` or the
  hooks. ADR-008 asks for a burst measurement before touching the bucket;
  that is a separate change.
- Per-take `envs`, `metadata` or `cpu_time_limit` (fixed at `/run`, ADR-008
  consequence 3); per-take `template` (a pool is one template and version).
- Pre-warmed (`RUNNING`) slots; a pool that also keeps idle *taken*
  sandboxes; returning a sandbox to the pool (`release()`): a taken sandbox
  is the taker's until `kill()`.
- A `rayito.e2b` mapping for E2B's template pools: E2B has none in its SDK.

## Decisions

### D1. Surface, modules and exports (Python)

Public names, exported from `rayito`: `PoolConfig`, `SandboxPool`,
`AsyncSandboxPool`, `PoolStats`, `PoolSlotInfo`, `PoolBackend`,
`InMemoryPoolBackend`, `JsonFilePoolBackend`, `PoolClosedException`
(subclass of `SandboxException`). Modules, following the repo's sync/async
convention:

- `rayito/_pool_base.py` — pure: `PoolConfig` validation, `SlotRecord`
  (dataclass, `repr` redacts `access_token`), record ↔ JSON dict, slot
  selection (`pick_ready_slot`), the recycle rule (`slot_is_stale`), the
  reconciliation rule (`reconcile_action`), the failure backoff
  (`FillBackoff`), `PoolStats` arithmetic, `sandbox_info_from_record`. No
  I/O, no threads.
- `rayito/_pool_backends.py` — `PoolBackend` protocol,
  `InMemoryPoolBackend`, `JsonFilePoolBackend` (sync file I/O; the async
  pool wraps it with `asyncio.to_thread`).
- `rayito/sandbox_sync/pool.py` — `SandboxPool` (threads).
- `rayito/sandbox_async/pool.py` — `AsyncSandboxPool` (asyncio tasks).

```python
@dataclass(frozen=True)
class PoolConfig:
    size: int
    template: str | None = None            # None → RAYITO_TEMPLATE
    template_version: str | None = None
    timeout: int = MAX_DURATION_SECONDS    # 28 800: park as long as AWS allows
    idle: IdlePolicy = DEFAULT_IDLE_POLICY # must have auto_resume=True
    envs: Mapping[str, str] | None = None
    metadata: Mapping[str, str] | None = None
    cpu_time_limit: int | None = None
    execution_role_arn: str | None = None
    ingress: Sequence[str] | None = None
    egress: Sequence[str] | None = None
    logging: LoggingOption = "disabled"
    min_remaining_seconds: int = 3600      # never hand out less life than this
    fill_concurrency: int = 4              # warm-ups in flight
    sweep_interval_seconds: float = 30.0
    ready_timeout: float = DEFAULT_READY_TIMEOUT_SECONDS  # per warm-up
```

Validation in `__post_init__` (`InvalidArgumentException`, message names
the field): `1 <= size <= 64`; `timeout` through `validate_timeout`;
`idle` is an `IdlePolicy` (never `None`) with `auto_resume=True` and
`max_idle_seconds < timeout`; `60 <= min_remaining_seconds <= timeout - 60`;
`1 <= fill_concurrency <= 8`; `sweep_interval_seconds >= 5`;
`ready_timeout > 0`; `envs`/`metadata`/`cpu_time_limit` through the existing
`_payload` validators (the payload size limit is caught at the first
warm-up, as in `create()`). `size <= 64` is a client-side sanity cap
(64 × 2 GB = 128 GB of the 1 024 GB quota); a larger fleet needs the shared
store this change does not build. `access_token` is deliberately absent:
tokens are per slot (D4). `allowed_ports` is absent: `get_host(port)` mints
per port on demand after the take, exactly as after `create()`.

```python
class SandboxPool:
    def __init__(self, config: PoolConfig, *, backend: PoolBackend | None = None,
                 region: str | None = None, session: boto3.session.Session | None = None,
                 control_plane: ControlPlane | None = None,
                 transport: TransportSettings | None = None,
                 monotonic: Callable[[], float] = time.monotonic,
                 now: Callable[[], datetime] = lambda: datetime.now(UTC),
                 sleep: Callable[[float], None] = time.sleep) -> None: ...
    def start(self) -> Self                       # idempotent; returns self
    def take(self, *, wait: float = 0.0, ready_timeout=..., request_timeout=...,
             reconnect_timeout=...) -> Sandbox
    def stats(self) -> PoolStats
    def close(self, *, drain: bool = True) -> None
    def __enter__(self) -> Self                   # start()
    def __exit__(...) -> None                     # close(drain=True)
```

`AsyncSandboxPool` has the same methods as coroutines (`start`, `take`,
`close`) plus `async with`; `stats()` stays synchronous in both. The
control plane is resolved exactly like `create()` does
(`resolve_control_plane(control_plane, session, region)`), so by default
the pool shares the process-wide plane and its buckets with every
`Sandbox.create()` in the process.

### D2. Slot life cycle and the record

```
            create()                pause(wait=True) + close()
  (empty) ──────────► warming ─────────────────────────────► ready
                        │  failure (any exception)                │
                        ▼                                         │ take()
                      failed (counted, VM terminated by create's  ▼
                      terminate_on_failure or by the pool)      taken (record deleted)
                                                                  │
  ready ──(remaining < min_remaining_seconds)──► recycled (terminate-microvm, record deleted)
  ready ──(listed TERMINATING|TERMINATED, or absent + get-microvm terminal)──► lost (record deleted)
  ready ──(listed RUNNING)──► re-parked (suspend-microvm; stays ready)
```

`SlotRecord` (frozen dataclass; everything `take()` needs without a
`get-microvm`):

| Field | Source |
|---|---|
| `sandbox_id`, `endpoint`, `template` (ARN), `template_version`, `started_at`, `maximum_duration_seconds`, `idle`, `execution_role_arn`, `ingress`, `egress` | the `SandboxInfo` returned by `run-microvm` |
| `access_token` | generated for this slot (D4); redacted in `repr`/`str` |
| `region` | the control plane |
| `state` | `"warming"` from the moment `run-microvm` returns until the park completes, then `"ready"` |
| `parked_at` | `now()` when `pause()` returned `SUSPENDED`; `None` while warming |

`expires_at = started_at + maximum_duration_seconds` and
`remaining_seconds(now)` are properties, the same arithmetic as
`SandboxInfo`. The record is written to the backend **immediately after
`run-microvm` returns** (state `warming`) so a crash between launch and park
leaves a record a persistent backend can clean up (D9), and rewritten as
`ready` after the park. Records are keyed by `sandbox_id`.

### D3. Warm-up is the ordinary `create()` plus `pause()`

A warm-up is `Sandbox.create(...)` with the pool's `PoolConfig` fields
mapped one-to-one onto `create()`'s kwargs (`template`, `template_version`,
`timeout`, `idle`, `envs`, `metadata`, `cpu_time_limit`,
`execution_role_arn`, `ingress`, `egress`, `logging`, `ready_timeout`,
`control_plane`, `transport`), `access_token=<the slot's fresh token>`,
`keep_on_failure=False`, followed by `sandbox.pause(wait=True)` (reads
`get-microvm`, `suspend-microvm` through the 2 TPS bucket, polls
`get-microvm` every 0.5 s until `SUSPENDED` within `ready_timeout`), then
`sandbox.close()` (drops the local channel and the refresher thread: the
pool keeps **data**, never live handles). Readiness of a slot is therefore
exactly `create()`'s readiness — `agent_ready and kernel_ready`, kernel
already rotated in `/run`, warm-up done — so the taker's first cell costs
one RTT.

Why not keep the handle open: N open handles mean N refresher threads and
N channels for VMs nobody is using, the JWE would have to be re-minted
after 45 min anyway, and the persistent backend needs the slot to be
reconstructible from data. Why `pause()` explicitly instead of waiting for
the idle policy: the idle timer is ≥ 60 s of paid `RUNNING` per slot and
imprecise; an explicit park is ≈ 1 s after `kernel_ready`.

The `SandboxInfo` returned by `run-microvm` is what fills the record
(`endpoint`, `started_at`, `maximum_duration_seconds`, `idle` with its
three fields resolved). `Sandbox.create()` already exposes it as
`sandbox.get_info()` without a network call (`_info`); the warm-up reads
`sandbox._info` through a small package-private accessor
(`Sandbox.launch_info` property, added by this change, sync and async)
rather than calling `get-microvm`.

### D4. `take()`: oldest ready slot, explicit resume, token hand-off, no `get-microvm`

1. Under the pool lock, `pick_ready_slot(records, now)` returns the `ready`
   record with the **earliest `expires_at`** whose
   `remaining_seconds(now) >= min_remaining_seconds` (oldest first so slots
   do not rot; a stale one is left for the sweeper). The record is deleted
   from the backend **before** any network call: from this instant the
   secret exists only in the `Sandbox` handed to the taker (and in the
   taker's process memory). `hits` is counted only once step 3 succeeds.
2. `control_plane.resume_microvm(sandbox_id)` (5 TPS bucket). Explicit
   resume is chosen over letting the readiness poll auto-resume because it
   measured 0.38 s versus 0.67 s p50 (benchmark §4) and it makes a dead slot
   fail fast: `False` (AWS conflict: not `SUSPENDED`) or a
   `SandboxNotFoundException`/`SandboxStateException` means the slot is
   gone — the pool logs `slot lost at take` with the id, counts `lost`,
   calls `terminate_quietly` and goes to step 5.
3. `Sandbox._open(info=sandbox_info_from_record(record), access_token=
   record.access_token, control_plane=..., transport=..., proxy_ports=
   (PortSpec.single(8080),), ready_timeout=..., request_timeout=...,
   reconnect_timeout=..., terminate_on_failure=True, readiness=TakePoll)`.
   No `get-microvm` on this path (eventually consistent and ≈ 0.1 s; the
   record has everything). `TakePoll(ReadinessPoll)` has
   `INITIAL_DELAY = 0.1`, `MAX_DELAY = 0.5`, `JITTER = 0.0` and is passed
   through a new keyword-only parameter `readiness: type[ReadinessPoll] =
   ReadinessPoll` of `_open`/`_wait_until_ready` (sync and async). The
   schedule of `create()`, `connect()` and `resume()` is unchanged (they keep
   the default). A JWE is minted here as in `connect()`; the pool never
   stores JWEs (they expire in 60 min, would be another secret at rest, and
   minting costs ≈ 0.1 s at 50 TPS).
4. The `Sandbox` is returned with `access_token == record.access_token`,
   `sandbox_id == record.sandbox_id`, `get_info()` from the record (the
   state field says `"RUNNING"` once `Health` answered) and `metadata` read
   from `Health`. If `_open` fails (`SandboxNotReadyException` or any
   other): `terminate_on_failure` has already terminated the VM; the pool
   counts `failed`, logs the exception class and the id, and goes to step 5.
5. **Fallback**: if no ready slot exists at entry, `take(wait=w)` waits up
   to `w` seconds (default 0: no wait) for a slot to become `ready`
   (condition variable notified by the filler); if still none, or if steps
   2–4 lost the slot, it runs a plain `Sandbox.create()` with the pool's
   launch kwargs and a fresh token (the same call a warm-up makes, without
   the `pause()`), counts `misses += 1` and returns it. The pool is a
   latency optimisation, never a semaphore: a caller always gets a sandbox
   or the exception `create()` would have raised. `take()` on a closed pool
   raises `PoolClosedException`.

A `take()` never blocks a refill: the filler notices the drop in
`ready + warming` on its next wake-up (it is signalled by `take`).

### D5. Filler: one background thread, bounded concurrency, shared buckets, backoff

`start()` spawns one daemon thread (`rayito-pool-filler-<n>`) that loops
until closed: compute `deficit = size - (ready + warming)`; submit up to
`deficit` warm-ups to a `ThreadPoolExecutor(max_workers=fill_concurrency)`
(a warm-up holds an open handle for ≈ 6–8 s; concurrency bounds memory and
threads, not TPS — TPS is the buckets' job); wait on a `threading.Condition`
until a warm-up finishes, a `take()` happens, the sweep interval elapses or
`close()` is called. Every AWS call of a warm-up goes through the pool's
`ControlPlane`, i.e. by default the shared per-process
`LambdaMicrovmsControlPlane` with its `RunMicrovm` 5 TPS, `SuspendMicrovm`
2 TPS, `ResumeMicrovm` 5 TPS, `TerminateMicrovm` 10 TPS and
`CreateMicrovmAuthToken` 50 TPS buckets, so a size-20 pool fills in ≥ 10 s
(suspend bucket) and never starves the application's own `create()` (it
queues behind the same buckets, first come first served).

`FillBackoff` (pure): after any warm-up failure (`QuotaExceededException`,
`CapacityException`, `RateLimitException`, `SandboxNotReadyException`,
`AuthenticationException`, anything) the filler sleeps `delay` before the
next submission, `delay` starting at 1 s and doubling to 60 s with ±25 %
jitter; a success resets it to 1 s. Failures increment `failed` and log
one warning with the exception class, the sandbox id when known and the
delay — never the token, never the payload. `size` warm-up failures in a
row do not stop the pool (the app may still be launching sandboxes fine;
the quota may free up); `stats().failed` and the warnings are the signal.

Async: the same loop as an `asyncio.Task` with an `asyncio.Semaphore(
fill_concurrency)` and `asyncio.Condition`; warm-ups are
`AsyncSandbox.create()` + `await pause(wait=True)` + `await close()`.

### D6. Sweeper: recycle before the wall, reconcile with `list-microvms`

Every `sweep_interval_seconds` (and once at `start()`, D9) the filler
thread runs the sweep:

1. **Recycle**: each `ready` record with `remaining_seconds(now) <
   min_remaining_seconds` (`slot_is_stale`) is deleted from the backend and
   terminated (`terminate_microvm`, 10 TPS; a `False`/not-found is fine);
   `recycled += 1`. The deficit created is refilled by the same loop
   iteration. With the defaults (`timeout` 28 800, `min_remaining_seconds`
   3 600) an untouched slot is recycled ≈ 7 h after its launch, so every
   slot handed out has ≥ 1 h of life and no slot ever reaches the 8 h wall
   while parked.
2. **Reconcile**: one paginated `list_microvms(image_arn=<template arn>,
   image_version=<config.template_version or None>, states=None)` (the
   SDK's default filter omits `TERMINATING|TERMINATED`); `reconcile_action(
   record, listed_state)` decides per `ready` record: listed `SUSPENDED` or
   `SUSPENDING` → nothing; listed `RUNNING` → `suspend_microvm` (2 TPS) and
   a warning `slot resumed out of band, re-parked` (someone connected to it
   with its id and token, or probed it; the pool keeps it because its state
   is unknown but its kernel is warm); listed `PENDING` → nothing; **absent
   from the listing** → `get_microvm`: terminal or not found → record
   deleted, `lost += 1`, warning with the id and `stateReason`; any other
   state → nothing (the listing lagged). `warming` records are the filler's
   business and are skipped. A `list-microvms` failure aborts this sweep
   with one warning; the next sweep retries.
3. **Idle policy as the safety net**: the slot's `idle` is the config's
   `IdlePolicy` with `suspended_duration_seconds` resolved by `create()` to
   `timeout − max_idle_seconds` (the existing rule), so a slot the pool
   forgets (process killed with the in-memory backend) terminates itself
   at its `timeout` at the latest: the maximum leak is `size` slots of
   storage for ≤ 8 h, i.e. ≈ $0.0008 per slot. With `auto_resume=True` a
   forgotten slot that some client wakes behaves like any other sandbox.

### D7. Immutable per-pool launch configuration

Everything `run-microvm` fixes (`runHookPayload` contents — token hash,
`envs`, `metadata`, `cpu_seconds` —, `idlePolicy`,
`maximumDurationInSeconds`, connectors, role, logging, image and version)
is a `PoolConfig` field and identical for every slot of a pool; only the
access token differs (D4). A `SandboxPool` is one template + version; an
application that needs two configurations runs two pools. `PoolConfig` is
frozen and a pool never mutates it; there is no `resize()` (open a new
pool, drain the old).

### D8. `Sandbox.create(pool=)` is sugar for `pool.take()`

`Sandbox.create(pool: SandboxPool | None = None, ...)` (and
`AsyncSandbox.create(pool: AsyncSandboxPool | None = None, ...)`): when
`pool` is given, `create()` returns `pool.take(ready_timeout=...,
request_timeout=..., reconnect_timeout=...)` and **rejects with
`InvalidArgumentException`, naming the first offending kwarg, any launch or
plane argument that differs from its default**: `template`,
`template_version`, `timeout`, `idle`, `envs`, `metadata`,
`cpu_time_limit`, `execution_role_arn`, `allowed_ports`, `ingress`,
`egress`, `logging`, `region`, `session`, `access_token`,
`keep_on_failure`, `control_plane`, `transport` (the pool's own are used).
Only `ready_timeout`, `request_timeout` and `reconnect_timeout` pass
through. `pool` accepts a `SandboxPool`, not a `PoolConfig`: a pool owns a
thread and N parked VMs with a lifetime and a bill, and must be started and
closed explicitly; hiding one behind a config object inside `create()`
would create untracked pools. `create()` does not start the pool: a
`take()` on a pool that was never started raises `PoolClosedException`
("pool not started: call start() or use it as a context manager").

### D9. `PoolBackend` and the two backends

```python
class PoolBackend(Protocol):
    @property
    def persistent(self) -> bool: ...
    def load(self) -> tuple[SlotRecord, ...]: ...
    def save(self, record: SlotRecord) -> None: ...    # upsert by sandbox_id
    def delete(self, sandbox_id: str) -> None: ...     # missing id is not an error
```

The pool serialises every backend call under its own lock; a backend is
not required to be thread-safe and is **not** multi-process safe.

- `InMemoryPoolBackend` (default): a dict; `persistent = False`.
- `JsonFilePoolBackend(path)`: `persistent = True`; the file is
  `{"schema": "rayito.pool/1", "slots": [<record dicts>]}` with
  `access_token` **in clear** (it is the slot's secret; there is nothing
  else to store), `started_at`/`parked_at` as ISO-8601 UTC, `idle` as its
  three fields. Writes are atomic (`<path>.tmp` then `os.replace`) and the
  file is created with mode `0o600` via `os.open(..., O_CREAT | O_WRONLY |
  O_TRUNC, 0o600)` (no effect on Windows; documented). `load()` on a
  missing file returns `()`; a file with another `schema` raises
  `InvalidArgumentException`. Intended uses: unit tests of persistence and
  recovering a pool in the same host after a restart. The docs say in so
  many words that it is not a shared store and that the file is as
  sensitive as `RAYITO_ACCESS_TOKEN`.

**Recovery at `start()`** (any backend, meaningful only for a persistent
one): `load()`; every `warming` record is an orphan of a process that died
mid-warm-up → `terminate_microvm` + `delete` (`lost += 1`); every `ready`
record goes through the sweep (D6) before it can be taken, so a slot that
died while nobody was looking is dropped and a stale one is recycled. The
records loaded count as `ready` immediately after the sweep. The async pool
calls the JSON backend through `asyncio.to_thread`.

### D10. `stats()`

```python
@dataclass(frozen=True)
class PoolSlotInfo:
    sandbox_id: str; state: Literal["warming", "ready"]; started_at: datetime
    expires_at: datetime; parked_at: datetime | None

@dataclass(frozen=True)
class PoolStats:
    size: int; ready: int; warming: int
    takes: int; hits: int; misses: int          # takes == hits + misses
    launched: int; recycled: int; lost: int; failed: int
    slots: tuple[PoolSlotInfo, ...]             # never the token
```

Counters are cumulative since `start()`; `hits` counts takes served from a
slot whose `_open` succeeded, `misses` counts fallbacks (empty pool or slot
lost at take), `launched` counts every `run-microvm` accepted (warm-ups and
fallbacks), `recycled` sweep recycles, `lost` slots dropped by
reconciliation or at take or at recovery, `failed` warm-ups and takes that
raised. `stats()` is synchronous, lock-protected and never touches the
network.

### D11. `close(drain=True)`, context managers, forgetting to close

`close()` sets the stop flag, wakes the filler, and joins it (≤ 30 s: a
warm-up in flight completes its `create()` — bounded by `ready_timeout` —
and, seeing the stop flag, terminates its VM instead of parking it). With
`drain=True` (default) every `ready` record is terminated
(`terminate_microvm`, 10 TPS bucket) and deleted, then the executor is shut
down. `drain=False` leaves `ready` records and their VMs parked for another
pool instance to recover (D9) and is **rejected with
`InvalidArgumentException` when `backend.persistent` is `False`** (the
slots would be unrecoverable). `close()` is idempotent; `take()` after it
raises `PoolClosedException`. `__enter__`/`__aenter__` call `start()`,
`__exit__`/`__aexit__` call `close(drain=True)`. There is no `__del__`
finaliser (non-deterministic, would call AWS from the GC); the docs and the
docstring say that a pool that is never closed leaves `size` suspended VMs
that self-terminate at their `timeout` (D6.3).

### D12. TypeScript parity

`clients/typescript/src/pool/config.ts` (`PoolConfig` interface +
`validatePoolConfig`, same fields with `timeoutMs`, `minRemainingMs`,
`fillConcurrency`, `sweepIntervalMs`, `readyTimeoutMs`), `pool/backend.ts`
(`PoolBackend`, `InMemoryPoolBackend`, `JsonFilePoolBackend` on
`node:fs/promises` with `writeFile(tmp, data, { mode: 0o600 })` +
`rename`), `pool/pool.ts` (`SandboxPool` with `start()`, `take({ waitMs,
readyTimeoutMs, requestTimeoutMs, reconnectTimeoutMs })`, `stats()`,
`close({ drain })`, `Symbol.asyncDispose`), `Sandbox.create({ pool })`
with the same rejection of launch options, `PoolClosedError`
(`SandboxError` subclass), exports in `index.ts`. The filler is a promise
loop driven by an `AbortController`; the sweep timer and the backoff timer
are `unref()`ed so an idle pool never keeps the Node process alive by
itself, while a warm-up in flight (a pending fetch) does. The take path
reuses the TS equivalent of `_open` with a `TakePoll` schedule (100 ms →
500 ms). Same pure core split (`pool/core.ts`: selection, staleness,
reconciliation, backoff, stats, record ↔ JSON) tested without I/O.

### D13. Logging and secrets

The pool logs, at `INFO`, slot transitions with `sandbox_id`, the new
state and the duration (`warm-up ms`, `park ms`, `take ms`); at `WARNING`,
failures with the exception class, recycles, losses and out-of-band
resumes. Never the access token, the JWE, `envs`, `metadata` values, the
payload or the backend path's contents. `SlotRecord.__repr__` prints
`access_token='<redacted>'`; `PoolSlotInfo` and `PoolStats` carry no token;
a unit test greps `caplog` for every slot token and the JWE and finds
none. The TypeScript pool uses the SDK's `Logger` the same way.

### D14. Security: custody of the slot secrets (the ADR-008 open point, closed)

Facts: the sha256 of a slot's access token is fixed in its `runHookPayload`
at `/run`, `/run` is accepted once per boot and a forged one is ignored
(`already_ran`, T2), so **the token that opens a slot is the token the pool
minted, for the whole life of that VM**. There is no rotation at `take()`
and none can be added without a second `/run`, which `rayd` rejects by
design. Consequences and mitigations, written into `SECURITY.md` as row
**T14 "custodia de secretos del pool"** and into `docs/site/docs/pool.md`:

- One fresh `secrets.token_bytes(32)` per slot (never one per pool): a
  leaked slot secret opens one VM, not the fleet.
- The secret lives in the pool process (in-memory backend) or in a `0600`
  JSON file (file backend) only until `take()`, which deletes the record
  before touching the network; after that only the taker's `Sandbox` holds
  it. `close(drain=False)` is the one path that leaves secrets at rest on
  purpose, and it is only allowed on a persistent backend.
- The pool operator can read every parked secret. This is the **same trust
  level** the SDK operator already has (`SECURITY.md` "Modelo": total trust,
  holds the IAM credentials that mint JWEs and terminate VMs). What changes
  is that a parked VM has a secret that exists *before* its user does; an
  application that hands taken sandboxes to different tenants must treat
  the pool process as a trusted component of its own, exactly as it treats
  the process that calls `create()` today.
- A slot's JWE is minted at take, never stored; a parked slot is reachable
  only by whoever can mint a JWE for it (IAM) **and** knows its secret (the
  two-level auth of ADR-004 is unchanged). `Health` remains readable with a
  JWE alone, as for any sandbox.
- Residual: a process dump or the JSON file exposes the parked secrets;
  `list()` reveals the parked ids to any IAM principal with `ListMicrovms`;
  a principal with `ResumeMicrovm` can resume a parked slot and start its
  meter (the sweeper re-parks it within a sweep interval and logs it).

`ARCHITECTURE.md` ADR-008 consequence (2) is amended with one sentence
pointing here ("cerrado en `m7-suspended-pool` D4/D14: un secreto por
plaza acuñado por el pool, borrado del backend al tomar, sin rotación;
SECURITY.md T14"). No new ADR: the two-level auth and its channel are
unchanged.

### D15. Cost model and the docs

`docs/site/docs/pool.md` (Spanish, mkdocs nav entry "Pool de sandboxes"
after "Modelo de costes") contains: why a pool of suspended VMs and not of
running ones (ADR-008 numbers), when it pays (bursty agents needing
sub-second sandboxes; not batch jobs), the API (`PoolConfig`,
`SandboxPool`, `take()`, `Sandbox.create(pool=)`, `stats()`, `close()`,
async, TypeScript), the semantics (fallback, refill, recycle, reconcile,
`list()` visibility, quota accounting), the backends and their limits, the
custody section (D14) and this cost table, every number traceable to
`AWS_API_NOTES.md` §12 and the benchmark:

| Concepto por plaza (`rayito-base`, 0,92 GB de snapshot) | Coste |
|---|---|
| Storage mientras está aparcada | 0,92 GB × $0,08/GB-mes ≈ **$0,074/mes** ($0,0001/h) |
| Aparcar (snapshot write al `pause()`) | 0,92 × $0,0038 ≈ **$0,0035** |
| Tomar (snapshot read al `resume`) | 0,92 × $0,00155 ≈ **$0,0014** |
| Calentar (lanzamiento: read + ≈ 7 s `RUNNING`) | $0,0014 + $0,00025 ≈ **$0,0017** |
| Reciclado de una plaza ociosa (cada ≈ 7 h con los defaults: calentar + aparcar) | ≈ **$0,005** ⇒ ≈ 3,4/día ⇒ ≈ **$0,52/mes** |
| **Total plaza ociosa** | ≈ **$0,6/mes** (frente a $91/mes una VM `RUNNING`) |
| Plaza tomada | la toma ($0,0014) + el sandbox normal ($0,126/h mientras corre) |

plus the sizing rule (`size` ≈ the peak number of sandboxes needed inside
one refill window, ≈ 8 s per slot at `fill_concurrency` 4 — a burst larger
than `size` falls back to `create()` at the usual 5–6 s) and the
regional-quota note (2 GB per slot against 1 024 GB). `cost.md` gains one
row ("Plaza del pool ociosa ≈ $0,6/mes; toma ≈ $0,0014 + 0,4–0,7 s") linking
`pool.md`. `README.md` gains a six-line snippet under the existing Python
example. `SPEC.md` §4's pool bullet says the suspended pool ships in
`m7-suspended-pool`, pre-warmed stays out. `ARCHITECTURE.md` Capa 3 gains
a "Pool de suspendidos (SDK)" paragraph: what runs where (a thread in the
client process; nothing in AWS but the parked VMs), the buckets it shares,
the record, the take path; ADR-008 amended per D14. `MILESTONES.md` M7 row
3 gets the measured p50/p95 and the run cost at acceptance.
`AWS_API_NOTES.md` §16 gains row **Q53**: "`take()` de una plaza suspendida
→ primera celda, p50/p95 sobre 20 tomas, frente a `create()`" with the
e2e's numbers. Changelogs (`clients/python`, `clients/typescript`) get an
`Unreleased / Added` entry.

### D16. Tests

**Fake control plane** (`clients/python/tests/unit/fake_control_plane.py`,
an in-memory `ControlPlane` implementation, distinct from the Stubber
fixture which cannot serve a variable number of calls): a state machine per
`sandbox_id` (`run_microvm` → `PENDING` then `RUNNING` after `k` `get`s or
immediately; `suspend` → `SUSPENDED`; `resume` → `RUNNING`; `terminate` →
`TERMINATED`; `list` reflects the map; `create_auth_token` → a fake JWE),
a call log with the fake clock's timestamps per operation (so tests assert
ordering and that `suspend` calls went through the plane's buckets —
the plane wraps its calls in the real `TokenBucket`s with the fake clock),
programmable failures per operation and per call index (`QuotaExceeded`,
`RateLimit`, conflict, not-found), and an `endpoint` per launched VM that
points at a fake `rayd` from `fake_rayd_factory` (one fake `rayd` per slot
so `Health.sandbox_id` matches). A `TrackingTransport.for_loopback()` maps
the endpoint to `host:port`.

**Unit, Python** (`test_pool_base.py`, `test_pool_backends.py`,
`test_pool_sync.py`, `test_pool_async.py`; the async file mirrors the sync
one case by case): config validation (every rule of D1 with its message);
warm-up sequence and record contents (`run` → `Health` until
`kernel_ready` → `suspend` → `get` until `SUSPENDED` → record `ready`;
tokens differ per slot; the `runHookPayload` of each launch carries that
slot's `token_sha256`); fill respects `fill_concurrency` (never more than N
handles open, asserted through the fake `rayd`'s open channels) and the
buckets (with a fake clock, 5 warm-ups issue `run` no faster than 5/s and
`suspend` no faster than 2/s); `take()` pops the oldest, issues `resume`,
no `get`, opens with the slot's token (the fake `rayd` sees the right
`x-access-token` on `Metrics`), deletes the record, `run_code("1+1")`
returns `"2"`; `take()` on an empty pool falls back to `create()` (`misses`
1, `launched` +1); `take(wait=5)` returns a slot that becomes ready during
the wait; slot lost at take (fake `resume` → conflict) → terminate +
fallback + `lost`; `_open` failure at take → `failed` + fallback; refill
after take (`ready` back to `size`); recycle with a fake clock (advance
past `timeout − min_remaining_seconds` → terminate + refill, `recycled`);
reconcile: terminated out of band → dropped + refilled (`lost`), resumed out
of band → `suspend` again; absent from the listing + `get` terminal →
dropped; failure backoff (fake `run` raising twice → delays 1 s, 2 s with
the fake sleep; success resets); `close(drain=True)` terminates every ready
slot and joins; `close(drain=False)` rejected on the in-memory backend and
accepted on the JSON one (records remain); recovery (`warming` orphan
terminated, `ready` record re-verified then taken); `Sandbox.create(pool=)`
sugar, each rejected kwarg named, pass-through knobs honoured, unstarted
pool → `PoolClosedException`; no token in `caplog` (D13); `stats()`
arithmetic; JSON backend round-trip, atomic write (`.tmp` gone), `0o600`
on POSIX (`skipif` on Windows), schema rejection, missing file → `()`.
`rayito.e2b`: `Sandbox.create(pool=...)` raises `TypeError` (unknown kwarg)
and no name containing `Pool` is in `rayito.e2b.__all__`.

**Unit, TypeScript** (`tests/unit/pool.test.ts` over the existing fake
control plane and fake `rayd` in `tests/unit/fake/`): the same list minus
the thread-specific cases, plus "timers are unref'd" (a started idle pool
does not keep a `vitest` worker alive: asserted with a fake timer API).

**e2e, Python** (`tests/e2e/test_m7_pool.py`, marker `e2e`, guarded by
`RAYITO_E2E=1` + `RAYITO_TEMPLATE`, the conftest's sweeper and pre-flight
apply; every VM `timeout <= 1800`):

- `test_pool_take_latency` — rule fixed here, before measuring: pool
  `size=3`, `timeout=900`, `min_remaining_seconds=300`,
  `sweep_interval_seconds=10`, `fill_concurrency=3`, the e2e settings'
  role/logging; `start()`; then **20 times**: poll `stats().ready >= 1`
  (100 ms, ≤ 90 s), `t0 = perf_counter()`, `sbx = pool.take()`,
  `execution = sbx.run_code("1+1", timeout=60)`, `t = perf_counter() − t0`,
  assert `execution.text == "2"`, record `t`, `sbx.kill()`. Then **20
  times** sequentially: `t0`, `Sandbox.create(<same launch kwargs, timeout
  900>)`, `run_code("1+1")`, `t`, `kill()`. `T_take = p95(take samples)`
  (nearest-rank), `T_create = p95(create samples)`; print both with their
  p50/min/max and `pool.stats()`; **assert `T_take < 1.5`**, `hits == 20`,
  `misses == 0`; `close(drain=True)`; then `Sandbox.list(template=...)`
  shows none of the 43 ids as `PENDING|RUNNING|SUSPENDING|SUSPENDED`.
- `test_pool_recycles_before_the_wall` — pool `size=1`, `timeout=720`,
  `min_remaining_seconds=600`, `sweep_interval_seconds=10`; `start()`; wait
  for `ready == 1` and note `first = stats().slots[0].sandbox_id`; the slot
  has ≈ 710 s left at park and crosses the 600 s line ≈ 110 s later; poll
  `stats()` (1 s, ≤ 240 s) until `recycled == 1`; assert a **different**
  `ready` id exists within another 60 s, `get_info(first).state in
  TERMINATING|TERMINATED`; `close()`.
- `test_pool_json_backend_recovery` — pool `size=1` on a
  `JsonFilePoolBackend(tmp_path / "pool.json")`; wait `ready == 1`;
  `close(drain=False)`; a **new** pool on the same file: `start()`, the
  loaded slot survives the sweep (`ready == 1`, same id, `launched == 0`),
  `take()` → `run_code("1+1") == "2"`, `kill()`; `close()`.

Cost: ≈ 23 + 20 + 2 + 1 launches of seconds each ≈ $0.25 (pessimistic
$0.5); wall ≈ 7 min.

**e2e, TypeScript** (`tests/e2e/pool.e2e.test.ts`, same guard): `size=2`,
5 takes with the same protocol, every take → first cell recorded and
printed, `hits === 5`, kernel answers `1+1`, drain leaves no live VM. The
TypeScript e2e proves the path; the **acceptance number is the Python
e2e's** (D17).

### D17. Acceptance rule (fixed before running)

The change is accepted when, in one run of `make test-e2e` against the
current `rayito-base` from the usual client (RTT ≈ 85–95 ms):
`T_take < 1.5 s` (p95 of 20 `take()` → `run_code("1+1")` samples, each
issued with a ready slot available), `hits == 20`, `misses == 0`, the
recycle test saw `recycled == 1` with a new ready slot and the first id
terminal, the recovery test took the recovered slot, `list-microvms` shows
no live VM of the template afterwards, and every unit/lint gate of both
SDKs is green. `T_create` is reported next to `T_take` (expected ≈ 5–6 s)
and both go into `MILESTONES.md`, `AWS_API_NOTES.md` Q53 and `pool.md`. If
`T_take >= 1.5 s` the change is **not** accepted: the implementer records
the per-phase timings (`resume-microvm` API, mint, first `Health`, cell)
in the test output and amends this design (e.g. `TakePoll` schedule) —
never the rule.

## Risks / Trade-offs

- **Parked slots are a standing cost and a standing quota use** (2 GB each
  against the regional memory quota, `SUSPENDED` in `list()`). Mitigation:
  `size <= 64`, `stats()`, `close(drain=True)` by default, the idle policy
  as the self-termination net, the cost table.
- **The 8 h wall applies to the taker too**: a slot recycled at 7 h gives
  the taker ≥ `min_remaining_seconds` (1 h by default) — less than a plain
  `create(timeout=3600)`'s guaranteed hour only when the taker expected
  more. Mitigation: `min_remaining_seconds` is configurable; `get_info().
  remaining_seconds()` tells the truth; `pool.md` says a pooled sandbox is
  for short, bursty work.
- **`SuspendMicrovm` at 2 TPS is the fill bottleneck**: a size-20 pool
  needs ≥ 10 s of suspends; a burst of takes larger than the refill rate
  falls back to `create()` — by design (D4.5), never a queue.
- **Out-of-band resumes** (another client, a `Sandbox.list(metadata=)` only
  probes `RUNNING`, so not that) start a slot's meter until the next sweep
  (≤ 30 s by default): bounded and logged.
- **Secret custody** (D14): accepted and documented as T14; the
  alternative (no pool) is the status quo, and a pool of *running* VMs
  would have the same custody problem at 130× the cost.
- **A crashed pool with the in-memory backend leaks up to `size` parked
  slots for ≤ 8 h** (≈ $0.0008 each): documented; the JSON backend recovers
  them on the same host; the shared store is future work.
- **`connect()` path without `get-microvm`**: a slot terminated between
  the last sweep and the take fails at `resume-microvm` (fast, handled) or
  at the first `Health` (bounded by `ready_timeout`, then fallback). The
  latter costs the taker up to `ready_timeout` in the worst case;
  mitigation: the take `ready_timeout` default stays 90 s but the sweep
  runs every 30 s, so the window is small, and `stats().failed` shows it.
- **TypeScript timers**: an `unref()`ed sweep timer means a process that
  only holds a pool exits without draining; documented (`await
  pool.close()`), the idle policy nets the leak.
- **Measurement variance**: `T_take` depends on the client's RTT and the
  region; the rule is fixed with the same client and region as the M6
  benchmark and the numbers are reported with their p50 and RTT.

## Migration Plan

Additive: no existing public name changes, no default changes to
`create()`/`connect()`, no wire change, no image change. The only edits to
existing code are the `pool=` kwarg on `create()`, the private `readiness`
parameter on the open path, the `launch_info` accessor and the exports.
Rollback is deleting the new modules and the kwarg. Users who never build a
`SandboxPool` see no behavioural difference. The docs site build stays
strict.

## Open Questions

None. Every decision above is closed; measured values that this change
produces (`T_take`, `T_create`, the e2e cost) are recorded at acceptance
without reopening the design.

## Amendments (implementation, 2026-09-16)

Recorded while implementing; the acceptance rule D17 is unchanged.

- **A1. Numbering.** `m7-s3-persistence` and `m7-cli` landed their §16 rows
  first (Q53–Q55), so the pool's measurement row is **Q56**, not Q53;
  `SECURITY.md` T14 was still free and is used as written.
- **A2. Settle cell before parking (D3 amended).** The e2e found that
  `create()` can report `kernel_ready` at 2–3 s (instead of ≈ 6 s): `rayd`
  answers `/run` with 200 (which opens traffic) and queues `Control::Rotate`
  for the supervisor loop to mark `Rotating` afterwards
  (`crates/rayd/src/hooks/mod.rs` `run`, `crates/rayd/src/code/supervisor.rs`
  `request_rotation`/`begin_rotation`), so a `Health` that lands in that gap
  sees `kernel_ready` of the un-rotated kernel. A slot parked in that state
  lost its kernel at resume (`kernel_state_lost`; 3 of 40 warm-ups in two
  runs, takes of 6.2 / 12.4 / 11.7 s, run p95 6.2 s and 0.80 s). The warm-up
  now runs one trivial cell (`pass`) after `create()` — `rayd` holds
  `Execute` while the default context restarts, so the cell only returns on
  the rotated kernel — waits 0.3 s and re-checks `Health.kernel_ready`
  (`_wait_until_ready` + cell again if a rotation started late; three
  attempts). A slot therefore arrives with `execution_count == 1`. In the
  acceptance run 0 of 23 warm-ups lost the kernel. The root fix (mark
  `Rotating` synchronously in the `/run` handler, or expose "rotated" in
  `Health`) belongs to `rayd` and is recorded in Q56 for its owner.
- **A3. Nearest-rank percentile.** `T_take`/`T_create` use
  `sorted[ceil(0.95·n) − 1]` (rank 19 of 20), the definition of
  `scripts/bench_cold_start.py`; the first draft of the e2e used a rounding
  that reported the maximum.
- **A4. `sleep` parameter (D1).** `SandboxPool(sleep=None)` by default: the
  filler's backoff waits on the stop event so `close()` interrupts it; an
  injected `sleep` (tests) is called with the delay instead. Same for
  `AsyncSandboxPool(sleep=None)`.
- **A5. Bucket pacing assertion (D16).** A `TokenBucket` allows a burst of
  `rate` calls at once, so the unit test asserts the bucket contract in its
  window form (≤ 2·rate calls in any 1 s window and the n-th suspend at least
  `(n − 2)/2` s after the first) rather than "never closer than 0.2 / 0.5 s".
- **A6. Record saved right after `run-microvm` (D2).** Implemented without
  decomposing `create()`: the warm-up passes `create()` a `LaunchObserver`
  (a `ControlPlane` decorator that delegates everything and reports the
  `SandboxInfo` of each accepted `run-microvm`), so the `warming` record is
  written before `create()` continues to readiness. `Sandbox.launch_info`
  exists as specified (the info the handle was opened with, never refreshed).
- **A7. TypeScript prerequisites (D12).** The TypeScript `create()` had no
  `metadata`/`cpuTimeLimit`; both were added to `buildRunHookPayload`,
  `buildLaunchPlan` and `SandboxCreateOptions` (same JSON as Python) so
  `PoolConfig` maps one-to-one and `create({ pool })` can reject them. An
  endpoint carrying an explicit port (`host:port`, `[v6]:port`) now overrides
  `transport.port` in `baseUrl`, which is what lets the unit tests run one
  fake `rayd` per slot (the Python tests do the same by overriding
  `TransportSettings.target`). The pool exposes `requestSweep()` and
  `SandboxPool.timersOf()` for the tests, as `Sandbox.coreOf()` does.
- **A8. Take-path instrumentation (D13).** The `INFO` line of a take carries
  the phase split (`resume-microvm` ms, open ms, parked age) and
  `Sandbox._probe_health` logs at `DEBUG` the status of a not-yet-reachable
  `Health`; both were what located A2.
- **A9. CloudWatch runtime logs.** The e2e VMs' runtime logs never appeared in
  `/rayito/rayito-base` within 30 min despite `logging=cloudwatch` with the
  execution role; the diagnosis of A2 came from the SDK logs and `rayd`'s
  source. Noted in Q56 for `m7-cli` (`rayito sandbox logs`).

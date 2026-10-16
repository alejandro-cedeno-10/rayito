# Sandbox pool

A `SandboxPool` keeps N **suspended** MicroVMs that are already warm
(`agent_ready`, kernel rotated, warm-up done) so that `take()` hands over a
usable sandbox in under a second instead of the 5-6 s of a `create()`. It
is measured against real AWS (details at the end, in "Sources and
measurements").

## Why suspended and not `RUNNING` { #por-que-suspendidos-y-no-running }

The cold-start benchmark measured a `create()` → `kernel_ready` of **p50
5.2 s / p95 6.1 s** (p95 8.6 s in a burst of 20) and an explicit `resume()`
→ `Health` of **p50 0.38 s / p95 0.40 s**. A `SUSPENDED` VM only pays its
snapshot's storage (0.92 GB × $0.08/GB-month ≈ **$0.074/month**); a
`RUNNING` one pays $0.126/h ≈ **$91/month**. Since resuming costs less
than 2 s, the rule fixed before measuring chose the suspended pool.

## Measured (`test_pool_take_latency`, 20 takes with a ready slot versus 20 `create()`) { #medido-test_pool_take_latency-20-tomas-con-plaza-lista-frente-a-20-create }

| | p50 | p95 | min | max |
|---|---|---|---|---|
| `T_take`: `take()` → `run_code("1+1")` | **0.770 s** | **0.897 s** | 0.739 s | 0.922 s |
| `T_create`: `create()` → `run_code("1+1")` | 6.151 s | 6.488 s | 3.029 s | 6.500 s |

A take is ≈ 0.14 s of `resume-microvm`, ≈ 0.5 s of minting the JWE,
opening the channel and seeing `Health` with the `TakePoll` poll (0.1 →
0.5 s), and ≈ 0.1 s of the cell. All 20 `take()` calls were hits (`hits ==
20`, `misses == 0`); run of 2026-09-16 (`rayito-base` 17.0, measured RTT
109 ms; the file's three tests in 336 s, ≈ $0.25). Nearest-rank percentile
as in the cold-start benchmark.

## When it pays off { #cuando-compensa }

- Bursty agents that need a sandbox **right now** (a tool per conversation
  turn, an evaluator that opens and closes sandboxes): the pool turns 5-6 s
  into under 1 s.
- **Not** for long batch jobs: a taken slot is a normal sandbox that bills
  $0.126/h while it runs, and the 6 s `create()` is negligible against
  minutes of work.
- The launch configuration is **per pool** (`envs`, `metadata`,
  `cpu_time_limit`, idle policy, connectors, role, `timeout`, egress): two
  configurations are two pools.
- **Agent in the sandbox**: a pool is not needed. The normal startup is
  `Sandbox.create(...)` + `sbx.agent.run(...)`, and the turns of a single
  conversation do better by pausing their VM; a pool with
  [`warmup`](#calentamiento-warmup) only pays off with many **new**
  conversations that need the first message to be fast
  ([Which one do I use?](guias/agente-en-el-sandbox.md#que-uso)).

## API { #api }

=== "Python"

    ```python
    from rayito import PoolConfig, SandboxPool

    config = PoolConfig(size=3, template="rayito-base")
    with SandboxPool(config) as pool:      # start() on enter, close(drain=True) on exit
        sbx = pool.take()                  # < 1 s with a ready slot; create() if there is none
        print(sbx.run_code("1+1").text)    # "2"
        sbx.kill()
        print(pool.stats())                # ready, warming, takes, hits, misses, ...
    ```

    `Sandbox.create(pool=pool)` is sugar for `pool.take()`: it accepts
    `ready_timeout`, `request_timeout`, `reconnect_timeout`, `secrets`,
    `secret_cache`, `transfer` and `logger`, and rejects with
    `InvalidArgumentException` any other launch or control-plane kwarg
    (`template`, `timeout`, `envs`, `region`, `control_plane`, `gateways`,
    `persist`, `index`...). To open a gateway on the taken slot (an agent,
    for example) use `pool.take(gateways=...)`. `pool` takes an
    already-started `SandboxPool`, never a `PoolConfig`: a pool has a
    thread, N VMs and a bill, and is started and closed explicitly.

=== "Python (async)"

    ```python
    import asyncio

    from rayito import AsyncSandboxPool, PoolConfig


    async def main() -> None:
        async with AsyncSandboxPool(PoolConfig(size=3, template="rayito-base")) as pool:
            sbx = await pool.take()
            print((await sbx.run_code("1+1")).text)
            await sbx.kill()


    asyncio.run(main())
    ```

    `await AsyncSandbox.create(pool=pool)` with the same rules.

=== "TypeScript"

    ```ts
    import { SandboxPool } from "rayito";

    await using pool = await new SandboxPool({ size: 3, template: "rayito-base" }).start();
    const sbx = await pool.take();
    console.log((await sbx.runCode("1+1")).text);
    await sbx.kill();
    console.log(pool.stats());
    ```

    `Sandbox.create({ pool })` rejects with `InvalidArgumentError` any
    launch option alongside `pool`. The pool's timers are `unref()`ed: an
    idle pool does not keep the process alive, so call `pool.close()` (or
    `await using`) before exiting or the slots stay parked until their
    `timeoutMs`.

### `PoolConfig` { #poolconfig }

In TypeScript `PoolConfig` is the object `new SandboxPool(...)` receives,
with the same fields in camelCase and times in milliseconds.

| Field (Python / TypeScript) | Default | What it is |
|---|---|---|
| `size` | — | parked slots (`1..=64`; 64 × 2 GB is 128 GB of the 1,024 GB regional quota) |
| `template`, `template_version` / `templateVersion` | `RAYITO_TEMPLATE`, latest | the image: a pool is one image and one version |
| `timeout` / `timeoutMs` | 28,800 s | maximum lifetime of each slot (`maximumDurationInSeconds`, counts suspended time); park as long as AWS allows |
| `idle` | `IdlePolicy()` | required and with `auto_resume=True`; `suspended_duration_seconds` resolves to `timeout − max_idle_seconds`, the safety net of a forgotten slot |
| `envs`, `metadata`, `cpu_time_limit`, `execution_role_arn`, `ingress`, `egress`, `logging` | as in `create()` | fixed in the `runHookPayload` and in `run-microvm`: per pool, not per take |
| `allow_internet_access` / `allowInternetAccess`, `network` | `True`, none | the egress policy of all slots, as in `create()`; an agent pool passes `allow_internet_access=False` |
| `min_remaining_seconds` / `minRemainingMs` | 3,600 s | minimum remaining life a slot is handed over with; below it, it is recycled |
| `fill_concurrency` / `fillConcurrency` | 4 | warm-ups in flight (`1..=8`; bounds handles and memory; the pace is set by the token buckets) |
| `sweep_interval_seconds` / `sweepIntervalMs` | 30 s | cadence of recycling and reconciliation (minimum 5 s) |
| `ready_timeout` / `readyTimeoutMs` | 90 s | readiness deadline of each warm-up |
| `index` | none | `DynamoDbIndex(...)`: writes the [metadata index](optional-features.md) row of each slot when it is launched (its own cost, off by default) |
| `warmup` | `()` / `[]` | steps each slot runs before being parked; see [Warm-up](#calentamiento-warmup) |

There is no `access_token` (one per slot, see custody) or `allowed_ports`
(`get_host(port)` mints per port after the take, as after `create()`).
Nor `gateways` or `secrets`: warm slots never carry secrets, which are
bound on take.

### `take()` { #take }

| Python | TypeScript | Default | What it is |
|---|---|---|---|
| `wait` | `waitMs` | 0 | how long to wait for the filler to park a slot before falling back to `create()` |
| `ready_timeout`, `request_timeout`, `reconnect_timeout` | `readyTimeoutMs`, `requestTimeoutMs`, `reconnectTimeoutMs` | as in `create()` | deadlines of the handle that is handed over |
| `secrets`, `secret_cache` | `secrets`, `secretCache` | none | [secrets](optional-features.md) injected into the taken sandbox |
| `gateways` | `gateways` | none | opens the [secrets gateway](funciones-opcionales/pasarela-de-secretos.md) on the taken sandbox, just like `create(gateways=)`; it is what an agent needs |
| — | `logger` | none | the handle's logger |

`close(drain=True)` (TS: `close({ drain })`) and `stats()` complete the
API.

## What a take does { #que-hace-una-toma }

1. Under the pool's lock it picks the `ready` slot that **expires soonest**
   with at least `min_remaining_seconds` of life (the oldest first, so none
   rots) and **deletes its record from the backend before touching the
   network**: from that instant the secret only lives in the `Sandbox` the
   taker receives.
2. Explicit `resume-microvm` (5 TPS bucket): 0.38 s measured versus 0.67 s
   for auto-resume, and a dead slot fails fast (`False`,
   `SandboxNotFoundException`, `SandboxStateException` →
   `terminate-microvm`, `lost += 1`, fallback).
3. It mints the JWE and opens the handle with the slot's token, polling
   `Health` from 0.1 s doubling to 0.5 s (`TakePoll`); **no `get-microvm`**
   on the path (it is eventually consistent and the record already carries
   everything). A failure here terminates the VM, counts `failed` and falls
   back.
4. It returns the `Sandbox` (`access_token == the slot's`, `get_info()` from
   the record, `metadata` from `Health`).
5. **Fallback**: with no ready slot (after waiting up to `take(wait=w)`
   seconds, 0 by default) or with the slot lost, a normal `create()` with
   the same configuration and a fresh token (`misses += 1`). The pool is a
   latency optimization, **never a semaphore**: there is always a sandbox
   or the exception `create()` would have raised. `take()` on a pool that
   is not started or is closed is `PoolClosedException`.

## Filling, quotas and backoff { #relleno-cuotas-y-backoff }

A thread (`rayito-pool-filler-<n>`; an `asyncio.Task` in async, a promise
in TypeScript) keeps `ready + warming == size`. Each warm-up is the normal
`create()` with a fresh `secrets.token_bytes(32)`, **a trivial cell**
(`pass`: `rayd` holds `Execute` while the `/run` kernel rotates, so the slot
is never parked in the middle of the rotation, see below), `pause(wait=True)`
(`suspend-microvm`, **2 TPS** bucket, `get-microvm` until `SUSPENDED`) and
`close()` of the handle: the pool stores data, never channels or JWEs. All
calls go through the process's shared control plane, so the pool and the
application's `create()` calls share the same buckets (`RunMicrovm` 5,
`SuspendMicrovm` 2, `ResumeMicrovm` 5, `TerminateMicrovm` 10,
`CreateMicrovmAuthToken` 50 TPS): a pool of 20 takes ≥ 10 s to fill because
of the suspend bucket and never leaves the application without quota
(order of arrival). After any failed warm-up the filler waits 1 s doubling
up to 60 s (±25 % jitter), counts `failed`, warns once with the exception's
class and carries on: `size` failures in a row do not stop the pool.

**Sizing**: `size` ≈ the peak of sandboxes you need within one fill window
(≈ 8 s per slot with `fill_concurrency=4`); a burst larger than `size`
falls back to `create()` at the usual 5-6 s.

## Recycling, reconciliation and `list()` { #reciclado-reconciliacion-y-list }

Every `sweep_interval_seconds` (and once in `start()`):

- **Recycling**: every `ready` slot with less than `min_remaining_seconds`
  of life is terminated and replenished (`recycled += 1`). With the defaults
  (28,800 / 3,600) an intact slot is recycled ≈ 7 h after launch: none
  reaches the 8 h wall parked and every slot handed over has ≥ 1 h. **The
  wall also applies to whoever takes**: `get_info().remaining_seconds()`
  tells the truth; a pool slot is for short, bursty work.
- **Reconciliation**: a `list-microvms` filtered by image (and by version if
  pinned) and, per `ready` slot: listed `SUSPENDED|SUSPENDING|PENDING` →
  nothing; `RUNNING` → someone resumed it outside the pool,
  `suspend-microvm` and a warning; absent → `get-microvm` and, if it is
  terminal or does not exist, record deleted, `lost += 1` and a warning with
  the `stateReason`. A failed `list-microvms` aborts that sweep with a
  warning; the next one retries.
- Parked slots **show up as `SUSPENDED` in `Sandbox.list()`** and count 2 GB
  each against the regional memory quota (1,024 GB in `us-east-1`,
  `RUNNING + SUSPENDED`).

## Backends { #backends }

`PoolBackend` is the seam for a future shared store; the pool serializes
every call under its own lock, so a backend does not need to be
thread-safe.

- `InMemoryPoolBackend` (the default): records die with the process. A pool
  that dies without `close()` leaves `size` suspended VMs that **terminate
  on their own** at their `timeout` (idle policy): the maximum leak is
  `size` slots of storage for ≤ 8 h (≈ $0.0008 per slot).
- `JsonFilePoolBackend(path)`: `{"schema": "rayito.pool/1", "slots": [...]}`
  with the access tokens **in the clear**, ISO-8601 UTC dates, written
  atomically (`<path>.tmp` + `os.replace`) with mode `0600` (no effect on
  Windows). It is **single-process** (no locking or coordination between
  hosts), meant for tests and for recovering a pool on the same host after
  a restart; both SDKs read the same file. The file is as sensitive as
  `RAYITO_ACCESS_TOKEN`.

`close(drain=False)` leaves the slots parked and their records for another
pool and is **only supported with a persistent backend**. In `start()` the
pool loads the backend: a `warming` record is an orphan of an interrupted
warm-up (it is terminated, `lost += 1`) and every `ready` one goes through
a sweep before it can be taken (`launched` does not change). `stats()`
returns `PoolStats` (`size, ready, warming, takes, hits, misses, launched,
recycled, lost, failed, slots`) with no secret.

## Secret custody { #custodia-del-secreto }

The access token's sha256 travels in `run-microvm`'s `runHookPayload` and
`/run` is accepted **once per startup** (ADR-004, `SECURITY.md` T2), so
**the token that opens a slot is the one the pool minted, for the whole
life of the VM**: there is no rotation in `take()` nor can there be one
without a second `/run`, which `rayd` rejects by design. Consequences
(`SECURITY.md` T14):

- **A fresh 32-byte secret per slot**, never one per pool: a leak opens one
  VM, not the fleet.
- The secret lives in the pool's process (in-memory backend) or in the
  `0600` file (JSON backend) only until `take()`, which deletes the record
  before touching the network; afterwards only the taker's `Sandbox` has it.
  `close(drain=False)` is the only path that leaves secrets at rest on
  purpose.
- Whoever operates the pool can read all the parked secrets: it is the
  **same level of trust** that whoever operates the SDK already has (they
  hold the IAM credentials that mint JWEs and terminate VMs). What changes
  is that a slot has a secret *before* it has a user: an application that
  hands out taken sandboxes among different tenants must treat the pool's
  process as a trusted component of its own, exactly as it treats today
  the process that calls `create()`.
- The JWE is minted on take and never stored: a parked slot is only
  reachable by whoever can mint a JWE for it (IAM) **and** knows its secret
  (ADR-004's two-level authentication does not change).
- Residual: a dump of the process or the JSON file exposes the parked
  secrets; `list-microvms` reveals the ids to any principal with
  `ListMicrovms`; a principal with `ResumeMicrovm` can resume a slot and
  start its counter (the sweep parks it again within ≤ one interval and
  warns about it).

## The `/run` rotation and the settling cell { #la-rotacion-de-run-y-la-celda-de-asentado }

`create()` considers a VM ready with `agent_ready and kernel_ready`. `rayd`
answers 200 to `/run` and schedules the default kernel's rotation in the
background; between that 200 (which opens traffic) and the start of the
rotation there is a window in which `Health` still reports `kernel_ready`
for the unrotated kernel. In the pool e2e, 2 of 40 warm-ups saw
`kernel_ready` at 2-3 s (instead of ≈ 6 s) and, parked in that state, lost
the kernel on resume (`kernel_state_lost`, 6-12 s until the first cell).
That is why each warm-up runs a trivial cell before parking: `rayd` holds
`Execute` while the default context restarts, so the cell only returns on
the rotated kernel, and a `Health` after 0.3 s confirms that no rotation
started afterwards. A slot therefore arrives with `execution_count == 1`.
The root fix (marking `Rotating` in the `/run` handler itself) belongs to
`rayd`, `AWS_API_NOTES.md` Q53.

## Warm-up (`warmup`) { #calentamiento-warmup }

!!! info "Since 0.8.0"
    `PoolConfig.warmup` arrives with
    [0.8.0](novedades/0.8.0.md). The figures in this section are measured
    on real AWS on 2026-10-07 (Q147 of `AWS_API_NOTES.md`); the costs are
    list prices (checked 2026-10-06) for those times, with the detail in
    [Pricing](cost.md#coste-de-la-vm-con-fast-start).
    Guide: [Agent in the sandbox](guias/agente-en-el-sandbox.md).

An [agent](guias/agente-en-el-sandbox.md) pays for its first `exec` (19.6 s
median after `create()` without prefetch, measured) on every new VM.
`PoolConfig.warmup` moves that cost into the slot's warm-up, before parking
it, instead of into the take. It only pays off if many **new** conversations
arrive whose first message has to be fast: for the turns of a single
conversation it is enough to pause the VM between them, without a pool (see
[Which one do I use?](guias/agente-en-el-sandbox.md#que-uso)).

=== "Python"

    ```python
    from rayito import PoolConfig, agent_pool_warmup

    config = PoolConfig(
        size=3,
        template="rayito-agent",
        allow_internet_access=False,  # an agent pool closes egress
        warmup=agent_pool_warmup("opencode"),
    )
    ```

=== "TypeScript"

    ```ts
    import { SandboxPool, agentPoolWarmup } from "rayito";

    const pool = new SandboxPool({
      size: 3,
      template: "rayito-agent",
      allowInternetAccess: false, // an agent pool closes egress
      warmup: agentPoolWarmup("opencode"),
    });
    console.log(pool);
    ```

The model's gateway is opened when the slot is taken, with
`pool.take(gateways=...)`: full example in
[Agent in the sandbox: agent pool](guias/agente-en-el-sandbox.md#pool-de-agentes-c).

`agent_pool_warmup(runtime="opencode")` (TS: `agentPoolWarmup(runtime)`)
returns a list of `WarmupStep`
(`cmd`, `background=False`, `timeout_seconds`/`timeoutMs`, `tag`); you can
pass your own steps in `warmup` just the same. Each step runs after the
settling cell (below) and before `pause()`; a failure counts as a failed
warm-up (the VM is terminated, the same backoff as a normal warm-up is
applied, `failed += 1`). A background step (`background=True`) is launched
and released: the filler does not wait for it to finish. A recycle
relaunches the slot and runs them again.

| Option | What it pre-warms | Measured (pool of 2, n=5) | Cost per recycle cycle (≈ every 7 h) | Cost per slot per month |
|---|---|---|---|---|
| **C: `agent_pool_warmup("opencode")`** | the OpenCode binary already in the page cache (`agent_pool_warmup("deepagents")` does the same for deepagents) | take → first token **p50 5.1 s / p95 6.7 s**; slot ready in 15.5 s | launch $0.0014 + 15.5 s of compute $0.0005 + parking ≈ 0.92 GB $0.0035 ⇒ ≈ **$0.0054** | ≈ **$0.64** (versus $0.60 for a base slot) |

The API does not return the size of a `suspend` snapshot: C's is taken as
equal to the image's (the guest's used memory after the take, 503 MiB, is
smaller than that of a fresh `create()`). A take reads that snapshot:
≈ $0.0014.

### Result of the AWS measurement { #resultado-de-la-medida-en-aws }

Prefetch (option A of
[Agent templates](funciones-opcionales/templates-de-agente.md)) lowers the
first `exec` after `create()` by 76 % (19.6 s → 4.7 s median), so it stays
on by default. C leaves the first token at 5.1 s from the take and is the
recommended option when a pool is needed. How to choose among A, B, C and
no pool: [Agent in the sandbox](guias/agente-en-el-sandbox.md#arranque-rapido).

## Cost per slot (`rayito-base`, 0.92 GB snapshot; `AWS_API_NOTES.md` §12 and `docs/benchmarks/2026-09-cold-start.md`) { #coste-por-plaza-rayito-base-092-gb-de-snapshot-aws_api_notesmd-12-y-docsbenchmarks2026-09-cold-startmd }

| Item per slot | Cost |
|---|---|
| Storage while parked | 0.92 GB × $0.08/GB-month ≈ **$0.074/month** ($0.0001/h) |
| Parking (snapshot write on `pause()`) | 0.92 × $0.0038 ≈ **$0.0035** |
| Taking (snapshot read on `resume`) | 0.92 × $0.00155 ≈ **$0.0014** |
| Warming (launch: read + ≈ 7 s `RUNNING`) | $0.0014 + $0.00025 ≈ **$0.0017** |
| Recycling an idle slot (every ≈ 7 h with the defaults: warm + park) | ≈ **$0.0052** ⇒ ≈ 3.4/day (≈ 103/month) ⇒ ≈ **$0.53/month** |
| **Total idle slot** | ≈ **$0.60/month** (versus $91/month for a `RUNNING` VM) |
| Taken slot | the take ($0.0014) + the normal sandbox ($0.126/h while it runs) |

The full e2e (≈ 46 launches of seconds: 20 takes + 20 `create()` + the
filler slots, recycling and recovery) costs ≈ $0.25.

??? info "Sources and measurements"
    - Design: ADR-008 in [`ARCHITECTURE.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/ARCHITECTURE.md);
      secret custody: T2 and T14 in [`SECURITY.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/SECURITY.md).
    - Pool latencies: [`clients/python/tests/e2e/test_m7_pool.py`](https://github.com/alejandro-cedeno-10/rayito/blob/main/clients/python/tests/e2e/test_m7_pool.py)
      (run of 2026-09-16); cold start:
      [`docs/benchmarks/2026-09-cold-start.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/docs/benchmarks/2026-09-cold-start.md).
    - Prices: `AWS_API_NOTES.md` §12; the `/run` rotation: Q53.

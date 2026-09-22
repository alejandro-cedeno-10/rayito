## Why

The M6 benchmark (`docs/benchmarks/2026-09-cold-start.md` §9, rule D11 of
`m6-benchmark-pool` fixed before measuring) found `B20` = **8.57 s** (p95 of
`run-microvm` → `kernel_ready` in an SDK burst of 20 `create()`; p50 5.68 s)
against the 8 s threshold, and `R` = **0.40 s** (p95 explicit `resume()` →
`Health` with a new generation; auto-resume 0.68 s; first cell 0.10 s in every
state). The rule therefore says **pool**, and because `R < 2 s` it is a pool of
**suspended** MicroVMs, never of `RUNNING` ones at $0.126/h. That decision is
ADR-008 in `ARCHITECTURE.md`, `SPEC.md` §4 no longer lists the pool as a
generic non-goal (only the pre-warmed one), `MILESTONES.md` M7 lists
`m7-suspended-pool` as item 3 ("Decidido en ADR-008: pool en cliente de VMs
suspendidos con traspaso de token"), and the M7 research report
(`docs/research/2026-09-m7-oss-readiness.md` §5) names the acceptance:
`Sandbox.create(pool=…)` p95 to first cell < 1.5 s over 20 takes, slots
recycled before the 8 h wall.

Today `create()` is the only path: every sandbox pays 5–9 s of cold start,
most of it the kernel warm-up that `/run` repeats. A parked suspended VM costs
storage only (≈ 0.92 GB × $0.08/GB-month ≈ $0.07 per slot and month, plus
$0.0035 per park and $0.0014 per take) and comes back usable in ≈ 0.4–0.7 s.
ADR-008 left exactly one design point open — the access token is hashed into
`runHookPayload` at `/run` and cannot be rotated afterwards, so the pool
must mint each slot's secret and hand it to whoever takes the slot — and
this change closes it in writing (`design.md` D4, D9, D14) before any code.

## What Changes

Track 3 of M7 (ADR-008), decided in full by `design.md`:

- **Python SDK first** (`clients/python/src/rayito/`): `PoolConfig` (the
  immutable launch configuration shared by every slot: `size`, `template`,
  `template_version`, `timeout` = 28 800 by default, `idle` — an `IdlePolicy`
  with `auto_resume=True`, mandatory —, `envs`, `metadata`, `cpu_time_limit`,
  `execution_role_arn`, `ingress`, `egress`, `logging`,
  `min_remaining_seconds` = 3 600, `fill_concurrency` = 4,
  `sweep_interval_seconds` = 30, `ready_timeout`), `SandboxPool` and
  `AsyncSandboxPool` (`start()`, `take()`, `stats()`, `close(drain=)`,
  context managers), `PoolStats`, `PoolSlotInfo`, the `PoolBackend`
  protocol with `InMemoryPoolBackend` (default) and `JsonFilePoolBackend`
  (0600, atomic replace, single process, meant for tests and for recovering
  a pool after a restart), and the sugar `Sandbox.create(pool=pool)` /
  `AsyncSandbox.create(pool=pool)`. A slot is warmed with the ordinary
  `create()` (readiness = `agent_ready and kernel_ready`), parked with
  `pause(wait=True)` (`suspend-microvm`, 2 TPS bucket) and its local handle
  closed; `take()` pops the oldest ready slot, calls `resume-microvm`
  (explicit resume: 0.38 s p50 measured, versus 0.67 s auto-resume), opens
  the handle with the slot's own access token and a 100 ms readiness poll,
  and falls back to a plain `create()` when no slot is ready or the slot
  turns out dead. A filler thread keeps `ready + warming == size` through
  the **shared** control plane (so the pool and the application's own
  `create()` calls share the 5 TPS `RunMicrovm`, 2 TPS `SuspendMicrovm`,
  5 TPS `ResumeMicrovm` and 10 TPS `TerminateMicrovm` buckets), backs off
  1 s → 60 s after a failed warm-up, recycles every slot whose remaining
  life drops under `min_remaining_seconds` (the 8 h `maximumDurationInSeconds`
  counts suspended time) and reconciles slots with `list-microvms` every
  sweep (terminated out of band → refilled; resumed out of band →
  re-parked). No server-side component: no Lambda function, no table, no
  service.
- **Token hand-off, decided**: one fresh 32-byte secret per slot, hashed into
  that slot's `runHookPayload`, stored in the backend record, deleted from
  the backend at `take()` and handed to the taker as `sbx.access_token`. No
  rotation exists (`/run` is accepted once per boot, ADR-004): the pool
  process is a custodian of every parked slot's secret, the JSON backend
  stores them at rest, and `SECURITY.md` gains row T14 saying so.
- **TypeScript parity** (`clients/typescript/src/pool/`): `SandboxPool`,
  `PoolConfig`, `PoolStats`, `PoolBackend`, `InMemoryPoolBackend`,
  `JsonFilePoolBackend` and `Sandbox.create({ pool })`, with the same
  semantics and unref'd timers.
- **Tests**: unit tests over an in-memory fake `ControlPlane` state machine
  plus the existing fake `rayd` (Python sync + async, TypeScript); a guarded
  e2e (`tests/e2e/test_m7_pool.py`, marker `e2e`) that measures p95
  `take()` → first cell over 20 takes with `size=3` against 20 plain
  `create()` → first cell, **passes only if the take p95 is under 1.5 s**,
  and a second e2e that watches a slot with `timeout=720` and
  `min_remaining_seconds=600` be recycled before its wall.
- **Docs**: `docs/site/docs/pool.md` (what it is, when it pays, API,
  fallback, recycling, backends, custody of the secret, the cost model per
  slot: storage + park + take + recycle every ≈ 7 h ≈ $0.6 per idle slot and
  month, against $91 for a `RUNNING` one), a `cost.md` row, a README
  snippet, changelog entries, `ARCHITECTURE.md` Capa 3 paragraph and the
  ADR-008 open point closed by reference, `SPEC.md` §4, `MILESTONES.md` M7
  row 3, `AWS_API_NOTES.md` §16 Q53 with the measured take latency.
- **Untouched on purpose**: `rayd`, the `.proto`, the image, the hooks, the
  readiness schedule of `create()`/`connect()`, the token buckets (relaxing
  the 5 TPS bucket is a separate measurement ADR-008 asks for and is **not**
  done here), and `rayito.e2b`, which stays unaware of pools (a unit test
  asserts it).

## Capabilities

### New Capabilities
- `sandbox-pool`: the client-side pool of suspended MicroVMs — configuration,
  slot life cycle, warm-up, take with token hand-off, fallback, refill under
  the shared quotas, recycling before the wall, reconciliation, stats,
  pluggable backend, async parity, security and the measured acceptance.

### Modified Capabilities
- `typescript-sdk`: adds the pool surface, its unit tests and a guarded e2e
  (new requirement; nothing existing changes).
- `python-release`: the docs site nav gains `pool.md` (the "Docs site
  skeleton" requirement is restated with the current ten pages, `verify.md`
  included).
- `e2b-compat`: adds the requirement that the shim neither accepts `pool`
  nor exports any pool name.

## Impact

- New: `clients/python/src/rayito/_pool_base.py`, `_pool_backends.py`,
  `sandbox_sync/pool.py`, `sandbox_async/pool.py`;
  `clients/python/tests/unit/fake_control_plane.py`, `test_pool_base.py`,
  `test_pool_backends.py`, `test_pool_sync.py`, `test_pool_async.py`;
  `clients/python/tests/e2e/test_m7_pool.py`;
  `clients/typescript/src/pool/{config,backend,pool}.ts`,
  `clients/typescript/tests/unit/pool.test.ts`,
  `clients/typescript/tests/e2e/pool.e2e.test.ts`; `docs/site/docs/pool.md`.
- Modified: `clients/python/src/rayito/__init__.py` (exports),
  `sandbox_sync/main.py` and `sandbox_async/main.py` (`pool=` on `create()`,
  a private readiness-schedule parameter on the connect path),
  `clients/typescript/src/index.ts` and `src/sandbox/sandbox.ts`,
  `docs/site/mkdocs.yml`, `docs/site/docs/cost.md`, `README.md`,
  `clients/python/CHANGELOG.md`, `clients/typescript/CHANGELOG.md`,
  `ARCHITECTURE.md` (Capa 3 + ADR-008 amendment), `SECURITY.md` (T14),
  `SPEC.md` §4, `MILESTONES.md` M7, `AWS_API_NOTES.md` §16 (Q53);
  `Makefile` unchanged (`test-e2e` already runs every file under
  `tests/e2e`).
- AWS: every call is one already in `AWS_API_NOTES.md` §2, §3, §5, §6
  (`run-microvm`, `create-microvm-auth-token`, `get-microvm`,
  `list-microvms`, `suspend-microvm`, `resume-microvm`,
  `terminate-microvm`); no new parameter. The e2e launches ≈ 23 pooled
  VMs + 20 plain ones + 2 for the recycle test, each alive seconds, ≈ $0.25
  (pessimistic $0.5), everything terminated at exit and swept by the e2e
  conftest. Parked slots count against the regional memory quota (2 GB
  each; 1 024 GB in us-east-1) and appear as `SUSPENDED` in `list()`.
- Governed by measured facts (`AWS_API_NOTES.md`): suspended time counts
  toward `maximumDurationInSeconds` (§5, §11); `suspend-microvm` and
  `resume-microvm` are idempotent 200s (§5, Q38); a JWE minted before a
  suspension stays valid after the resume, expiry is wall-clock (§3);
  `get-microvm` is eventually consistent, readiness is `Health` (§6);
  `runHookPayload` (token hash, `envs`, `metadata`) is fixed at `/run` and
  `/run` is accepted once per boot (§8, ADR-004, T2); the `/run` payload can
  be logged by CloudTrail so only the hash leaves the client (T4);
  `list-microvms` items carry `microvmId, state` and keep `TERMINATED`
  entries ≈ 20 min (§6); explicit resume 0.38/0.40 s and auto-resume
  0.67/0.68 s (p50/p95, benchmark §4).

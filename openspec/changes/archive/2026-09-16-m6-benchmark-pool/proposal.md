## Why

`SPEC.md` §4 keeps "pool de MicroVMs pre-calentados" as a non-goal "sólo si
M0/M6 demuestran que el cold start lo justifica", and `MILESTONES.md` M6
asks to "decidir aquí, con datos, si hace falta un pool". Every cold-start
number the repo holds today is a by-product of something else: M0 measured
the probe image (672 MB of memory, no kernel rotation) with plain HTTP; the
M4/M5 `kernel_ready_s` values (3.4–9.5 s) come from the SDK's readiness poll,
whose 0.25 s → 2 s doubling backoff caps the resolution at up to 2 s, on
one to seven VMs per session, never in a concurrent burst through the
current image; resume was timed on four cycles; the first `run_code`
after a create or a resume was never isolated from the readiness wait; and
nothing quantifies how much of the 919 MB memory snapshot (346–353 MB of
it the pandas/matplotlib/scipy/sklearn warm-up, Q34) the user pays on every
launch and resume. Meanwhile the cost model in `AWS_API_NOTES.md` §12 is
still "aritmética propia" pending Q8/Q22: Cost Explorer now has two days of
real MicroVM usage to check it against. The pool question, the "coste real"
and "cold start" bullets of `SPEC.md` §7 and the README latency/cost table
cannot be closed without one dedicated, reproducible measurement.

## What Changes

Track D of M6 ("bench + decision"), decided in full by `design.md`:

- `scripts/bench_cold_start.py` (boto3 + the Python SDK `rayito` 0.0.5):
  (a) 20 sequential creates timing `run-microvm` → `Health.agent_ready`
  and → `Health.kernel_ready` with a bench-owned 100 ms `Health` poll;
  (b) concurrent bursts of 5, 10 and 20 creates, once through the SDK
  control plane (5 TPS `RunMicrovm` token bucket, standard retries) and
  once raw (boto3 client with one attempt, no bucket) to observe
  throttling; (c) 10 explicit `pause()`/`resume()` cycles and 10
  `pause()`/auto-resume cycles, p50/p95; (d) the first `run_code` cell
  after every create and after every resume, plus a "stack" cell (pandas +
  matplotlib PNG) after every create; (e) the same (a) and (d) against a
  **slim** image variant whose kernel warm-up is disabled, to correlate
  `memorySnapshotSizeInBytes` with seconds. Every VM is launched with
  `maximumDurationInSeconds=600`, lives ≤ 300 s, is terminated by the
  script and swept at exit; a dry-run cost estimator refuses to start over
  `--budget-usd` (default 5). Raw JSON under `docs/benchmarks/raw/`.
- Image variant: a build flag for the sidecar warm-up (`kernel-sidecar/
  ipython/startup/warmup_variant` marker file read by `0004_warmup.py`;
  the `e2b/chart` formatter registers lazily so nothing imports matplotlib
  in the slim kernel), `scripts/image_zip.py --variant slim` (writes the
  marker into the zip) and `scripts/publish_image.py --variant slim`
  (image `rayito-base-slim`, artifact checked for the marker). Hooks,
  memory and `/validate` are identical in both variants.
- `docs/benchmarks/2026-09-cold-start.md`: setup, the (a)–(e) tables, the
  cost of the run, the Cost Explorer verification of `AWS_API_NOTES.md`
  §12 (unit prices, usage types, real per-launch and per-cycle cost,
  suspend write size) and a **DECISION** section applying the rule fixed
  in `design.md` D11: p95 `kernel_ready` in the SDK 20-burst under 8 s and
  p95 explicit resume under 2 s ⇒ **no pool for v0.1** and a "suspended
  pool" sketch as a future change; otherwise ADR-008 for a pre-warmed
  pool in `ARCHITECTURE.md`.
- Docs with the measured numbers: `SPEC.md` §7 (cold start, cost) and §4
  (pool non-goal resolved), `README.md` cost/latency table,
  `MILESTONES.md` M6 benchmark bullet, `AWS_API_NOTES.md` §12 verified
  prices + §16 Q8, Q22 filled and two rows added (cold start / resume /
  first cell full vs slim; raw burst throttling — Q42/Q43 if this track
  lands before the other M6 tracks, renumbered otherwise), `ARCHITECTURE.md`
  Capa 1 (variants) and sidecar warm-up paragraph.

Not in this change: the pool itself (a follow-up change if the rule says
so), any other M6 track (cgroups, IMDS block, egress, TypeScript client,
E2B shim), any `.proto` or `rayd` change, any change to the SDK's public
surface or readiness poll, any new AWS parameter (every API call is one
already listed in `AWS_API_NOTES.md` §2–§6 plus `ce get-cost-and-usage`).

## Capabilities

### New Capabilities
- `cold-start-benchmark`: the benchmark script (phases, timing points,
  burst modes, guardrails, raw JSON schema), the report, the pool decision
  rule and the Cost Explorer verification.
- `image-variant`: the warm-up build flag in the sidecar, `image_zip.py`
  / `publish_image.py --variant`, and the invariants shared by the full
  and slim images.

### Modified Capabilities
- none (no existing spec changes; `suspend-resume`, `code-execution`,
  `process-lifecycle`, `filesystem` and `pty` are measured, not altered).

## Impact

- New: `scripts/bench_cold_start.py`, `docs/benchmarks/2026-09-cold-start.md`,
  `docs/benchmarks/raw/*.json`, `clients/python/tests/unit/test_scripts_bench.py`,
  `clients/python/tests/unit/test_scripts_image_variant.py`.
- Modified: `kernel-sidecar/ipython/startup/0004_warmup.py` (marker check),
  `kernel-sidecar/ipython/startup/0001_charts.py` (lazy `for_type_by_name`
  registration), `kernel-sidecar/tests/test_kernel.py` (+ slim test),
  `scripts/image_zip.py` (`--variant`), `scripts/publish_image.py`
  (`--variant`, artifact check, default name), `Makefile`
  (`image-zip-slim`, `image-publish-slim`, `bench-cold-start`, `clean`),
  `.github/workflows/ci.yml` (slim zip smoke step), `SPEC.md`, `README.md`,
  `MILESTONES.md`, `AWS_API_NOTES.md`, `ARCHITECTURE.md` (and ADR-008 only
  if the rule says pool).
- AWS: two image versions built from the same tree, the next `rayito-base`
  version (11.0 if Track D publishes first; full, must pass the M1–M5 e2e) and `rayito-base-slim` 1.0 (≈
  $0.04 of minimum storage each), one e2e session (≈ $0.05), then ≈ 112
  short MicroVMs and 20 suspend/resume cycles for the bench, estimated
  $0.5 (pessimistic $1.5) against a hard budget of $5; Cost Explorer
  queries at $0.01 each. Everything terminated at the end of the run.
- Governed by measured facts: `RunMicrovm` 5 TPS with no `ThrottlingException`
  seen at 20 simultaneous (§2, Q3); `SuspendMicrovm` 2 TPS and idempotent
  (§5, Q38); the proxy retains the first request of an auto-resume (Q40);
  `/validate` decides what the lazily restored disk prefetches (Q35);
  the warm-up adds 346–353 MB to the memory snapshot (Q34); `get-microvm`
  is eventually consistent, readiness is `Health` only (§6).

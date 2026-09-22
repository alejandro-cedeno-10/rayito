## Context

State after M5 (accepted 2026-09-16, image `rayito-base` **10.0**: memory
snapshot 919 146 496 B, code install 1 290 379 264 B, disk 37 462 016 B,
`chipsetGeneration` 3; SDK `rayito` 0.0.5; `rayd` 4 533 432 B). What the
repo knows about cold start and cost, and why it is not enough:

- **Readiness numbers come from the SDK's own poll.** `Sandbox.create()`
  returns only at `agent_ready and kernel_ready`; the e2e conftest prints
  that as `kernel_ready_s`. The poll is `ReadinessPoll`: 0.25 s doubling to
  2 s with jitter, so a value like `9.46 s` may be up to 2 s late, and
  `agent_ready` is never timed separately from `kernel_ready`. M5's seven
  VMs: 8.48 / 6.01 / 9.12 / 8.43 / 3.97 / 9.46 / 8.35 s; M4 (8.0, six VMs):
  3.37–8.05 s, p50 6.22 s, `restart_ms` 2 030–4 077 (Q35).
- **Burst behaviour was measured on the probe image only** (M0, Q3: 20
  simultaneous `run-microvm`, 0 `ThrottlingException`, first HTTP 200 p50
  2.53 s / p95 3.59 s, 672 MB memory, no kernel, no `/run` rotation).
  Nothing measures 20 concurrent creates through the current image and
  the SDK's 5 TPS bucket.
- **Resume**: `resume()` → `Health` with the new generation 0.37 / 0.73 s
  (four cycles, Q39); auto-resume first command 0.67 s (three runs, Q40);
  `pause()` → `SUSPENDED` 1.37–1.49 s. No p50/p95 over ten cycles.
- **First cell after create was never isolated**: the e2e runs a CSV write
  first. After resume: `x` → `42` in 0.10 s (Q39).
- **Snapshot size**: the warm-up (numpy, pandas, matplotlib.pyplot,
  scipy.stats, sklearn.linear_model, one PNG, one chart extraction,
  `describe()`, `linalg.inv`) adds 346–353 MB of memory snapshot (572 →
  918–925 MB, Q34) and AWS bills snapshot **read** on every launch and
  resume at $0.00155/GB. Whether that buys or costs seconds at
  `kernel_ready` and at the first stack cell is unmeasured; the design
  knob D14 of M4 (1.2 GB cap) was set without data.
- **Cost**: `AWS_API_NOTES.md` §12 is arithmetic from the pricing page. Q8
  and Q22 are "pendiente (revisar el 2026-09-16)". A first `ce
  get-cost-and-usage` on 2026-09-16 (daily, `SERVICE = AWS Lambda`, grouped
  by `USAGE_TYPE`, all three days still `Estimated`) already shows the
  MicroVM usage types and lets the unit prices be checked (table in D12):
  memory and vCPU seconds, snapshot read/write GB and storage GB-hours
  reconcile with §12 to within 0.3 %; snapshot **read** was the largest
  MicroVM line of 2026-09-15 (113.5 GB, $0.176, more than the $0.113 of
  compute); the 2.54 GB written on 2026-09-15 match M0's four suspend
  cycles of a 672 MB snapshot (so a suspend writes ≈ the memory snapshot,
  not 2 GB, and a cycle at 10.0 costs ≈ $0.005, not the $0.0107 of §12);
  image builds do not appear as snapshot writes; the memory-GB-s / vCPU-s
  ratio is 1.87–1.89, below the nominal 2.0; and 67.5 GB-h of snapshot
  storage were billed on 2026-09-14, before the first Rayito image (the
  account is shared). All of this needs the final (non-estimated) numbers
  and a day with a known launch count to turn into a per-launch cost.

Measured facts this design relies on (`AWS_API_NOTES.md`): `RunMicrovm`
5 TPS applied, no 429 at 20 simultaneous (§2, §11, Q3); `SuspendMicrovm`
2 TPS, idempotent 200 (§5, Q38); `ResumeMicrovm` 5 TPS; auto-resume
retains the first request (Q4, Q40); `CreateMicrovmAuthToken` 50 TPS (§3);
`get-microvm` eventually consistent, readiness is `Health` (§6); the proxy
returns 429 on account or per-VM rate limits and 502 while the snapshot
restores (§7); `/validate` decides what the lazily restored disk has
prefetched — 45 s vs 2–4 s for the first Python start (Q35); the
per-account memory quota is 1024 GB (§11); pricing lines of §12.

Constraints: `openspec/project.md` hard rules (no invented AWS
parameters — every call below is `run-microvm`, `get-microvm`,
`list-microvms`, `suspend-microvm`, `resume-microvm`, `terminate-microvm`,
`create-microvm-auth-token`, `get-microvm-image-version`,
`list-microvm-image-builds`, `get-microvm-image-build`,
`create/update-microvm-image` as already used, plus Cost Explorer
`get-cost-and-usage`; identifiers in English; no inline comments; no
`.proto` change; nothing outside the active track; never log tokens or
code), the user's rule of keeping every VM under 5 minutes and the total
under $5, and "a milestone never closes on mocks": the benchmark is the
acceptance and runs only against real AWS.

Coordination with the other M6 tracks (changes `m6-hardening` — Track A —,
`m6-typescript-sdk` — Track B — and `m6-e2b-compat` — Track C — are being
written in parallel and also touch `scripts/publish_image.py`, `Makefile`,
`ci.yml`, `0004_warmup.py`, `AWS_API_NOTES.md` §16, `MILESTONES.md` M6,
`README.md`): this design names the full subject version **11.0** and the
new §16 rows **Q42/Q43** on the assumption that Track D publishes and
lands first; whoever lands second renumbers (Track A claims Q42–Q46 and a
`rayito-base` version of its own, Track C claims Q42). The bench itself is
agnostic: it records whatever `imageVersion` it launched and whatever
warm-up list `0004_warmup.py` holds at bench time (if Track A's "snapshot
diet" drops `scipy`/`sklearn` first, the full subject is that list and the
report says so). `publish_image.py --variant` (this track) and
`--os-capabilities` (Track A) are independent flags on the same script;
`Makefile` and `ci.yml` additions are additive targets/steps.

## Goals / Non-Goals

**Goals:**

- One reproducible script that produces, in a single run and one JSON
  file, every number the pool decision needs, at 100 ms resolution,
  with the SDK 0.0.5 as the client and the current `rayito-base` version
  as the subject.
- A like-for-like image variant (same Dockerfile, same hooks, same
  `/validate`, same packages on disk; only the kernel warm-up differs) to
  attribute seconds to `memorySnapshotSizeInBytes`.
- A written decision on the pool, with the rule fixed **before** the
  numbers exist, and the sketch of the alternative that costs nothing
  when the rule says "no pool".
- Cost model verified against the bill: unit prices, what a launch, a
  resume and a suspend really cost, and the corrections to §12.
- Docs (`SPEC.md`, `README.md`, `MILESTONES.md`, `AWS_API_NOTES.md`,
  `ARCHITECTURE.md`) carrying the measured numbers, not estimates.

**Non-Goals:**

- Implementing any pool (pre-warmed or suspended): a follow-up change.
- Changing the SDK's readiness poll, `create()`, `connect()`, `pause()` or
  `resume()`; changing `rayd`, the `.proto`, the hooks or their timeouts.
- Tuning the warm-up list, the `/validate` cell or the image size
  (`minimumMemoryInMiB` stays 2048): this change measures two points,
  it does not optimise.
- Comparing regions, memory sizes, or E2B/Modal/Fargate empirically.
- Statistical rigour beyond p50/p95/min/max on 20 (or 10) samples; the
  bench is a decision instrument, not a paper.
- A pytest e2e test for the bench: it is a script with its own guardrails
  (D8), run by hand, whose artefacts are the acceptance evidence.

## Decisions

### D1. One script, four phases, one JSON: `scripts/bench_cold_start.py`

A standalone Python 3.11+ script (stdlib + `boto3` + `rayito`), run from
the SDK environment so `rayito` and `grpcio` are importable:

```
cd clients/python && uv run python ../../scripts/bench_cold_start.py \
    --template <arn|name> [--template-slim <arn|name>] \
    [--sequential 20] [--bursts 5,10,20] [--burst-modes sdk,raw] \
    [--resume-cycles 10] [--phases a,b,c,e] [--out ../../docs/benchmarks/raw] \
    [--execution-role-arn <arn>] [--budget-usd 5] [--dry-run]
```

`make bench-cold-start` wraps it (requires `RAYITO_E2E=1` and
`RAYITO_TEMPLATE`; `RAYITO_TEMPLATE_SLIM` optional; `BENCH_ARGS` passthrough)
exactly like `test-e2e`. `AWS_PROFILE`/`AWS_REGION` come from the
environment (boto3). The script is `ruff` clean under the repo's existing
`uvx ruff check scripts` step, fully type-hinted, English identifiers,
small functions, no inline comments inside bodies. Phases:

| Phase | Flag | What | VMs |
|---|---|---|---|
| a | `--sequential N` (20) | N sequential launches of the full image, each with the (d) cells, terminated before the next | 20 |
| b | `--bursts 5,10,20` × `--burst-modes sdk,raw` | six batches in the order sdk 5, raw 5, sdk 10, raw 10, sdk 20, raw 20, 30 s apart; each VM runs the `1+1` cell only | 70 |
| c | `--resume-cycles N` (10) | VM A: N explicit `pause()`/`resume()` cycles; VM B: N `pause()`/auto-resume cycles; the two VMs run concurrently in two threads | 2 |
| e | `--template-slim` | phase a again against the slim image (same N, same cells) | 20 |

Phase (d) is not a phase: the cells are recorded inside (a), (b), (c) and
(e) samples. `--phases` selects a subset for reruns; every phase writes
into the same JSON (D9) under its own key, so a partial rerun is a
partial file, never a merge.

### D2. Launch helper and timing points (the only way a VM is created)

Every launch in every phase goes through one helper so the numbers are
comparable:

1. `plan = build_launch_plan(image_arn, region, template_version=None,
   timeout=600, idle=<per phase>, envs=None, execution_role_arn=<flag>,
   allowed_ports=None, ingress=None, egress=None, logging=<"cloudwatch"
   if a role was given else "disabled">, access_token=None)` — the same
   request `Sandbox.create()` would send (`rayito._sandbox_base`).
2. `t0 = perf_counter()`; `run-microvm` through the phase's launcher
   (D3); `api_s` = when it returns. On a `ClientError` the sample is
   recorded with `error` = the exception name (`ThrottlingException`,
   `ServiceQuotaExceededException`, `InsufficientCapacityException`, …)
   and `retry_after` when present; no VM exists, nothing else runs.
3. `create-microvm-auth-token` for port 8080, 60 min, through the SDK
   control plane (50 TPS bucket) — `token_s` recorded separately so the
   readiness numbers can be read with or without it (the SDK pays it
   too, after `run-microvm` and before the first `Health`).
4. Bench-owned `Health` poll (`HealthProbe`, D4): `agent_ready_s` = first
   `HealthResponse` with `agent_ready`, `kernel_ready_s` = first with
   `kernel_ready`, both from `t0`; `agent_uptime_ms_at_ready` =
   `uptime_ms` of that first `agent_ready` reply (how long `rayd` had
   been up when the proxy first delivered a request: restore + boot vs
   proxy readiness); `health_polls` and `throttled_polls` counted. Cap:
   `ready_timeout` 120 s → sample `error: "not_ready"`, one `get-microvm`
   for `state`/`stateReason`, terminate.
5. `sbx = Sandbox.connect(microvm_id, access_token=plan.access_token,
   control_plane=<shared plane>)` (public API; the VM is already ready,
   so this costs one `get-microvm`, one token and one `Health`), then the
   cells of D6.
6. `terminate-microvm` through the control plane (10 TPS bucket) in a
   `finally`; `vm_alive_s` = `perf_counter() − t0` at that moment.

Sample record (D9 `LaunchSample`): `phase`, `variant` (`full|slim`),
`mode` (`sdk|raw`), `batch` (burst size or 0), `index`, `microvm_id`,
`image_version`, `started_at` (UTC ISO), `api_s`, `token_s`,
`agent_ready_s`, `kernel_ready_s`, `agent_uptime_ms_at_ready`,
`health_polls`, `throttled_polls`, `first_cell_s`, `stack_cell_s`
(`null` in bursts), `vm_alive_s`, `error` (`null` when ok).

### D3. Two launchers: SDK bucket vs raw

- **`sdk`**: `LambdaMicrovmsControlPlane.from_session(region)` shared by
  the whole run (one `RunMicrovm` bucket at 5 TPS, `client_config()` with
  standard retries, 5 attempts). Concurrent creates queue on the bucket
  exactly as `Sandbox.create()` would.
- **`raw`**: a second `boto3` `lambda-microvms` client with
  `Config(retries={"mode": "standard", "total_max_attempts": 1},
  connect_timeout=5, read_timeout=60, user_agent_extra="rayito-bench")`
  calling `run_microvm(**plan.request.to_api())` directly from N threads
  at once, no bucket. Its only purpose is to observe what AWS does above
  5 TPS (M0 saw slower replies, no 429); a `ThrottlingException` is a
  recorded outcome, not a failure of the bench. Tokens, `Health`,
  `connect()` and terminate of raw VMs still go through the shared plane.

Bursts use `concurrent.futures.ThreadPoolExecutor(max_workers=N)`; the
batch record carries `batch_t0` and `batch_wall_s` (first `run-microvm`
call → last `kernel_ready`), and each sample additionally
`kernel_ready_from_batch_s` (from `batch_t0`) so both "what one create
sees" and "when the whole burst is usable" are reported. Between batches
the script sleeps 30 s and terminates the batch's VMs first.

### D4. `HealthProbe`: fixed 100 ms poll on a bench-owned channel

The SDK's `ReadinessPoll` doubles its interval; the bench needs constant
resolution, so it opens its own channel per VM with the SDK's transport
pieces (`TransportSettings().open_channel(host, ProxyAuthPlugin(store,
port=8080, access_token=None))`, `TokenStore` holding the one JWE minted in
D2 step 3) and `health_pb2_grpc.HealthServiceStub`. Loop: `Health(timeout=
1.0 s)`; `UNAVAILABLE`/`DEADLINE_EXCEEDED` (`is_not_yet_reachable`) → not
yet; `RESOURCE_EXHAUSTED` (proxy 429) → counted in `throttled_polls`, the
next sleep for that VM is 500 ms; any other status → sample `error`;
sleep 100 ms between attempts. The same probe serves phase (c) (D5):
after `resume(wait=False)` it polls until `resume_generation` is greater
than the value seen before the pause **and** `kernel_ready`. Resolution
therefore ≤ 100 ms + one RTT (≈ 93 ms from the current client) on every
readiness number; the report states this. The probe never sends
`x-access-token` (`Health` is the anonymous RPC) and never logs the JWE.

Rate budget: 20 VMs × 10 polls/s = 200 `Health`/s across 20 endpoints
during a 20-burst; the per-VM RPS cap at 1 vCPU is unpublished and the
account-level proxy limit unknown (§7). If 429s appear they are counted
and the backoff above keeps the burst going; the report shows
`throttled_polls` per batch.

### D5. Resume cycles (phase c)

Before the cycles, both VMs run `x = 42` and one `1+1` cell (warm). Then:

- **VM A (explicit)**, `idle=None`: N × [`t = perf_counter()`;
  `sbx.pause()` (`wait=True`; the SDK reads `get-microvm` first and waits
  for `SUSPENDED`) → `pause_s`; dwell `5 s`; `t = perf_counter()`;
  `sbx.resume(wait=False)` then `HealthProbe` until the new generation with
  `kernel_ready` → `resume_s`; `sbx.get_health()` once so the SDK records
  the generation; `t = perf_counter()`; `sbx.run_code("x")` → `first_cell_
  after_resume_s`, `kernel_alive = (text == "42")`; `resume_generation`
  recorded].
- **VM B (auto)**, `idle=IdlePolicy(max_idle_seconds=60,
  suspended_duration_seconds=600, auto_resume=True)`: N × [`pause()` →
  `pause_s`; dwell 5 s; `t = perf_counter()`; `sbx.commands.run("echo
  back")` → `auto_resume_s` (the proxy retains the `Start` stream, Q40;
  `stdout.strip() == "back"` asserted); `run_code("x")` →
  `first_cell_after_resume_s`, `kernel_alive`; generation recorded]. The
  explicit `pause()` followed by auto-resume on the first request is the
  sequence M0 Q4 measured (1.56 s).

`SuspendMicrovm` is 2 TPS: the two threads share the plane's bucket, so
a cycle pair never exceeds it. N = 10 ⇒ ≈ 10 × (1.5 + 5 + 1 + 0.5) ≈ 80 s
per VM, well inside the 300 s wall budget and the 600 s
`maximumDurationInSeconds` (suspended time counts, §5). Cycle record
(`CycleSample`): `vm` (`explicit|auto`), `cycle`, `pause_s`, `dwell_s`,
`resume_s` or `auto_resume_s`, `first_cell_after_resume_s`,
`resume_generation`, `kernel_alive`, `error`.

### D6. The cells (phase d)

Run through the public `sbx.run_code(code, timeout=60)` right after
`connect()` (create path) or right after the generation change (resume
path); timing is the wall time of the call:

- `first_cell_s`: `"1+1"`, asserted `text == "2"`. Measures kernel
  responsiveness with nothing to import (both variants).
- `stack_cell_s` (phases a and e only): one cell — `import io; import
  numpy as np; import pandas as pd; import matplotlib.pyplot as plt;
  df = pd.DataFrame({"a": [1.0, 2.0]}); fig, ax = plt.subplots();
  ax.plot(df["a"]); buf = io.BytesIO(); fig.savefig(buf, format="png");
  plt.close(fig); len(buf.getvalue())` — asserted `int(text) > 0`. On the
  full image the modules are already in the kernel (warm-up); on slim they
  are imported from the lazily restored disk that `/validate` prefetched.
  This is the number the warm-up buys or costs the user.

The cells are the only code the bench executes in a sandbox; their text
is a constant in the script and appears in the report, never in logs.

### D7. Slim variant: the warm-up build flag

Production mechanism (an image environment variable would not reach the
kernel: `rayd` gives the sidecar a from-scratch environment and the
sidecar builds the kernel's from its own): a marker file
`kernel-sidecar/ipython/startup/warmup_variant` next to the startup
scripts, absent in the repo. Rules:

- `0004_warmup.py`: reads `Path(__file__).with_name("warmup_variant")`;
  if it exists and its stripped content is `slim`, `_rayito_warmup()`
  returns before importing anything; any other content or absence = full
  warm-up (today's behaviour). The docstring records the flag.
- `0001_charts.py`: the `Figure._repr_e2b_chart_` monkey-patch (which
  imports `matplotlib.figure`, i.e. most of matplotlib and numpy, on every
  kernel start) is replaced by `shell.display_formatter.formatters
  ["e2b/chart"].for_type_by_name("matplotlib.figure", "Figure",
  _repr_e2b_chart_)`: IPython resolves the type lazily the first time a
  `Figure` is displayed, so a slim kernel starts with no scientific module
  loaded and the full kernel's memory snapshot is unchanged (the warm-up
  imports matplotlib anyway). `0002_data.py` and `0003_images.py` already
  import nothing heavy at start.
- `scripts/image_zip.py image <zip> [--variant full|slim]`: `slim` adds a
  synthetic entry `kernel-sidecar/ipython/startup/warmup_variant` with
  content `slim\n` (mode 0644, fixed date like every entry) to the
  archive; nothing is written into `image/`. The content hash (and so the
  S3 key) differs from the full zip by construction. `full` (default)
  writes nothing.
- `scripts/publish_image.py --variant full|slim` (default `full`):
  default image name `rayito-base` / `rayito-base-slim` (`--image-name`
  still overrides), log group `/rayito/<image-name>` as today, and a
  check that the artifact zip's marker matches the flag (present with
  `slim` ⇔ `--variant slim`); a mismatch is a `SystemExit` before any
  upload. `IMAGE_HOOKS`, `--memory-mib` and the rest are identical for
  both variants; `/validate` (the `rayd` cell: kernel restart + pandas +
  matplotlib) is unchanged and runs in both, so the slim image's disk
  prefetch is as good as the full one's and the comparison isolates the
  memory snapshot.
- `Makefile`: `image-zip-slim` (`image/rayito-image-slim.zip`, reuses the
  `build` and sidecar copy of `image-zip`), `image-publish-slim`, `clean`
  removes the slim zip (`.gitignore` already covers `/image/*.zip`).
  `.github/workflows/ci.yml` adds one smoke step after the existing
  `image_zip.py` step: build the slim zip and assert the marker entry is
  present in it and absent in the full one.
- Tests: `kernel-sidecar/tests/test_kernel.py` gains
  `test_slim_variant_starts_without_the_stack`: a temporary sidecar root
  (copies of `ipython/` and `jupyter/`, `src` reused) with the marker, a
  kernel started from it, `sorted(m for m in ("numpy", "pandas",
  "matplotlib") if m in sys.modules)` → `[]` after `ready`, then a
  `plt.plot` cell still yields `e2b/chart` and `image/png` (lazy
  formatter), and `test_warmup_leaves_the_namespace_clean` keeps asserting
  `['numpy', 'pandas']` for the default root. `clients/python/tests/unit/
  test_scripts_image_variant.py` (adds `scripts/` to `sys.path` like the
  sidecar conftest does for `src`): `image_zip` slim zip contains the
  marker with `slim\n` and the full one does not, exclusions unchanged;
  `publish_image` `--variant slim` defaults the name to `rayito-base-slim`,
  refuses a full zip, and `--variant full` refuses a slim zip.

Both subjects are built from the same tree on the same day (task 3):
`rayito-base` **11.0** (full: `make image-publish`, identical to 10.0 but
for the lazy chart registration) and `rayito-base-slim` **1.0** (`make
image-publish-slim`), same `rayd` binary, same sidecar, same Dockerfile,
same hooks; only the marker differs. 11.0 becomes the latest `ACTIVE`
full version and must pass the whole real-AWS e2e (`make test-e2e`, M1–M5)
before the bench runs: that is the regression gate for the
`0001_charts.py` change (`results[0].png` and `e2b/chart` in
`test_m4_code.py` / `test_m5_pty_suspend_resume.py` block 2). Both
`snapshotBuild` sizes and build seconds are recorded next to 10.0's.
Expected from Q34: slim memory ≈ 570 MB, code install unchanged (1.29
GB), shorter build (no 9.3–9.7 s warm-up in the build VM); full 11.0
within a few MB of 10.0.

### D8. Guardrails: cost, lifetime, cleanup

- Every `run-microvm`: `maximumDurationInSeconds=600`; `idle=None` except
  VM B of phase (c).
- Wall budget per VM: `VM_WALL_BUDGET_SECONDS = 300`. Each phase
  terminates its VMs as soon as their samples are complete; a watchdog
  check at every phase boundary and at exit terminates any tracked VM
  older than 300 s and marks its sample `over_budget: true`.
- Pre-flight: `list-microvms` filtered by each image ARN; more than 10
  live (non-`TERMINATING|TERMINATED`) VMs → refuse to start (same rule as
  the e2e conftest). Exit sweep: terminate every VM the run launched that
  is not yet terminal, then list again and print any survivor of either
  image with its `startedAt` (never terminating VMs the run did not
  launch).
- `--dry-run` prints the plan (phases, VM count, cycle count) and the
  cost estimate; the estimate uses the §12 unit prices with the
  pessimistic 300 s life per VM: `launches × (memory_snapshot_gb × 0.00155
  + 300 × (0.0000276944 + 2 × 0.0000036667)) + cycles × memory_snapshot_gb
  × (0.0038 + 0.00155) + slim storage 2.0 GB × 0.08 × 7/30`. The run
  refuses to start when the estimate exceeds `--budget-usd` (5). With the
  defaults (112 launches, 20 cycles, 0.92 GB) the estimate is ≈ $1.5;
  the realistic figure is ≈ $0.5.
- `Ctrl-C` (`KeyboardInterrupt`) runs the sweep and writes the JSON with
  what was collected (`meta.aborted: true`).
- No secrets: the script never prints the JWE, the access token or the
  `runHookPayload`; `boto3`/`grpc` loggers stay at WARNING.

### D9. Raw JSON schema

One file per run: `docs/benchmarks/raw/2026-09-cold-start-<runid>.json`,
`runid` = UTC `YYYYMMDDTHHMMSSZ` of the start. Committed with the report.

```json
{
  "schema": "rayito.bench.cold-start/1",
  "meta": {
    "generated_at": "...Z", "run_id": "...", "aborted": false,
    "region": "us-east-1", "client_rtt_ms": 93.0,
    "sdk_version": "0.0.5", "boto3_version": "...", "python": "3.12.x",
    "rayd_version": "<Health.agent_version of the first ready VM>",
    "args": {"sequential": 20, "bursts": [5, 10, 20], "burst_modes": ["sdk", "raw"],
             "resume_cycles": 10, "phases": ["a", "b", "c", "e"], "execution_role": false},
    "images": {
      "full": {"arn": "...", "version": "10.0", "memorySnapshotSizeInBytes": 919146496,
               "codeInstallSizeInBytes": 1290379264, "diskSnapshotSizeInBytes": 37462016,
               "chipsetGeneration": 3},
      "slim": {"...": "same keys, or null when --template-slim was not given"}
    }
  },
  "phases": {
    "sequential": {"variant": "full", "samples": ["LaunchSample"], "summary": "Summary"},
    "bursts": [{"variant": "full", "mode": "sdk", "size": 5, "batch_t0": "...Z",
                "batch_wall_s": 0.0, "throttled": 0, "failed": 0,
                "samples": ["LaunchSample"], "summary": "Summary"}],
    "resume": {"explicit": {"samples": ["CycleSample"], "summary": "Summary"},
               "auto": {"samples": ["CycleSample"], "summary": "Summary"}},
    "sequential_slim": {"variant": "slim", "samples": ["LaunchSample"], "summary": "Summary"}
  },
  "cost": {"launches": 112, "throttled_launches": 0, "suspends": 20, "resumes": 20,
           "vm_seconds": 0.0, "snapshot_read_gb_expected": 0.0,
           "snapshot_write_gb_expected": 0.0, "estimated_usd": 0.0}
}
```

`Summary` = `{"<metric>": {"n", "p50", "p95", "min", "max"}}` for every
numeric field of the samples (`api_s`, `token_s`, `agent_ready_s`,
`kernel_ready_s`, `kernel_ready_from_batch_s`, `first_cell_s`,
`stack_cell_s`, `pause_s`, `resume_s`, `auto_resume_s`,
`first_cell_after_resume_s`, …), computed over samples without `error`.
p50 = `statistics.median`; p95 = nearest-rank, `sorted[ceil(0.95 n) − 1]`
(the 19th of 20, the 10th of 10). `client_rtt_ms` = median of five
`Health` round-trips on the first ready VM of the run. The images block
comes from `get-microvm-image-version` (the launched version: the
response's `imageVersion`) + `list-microvm-image-builds` +
`get-microvm-image-build` (`snapshotBuild`, `chipsetGeneration`), the
same calls `publish_image.py` makes.

### D10. The report: `docs/benchmarks/2026-09-cold-start.md`

Written by hand from the JSON (the script prints the tables in Markdown
to make that mechanical; `--report` prints only). Sections, in order:

1. **Setup**: date, region, account (already public in the repo), client
   location and `client_rtt_ms`, SDK/`rayd`/boto3 versions, both images
   with version, `snapshotBuild` sizes and build seconds, the poll
   resolution statement (D4), the run id(s) and raw file(s).
2. **(a) Sequential creates — full**: table `metric | n | p50 | p95 | min
   | max` for `api_s`, `token_s`, `agent_ready_s`, `kernel_ready_s`,
   `agent_uptime_ms_at_ready`, `first_cell_s`, `stack_cell_s`; a one-line
   comparison with M0 Q2 (probe: 1.85 / 2.13 s to first HTTP 200) and M4
   Q35 (p50 6.22 s via the SDK poll).
3. **(b) Bursts**: one row per batch (`mode`, `size`, `ok`, `throttled`,
   `failed`, `batch_wall_s`, `api_s` p50/p95, `agent_ready_s` p50/p95,
   `kernel_ready_s` p50/p95, `kernel_ready_from_batch_s` max,
   `throttled_polls`), then the raw-vs-sdk reading: did AWS throttle at
   5/10/20 simultaneous, did latency degrade instead (M0: yes, no 429).
4. **(c) Resume**: explicit and auto tables (`pause_s`, `resume_s` /
   `auto_resume_s`, `first_cell_after_resume_s`), `kernel_alive` 20/20,
   generation sequence; comparison with Q39/Q40.
5. **(d) First cell**: `first_cell_s` after create (a, b) and after resume
   (c) side by side, and `stack_cell_s` full vs slim.
6. **(e) Snapshot size vs seconds**: one table with both variants:
   `memorySnapshotSizeInBytes`, `codeInstallSizeInBytes`, build seconds,
   `agent_ready_s` p50/p95, `kernel_ready_s` p50/p95, `first_cell_s`
   p50, `stack_cell_s` p50/p95, snapshot read $ per launch (`bytes /
   1e9 × 0.00155`), and the derived Δ per 100 MB of memory snapshot on
   `kernel_ready_s` p50 (two points only: stated as such).
7. **Cost of this run**: launches, cycles, VM-seconds, expected snapshot
   GB read/written, estimate at §12 prices, and — once Cost Explorer has
   the day — the actual usage lines of the bench day (D12) and the real
   per-launch / per-cycle / per-VM-minute cost.
8. **Cost Explorer verification of `AWS_API_NOTES.md` §12** (D12).
9. **DECISION** (D11).
10. **Appendix**: exact command lines, `--dry-run` output, the cells'
    source, deviations from this design (if any) and the date the numbers
    were taken.

Every table cell that comes from the JSON names the summary key it comes
from; no number appears in the report that is not in a raw file, except
the Cost Explorer lines (quoted with their query).

### D11. The pool decision rule (fixed before measuring)

Inputs: `B20 = phases.bursts[mode=sdk, size=20].summary.kernel_ready_s.p95`
(per-create latency in the SDK-bucketed 20-burst, the realistic worst
case an application sees) and `R = phases.resume.explicit.summary.
resume_s.p95`. Rule:

- **`B20 < 8.0 s` and `R < 2.0 s` ⇒ no pool for v0.1.** The report's
  DECISION section states both numbers, says so, and includes the
  **suspended pool** sketch as the optional future change
  (`m7-suspended-pool` candidate, not scheduled): the SDK (client side,
  no service) keeps N sandboxes created ahead with `IdlePolicy(auto_resume=
  True)`, paused right after `kernel_ready`; `Sandbox.create()` takes one
  and its first request auto-resumes it (measured `auto_resume_s`, ≈ 0.7
  s in M5); cost while parked = snapshot storage of the suspended VM
  ($0.08/GB-month × memory snapshot ≈ $0.07 per parked VM per month) plus
  one write per park ($0.0038/GB) and one read per take ($0.00155/GB);
  constraints the sketch must state: the 8 h `maximumDurationInSeconds`
  counts suspended time, so a parked VM is recycled before it expires
  (one launch per slot per < 8 h); the per-VM `runHookPayload` (token
  hash, envs) is fixed at `/run`, so the pool owner mints the access
  token and the taker inherits it (a token handoff inside the SDK's
  process, or a `/run`-time secret rotation that does not exist today —
  the sketch names this as the open design point); `envs` per sandbox
  are therefore pool-wide; suspended VMs hold the region memory quota
  (§11) and are visible in `list()` as `SUSPENDED`. `SPEC.md` §4 keeps the
  pool as a non-goal, now "decidido en M6 con datos: no en v0.1; pool de
  suspendidos como cambio futuro opcional".
- **Otherwise ⇒ pool.** The implementer writes **ADR-008 "Pool de
  MicroVMs"** in `ARCHITECTURE.md` (context = the measured numbers;
  decision = which pool: pre-warmed `RUNNING` VMs cost $0.126/h each and
  are only justified if `R ≥ 2 s` too, otherwise the suspended pool
  above; consequences = the token handoff, recycling, quota) and `SPEC.md`
  §4 drops the non-goal; the pool implementation is a separate OpenSpec
  change proposed in the ADR, not part of this one.

The thresholds come from the M6 brief (E2B's ≈ 1.6 s burst figure is the
competitor's claim, not a target: at $0.126/h a pool of running VMs is
the price of matching it, and the rule says when that price is worth
paying). The report also states the p50s and the slim numbers next to
the rule, so a reader can see how far from the thresholds the platform
is; the rule itself is not adjusted after the fact.

### D12. Cost Explorer verification of §12

Query (already run once on 2026-09-16 while all three days were
`Estimated`):

```
aws ce get-cost-and-usage --time-period Start=2026-09-14,End=<bench day + 1> \
  --granularity DAILY --metrics UnblendedCost UsageQuantity \
  --group-by Type=DIMENSION,Key=USAGE_TYPE \
  --filter '{"Dimensions":{"Key":"SERVICE","Values":["AWS Lambda"]}}'
```

Lines that belong to MicroVMs (the account also runs ordinary Lambda:
`Lambda-GB-Second-ARM`, `Request-ARM`, `Lambda-Event-Poller-Unit-Hour`,
durable execution are **not** ours and are excluded):

| Usage type | 2026-09-15 (est.) | USD | Implied unit price | §12 |
|---|---|---|---|---|
| `Lambda-MicroVM-Memory-GB-Second-ARM` | 6 177.505 GB-s | 0.022651 | $0.000003667/GB-s | $0.0000036667 ✓ |
| `Lambda-MicroVM-vCPU-Second-ARM` | 3 262.989 vCPU-s | 0.090367 | $0.00002769/vCPU-s | $0.0000276944 ✓ |
| `Lambda-MicroVM-Snapshot-Read-GB` | 113.489 GB | 0.175541 | $0.001547/GB | $0.00155 ✓ |
| `Lambda-MicroVM-Snapshot-Write-GB` | 2.540 GB | 0.009647 | $0.003798/GB | $0.0038 ✓ |
| `Lambda-MicroVM-Snapshot-Storage-GB-Hour` | 115.845 GB-h | 0.012872 | $0.0001111/GB-h = $0.080/GB-month | $0.08 ✓ |

2026-09-16 (partial, est.): memory 761.467 GB-s, vCPU 406.281 s, read
16.486 GB, storage 82.721 GB-h, no write line yet. 2026-09-14: storage
67.514 GB-h and nothing else — before the first Rayito image.

What the implementer verifies and records (tasks 5.x), with the final
numbers of 2026-09-15/16 and of the bench day:

1. Unit prices (table above) — expected to hold; §12 gets a "verificado
   en Cost Explorer" line with the five usage-type names.
2. **Per-launch snapshot read**: on the bench day the run knows its
   launches (L), resumes (R) and the memory snapshot of each variant;
   `Snapshot-Read-GB / (L + R)` weighted by variant gives the GB read per
   launch and whether AWS bills the memory snapshot in GB or GiB (0.919
   vs 0.856 for 10.0) or reads more than memory (code install pages).
   Records the real cost per launch of the full and slim images.
3. **Per-suspend write**: `Snapshot-Write-GB / suspends` on the bench day
   (20 suspends); the 2026-09-15 data (2.54 GB for M0's four cycles of a
   672 MB snapshot) suggests ≈ the memory snapshot. Corrects the §12
   "ciclo suspend+resume ≈ $0.0107" (assumed 2 GB) to the measured value
   and the `max_idle_seconds` break-even (≈ 2.5 min at 10.0 sizes instead
   of 5).
4. **Compute ratio**: memory GB-s ÷ vCPU-s was 1.89 (09-15) and 1.87
   (09-16) instead of 2.0; with the bench day's known VM-seconds the
   implementer states which of the two is rounded and how many VM-seconds
   were billed against the ≈ 112 × `vm_alive_s` the JSON sums.
5. **Builds** (Q22): whether the bench-day build of `rayito-base-slim`
   shows up in any MicroVM line (2026-09-15's eleven builds produced no
   write line; build compute is not identifiable in the shared
   `Lambda-GB-Second-ARM`), and whether the storage line grows by the new
   version's `memory + codeInstall + disk` GB the next day (minimum one
   week: check storage GB-h on the day after and seven days after
   publishing, the latter as a note in `AWS_API_NOTES.md` to be closed
   later).
6. **The 2026-09-14 storage baseline** (67.5 GB-h before any Rayito image):
   `list-microvm-images` in the account; if only Rayito images exist,
   the line is recorded as unexplained shared-account usage and the
   Rayito storage is computed as the delta over that baseline.
7. **Q8** (1 h active + 7 h suspended, 2 GB): computed from the verified
   unit prices and the measured write/read sizes rather than run (an 8 h
   VM would break the 5-minute rule); recorded as "derivado de precios
   verificados" with the formula.

Every figure goes to `AWS_API_NOTES.md` §12 and §16 (Q8, Q22 filled;
Q42/Q43 new) and to the report's section 8.

### D13. Docs alignment (with numbers, after the run)

- `SPEC.md` §7: the "Coste real" bullet rewritten with the verified
  prices and the measured per-launch / per-cycle cost; the "Cold start"
  bullet rewritten with (a)/(b)/(c)/(d)/(e) p50/p95 and a pointer to the
  report; §4 pool non-goal line updated per D11.
- `README.md`: a "Coste y latencia (medido 2026-09)" table: create →
  `kernel_ready` p50/p95 (sequential and 20-burst), `resume()` p50/p95,
  auto-resume p50, first cell p50, cost per launch, per cycle, per hour
  at 2 GB, with the link to the report.
- `MILESTONES.md` M6: the benchmark bullet replaced by the measured
  outcome (numbers, image versions, decision, report path) and a
  "Track D" status line.
- `AWS_API_NOTES.md`: §12 per D12; §15 gains one sentence on the slim vs
  full effect; §16 Q8 and Q22 "Medida" filled, Q42 "cold start, ráfaga,
  resume y primera celda con `rayito-base` 10.0 y `rayito-base-slim`
  1.0" and Q43 "`run-microvm` en ráfaga raw de 5/10/20 sin bucket" added
  in the table's style.
- `ARCHITECTURE.md`: Capa 1 gains a "Variantes de imagen" paragraph
  (marker file, `--variant`, `rayito-base-slim` is a measurement image,
  not a product template) and the sidecar warm-up paragraph gains the
  build flag and the measured cost of the warm-up; ADR-008 only per D11.
- `SECURITY.md`: unchanged (the bench adds no surface; the marker is
  read-only data in the image).

### D14. Tests

- `clients/python/tests/unit/test_scripts_bench.py` (adds `scripts/` to
  `sys.path`): `percentile` (nearest-rank on 1, 10, 20 samples; p95 of
  20 = 19th), `summarize` (skips samples with `error`, `n` right, empty
  → `null`s), `estimate_cost` (defaults ≈ $1.5, zero phases → slim storage
  only, budget refusal), `plan_from_args` (phases/batch order sdk 5, raw
  5, sdk 10, …), `render_tables` (Markdown row per metric), the JSON
  schema round-trip (`json.dumps` of a synthetic run validates the keys of
  D9), and the watchdog rule (a fake clock: a VM at 301 s is selected for
  termination and flagged). No AWS or network in unit tests.
- `test_scripts_image_variant.py` and the sidecar test per D7.
- `uvx ruff check scripts` clean; `cd clients/python && uv run pytest
  tests/unit && uv run ruff check . && uv run ruff format --check . && uv
  run mypy src` clean; `cd kernel-sidecar && uv run pytest && uv run ruff
  check . && uv run mypy src` clean.
- Acceptance = the real-AWS run (tasks 4.x) with its JSON, the report,
  the docs, and every VM `TERMINATED` (`list-microvms` for both images
  after the run shows no `RUNNING|SUSPENDED|PENDING`).

## Risks / Trade-offs

- **Proxy 429 on the bench's own polling** (200 `Health`/s in a
  20-burst): counted and backed off per VM (D4); if a batch shows
  `throttled_polls > 0` the report says so and the per-create numbers are
  still valid (a 429 costs at most 500 ms of resolution on that VM).
- **Bench-owned readiness differs from the SDK's** (fixed 100 ms vs
  doubling backoff): intentional; the report explains that the SDK's own
  `create()` returns up to one poll interval later than `kernel_ready_s`
  and that M4/M5 numbers were taken with that poll.
- **Raw burst may create VMs the script loses track of** if
  `run_microvm` times out client-side after AWS accepted it (the
  `clientToken` is set, but with one attempt there is no retry to learn
  the id): the exit sweep lists both images and prints survivors; the
  implementer terminates any by hand and notes it. `maximumDurationInSeconds
  =600` caps the damage at ≈ $0.02 per lost VM.
- **Two data points for the size correlation** (full 919 MB, slim ≈ 570
  MB): enough to price the warm-up, not to fit a curve; the report states
  the Δ per 100 MB as an interpolation between two points.
- **Cost Explorer lag** (~24 h, `Estimated` for two days): tasks 5.x are
  scheduled the day after the run; the report is published with the
  estimate and amended with the actual lines (the JSON keeps the
  expectation to compare against).
- **Shared account noise** in Cost Explorer: only the five `Lambda-MicroVM-*`
  usage types are attributed; other MicroVM users in the account during
  the bench day would inflate them — the implementer checks
  `list-microvm-images` and `list-microvms` for foreign images (task 5.1)
  and notes any.
- **`for_type_by_name` in `0001_charts.py`** changes how the chart
  formatter attaches (lazy instead of a class attribute) for the full
  image too: covered by the existing `test_startup_scripts_produce_chart_
  data_and_png` in the sidecar suite and, on real AWS, by the M4/M5 e2e
  run against `rayito-base` 11.0 before the bench (D7). It affects neither
  the memory snapshot of the full image (matplotlib is imported by the
  warm-up anyway) nor kernel readiness; if the e2e disagrees, 11.0 is set
  `INACTIVE`, the registration reverts to the class attribute and the
  slim variant gates the `Figure` import on the marker instead (a second,
  uglier mechanism kept only as the fallback).
- **Publishing 11.0 costs one version** ($0.037 of minimum storage, ≈ 200
  s of build) and one e2e session (≈ seven VMs, ≈ $0.05): accepted so
  that full and slim differ in nothing but the warm-up.
- **`auto_resume` cycles depend on `max_idle_seconds=60`**: with 5 s
  dwells and continuous activity between cycles the idle timer never
  fires; if a cycle's `commands.run` finds the VM already resumed
  (generation unchanged), the sample is marked `error: "not_suspended"`
  and excluded.

## Migration Plan

1. Land the sidecar flag, the `--variant` flags and the bench script with
   their unit tests; CI green (`ruff` on scripts, SDK and sidecar suites,
   the slim zip smoke step).
2. `make image-publish` → `rayito-base` 11.0 and `make image-publish-slim`
   → `rayito-base-slim` 1.0 (three-state gate each; builds may run
   concurrently, the quota is 10); record sizes and build seconds.
   `RAYITO_E2E=1 RAYITO_TEMPLATE=<rayito-base arn> make test-e2e` green
   on 11.0 (M1–M5) before any bench VM is launched.
3. `--dry-run`, then the full run (`make bench-cold-start` with both
   templates); JSON committed under `docs/benchmarks/raw/`.
4. Report + DECISION + docs (D10, D11, D13); Cost Explorer pass the next
   day (D12) amends the report and `AWS_API_NOTES.md`.
5. Rollback: nothing to roll back in production (no `rayd`/proto/SDK
   surface change); `rayito-base-slim` may be set `INACTIVE` or deleted
   after the run (minimum one week of storage is billed either way).
6. Acceptance agent checks the JSON, the report, the docs, the terminated
   fleet and archives the change.

## Open Questions

None blocking. To be answered by the run and recorded (D12, D13): whether
AWS throttles raw bursts of 10/20 (Q43); the real GB read per launch and
per resume and whether builds/storage appear as expected (Q22, §12); the
snapshot size Δ vs seconds (Q42/e); the 1.87–1.89 memory/vCPU ratio; the
2026-09-14 storage baseline; and the pool decision itself (D11), which is
mechanical once `B20` and `R` exist.

## Amendments (2026-09-16, implementation)

Recorded before the run, per `CLAUDE.md` ("no improvisar en silencio"):

1. **Full subject = `rayito-base` 10.0, no 11.0** (D7, tasks 3.1/3.3). The
   M6 brief measures "the current image", and `crates/`, `kernel-sidecar/`
   and `image/` are being edited in parallel by Track A, so a full rebuild
   from the working tree would not be like-for-like with anything. The slim
   subject is built from the **10.0 artifact itself** (`rayd-fc0df3c41a28.zip`
   extracted; `scripts/image_zip.py` reproduces its sha256 byte for byte)
   plus exactly three files: the marker, `0004_warmup.py` (marker check) and
   `0001_charts.py` (lazy `for_type_by_name`). `rayito-base-slim` 1.0 therefore
   differs from 10.0 by the warm-up alone (`rayd`, Dockerfile, sidecar `src`,
   hooks, `/validate` identical). The e2e regression gate of task 3.3 does not
   apply to the full image (10.0 already passed M1–M5); the lazy chart
   registration is covered by `kernel-sidecar/tests/test_variant.py` (real
   kernel, Linux CI) and by a manual `plt.show()` check on one slim VM after
   the run (report appendix). The next full `rayito-base` version (Track A)
   carries both startup changes and goes through the e2e as usual.
2. **Test locations.** `clients/python/` is owned by Track C during M6, so the
   bench and variant unit tests live in `scripts/tests/` (`make test-scripts`,
   run from the SDK environment) instead of `clients/python/tests/unit/`; the
   sidecar test is the new file `kernel-sidecar/tests/test_variant.py` rather
   than an addition to `test_kernel.py`. Same assertions as D7/D14.
3. **`ingress=["ALL_INGRESS"]`** in the launch plan (D2 said `None`): the e2e
   conftest's measured configuration, kept so the bench launches exactly what
   M1–M5 launched.
4. **`token_s`** is the duration of the `create-microvm-auth-token` call
   (D2 "recorded separately"), not an offset from `t0`.
5. **Extra record fields**, additive to D9: `resume_api_s` (the
   `resume(wait=False)` call alone, which includes the SDK's token re-mint),
   `state`/`state_reason` on `not_ready` samples, `dwell_s`, and
   `cost.failed_launches`. `pause_s` has the 0.5 s resolution of the SDK's
   `get-microvm` state poll (`wait_for_state`), stated in the report.
6. **`agent_uptime_ms_at_ready`** measures `rayd`'s monotonic uptime, which
   the snapshot carries over from the build VM (≈ 25 s at the first `Health`
   of the smoke VM): it is "snapshot age + restore-to-first-request", not
   restore latency; kept as data, interpreted as such.
7. **Cost Explorer timing.** All three days were still `Estimated` on the
   bench day; the D12 pass runs the day after (task 5) and the report ships
   with the run-day estimate next to the lines available at write time.
8. **Lazy formatters by lookup, not `for_type_by_name`** (D7). On the real
   kernel the deferred registration of `0001_charts.py` was gone by the time
   a figure was displayed (`rayito-base-slim` 1.0: `image/png` but no
   `e2b/chart`; `deferred_printers` empty after `import matplotlib.pyplot`):
   IPython's `select_figure_formats`, run by the inline backend on the first
   `pyplot` import, does `f.pop(Figure, None)` on **every** formatter, which
   removes a deferred entry by name. Both `0001_charts.py` and
   `0002_data.py` (which, contrary to D7's assumption, imported pandas at
   start) now override `lookup_by_type` and match the class by
   `(__module__, __name__)` along the MRO; nothing is imported at start and
   there is no registry entry to pop. Verified locally with an in-process
   `InteractiveShell` after calling `select_figure_formats`, and on AWS
   (`rayito-base-slim` 2.0). The slim delta spec is amended accordingly.
9. **Two slim versions.** `rayito-base-slim` 1.0 (marker + `0004_warmup.py`
   + the `for_type_by_name` `0001_charts.py`; pandas and numpy still loaded
   at start by `0002_data.py`: 750 694 400 B of memory) is kept as an
   intermediate data point; 2.0 (marker + the three lazy startup scripts) is
   the "nothing scientific at start" subject of phase (e). Both are in the
   report; the run JSON of phase (e) against 2.0 is a second raw file.

Recorded after the run, from the M6 review (2026-09-16):

10. **Measured resolution, not nominal** (D4). The runs of the report were
    polled through the SDK's `CHANNEL_OPTIONS` (reconnect backoff 500 ms →
    2 s) with a 1.0 s per-RPC deadline, so a refused first connection or a
    held request cost 0.5–2 s: the mean poll period per sample
    (`(kernel_ready_s − api_s − token_s) / health_polls`) was p50 0.28 /
    p95 0.37 s in (a) and up to 0.7 s in (e), and `agent_ready_s` is
    quantised in ≈ 1 s steps. D4's "≤ 100 ms + one RTT" did not hold. Fix:
    the probe opens its channel with bench-owned options (`initial` and
    `max` reconnect backoff 100 ms; `min` stays 500 ms because grpc-core
    uses `grpc.min_reconnect_backoff_ms` as the connect timeout — with it at
    100 ms every launch of a first validation attempt ended `not_ready`),
    a 0.5 s RPC deadline, and records the return time of every poll so each
    readiness point carries `agent_ready_gap_s` / `kernel_ready_gap_s`
    (launch samples) and `resume_gap_s` (explicit cycles): the gap since the
    previous poll returned, i.e. the window the transition fell in. The
    report states the resolution of the existing runs from the raw data,
    says that `B20` exceeds the 8 s threshold by less than that uncertainty
    (the rule is still applied verbatim), and drops the "≈ 0.2 s / 100 MB"
    `agent_ready` slope (below the demonstrated resolution). A validation
    run of the corrected probe (`20260916T201841Z`, 5 full + 5 slim,
    ≈ $0.015) measured `agent_ready_gap_s` p50 0.21 / p95 0.22 s (full) and
    0.30 / 0.61 s (slim), `kernel_ready_gap_s` 0.19–0.20 / 0.22–0.29 s: the
    nominal figure holds except when the proxy holds a request to the
    deadline. That run launched the latest image versions (`rayito-base`
    15.0, published by Track A meanwhile) and is cited only for the gaps.
11. **Exit path and registry semantics** (D8). `run_benchmark` marks the
    run aborted on any exception leaving the phases (not only Ctrl-C),
    sweeps, writes the JSON, and only then lists survivors; the image lookup
    at exit is guarded (`meta.images.<variant>.error`) so an API hiccup
    after a paid run cannot lose the file. `VmRegistry` marks a VM
    terminated only after `terminate-microvm` returns; any exception is
    logged, the VM stays pending for the watchdog and the sweep, and the ids
    still pending are printed at exit ("terminate by hand").
12. **Tests** (D14). `scripts/tests/test_bench_guardrails.py` exercises the
    probe's status classification and gaps, `wait_ready` timings and its
    120 s cap, `boot_vm`'s `not_ready` and throttled-launch samples, the
    watchdog flagging, the sweep under a failing terminate, the guarded
    image block and the Ctrl-C / crash exit paths with a fake control plane
    (the `ControlPlane` protocol), a fake `HealthServiceStub` and a fake
    clock; `kernel-sidecar/tests/test_formatters.py` runs `0001_charts.py`
    and `0002_data.py` in an in-process `InteractiveShell`, asserts nothing
    scientific is imported, calls `select_figure_formats` and checks
    `e2b/chart` and `e2b/data` (the scenario "registration survives the
    inline backend setup"), on any host with the pinned requirements. CI
    runs `scripts/tests` after the Python client job.


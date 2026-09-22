# cold-start-benchmark Specification

## Purpose
TBD - created by archiving change m6-benchmark-pool. Update Purpose after archive.
## Requirements
### Requirement: One script creates every benchmark VM through one launch helper
`scripts/bench_cold_start.py` SHALL create every MicroVM through a single launch helper that builds the request with the SDK's `build_launch_plan` (`maximumDurationInSeconds=600`, `idle=None` unless the phase says otherwise, logging `cloudwatch` only when `--execution-role-arn` is given), records `t0` immediately before `run-microvm`, then mints one `create-microvm-auth-token` for port 8080 (`token_s`), polls `Health` with the bench-owned probe until `agent_ready` (`agent_ready_s`) and until `kernel_ready` (`kernel_ready_s`), both measured from `t0`, records `uptime_ms` of the first `agent_ready` reply as `agent_uptime_ms_at_ready`, opens the sandbox with the public `Sandbox.connect(microvm_id, access_token=…, control_plane=…)`, runs the phase's cells, and calls `terminate-microvm` in a `finally`, recording `vm_alive_s`. A `ClientError` from `run-microvm` SHALL produce a sample with `error` set to the exception name (and `retry_after` when present) and no VM; a VM not ready after 120 s SHALL produce `error: "not_ready"` with one `get-microvm` `state`/`stateReason` and be terminated.

#### Scenario: sequential launch sample
- **WHEN** phase a launches one VM of the full image
- **THEN** the sample holds `api_s`, `token_s`, `agent_ready_s`, `kernel_ready_s`, `agent_uptime_ms_at_ready`, `health_polls`, `throttled_polls`, `first_cell_s`, `stack_cell_s`, `vm_alive_s`, `microvm_id`, `image_version`, `started_at` and `error: null`, with `agent_ready_s <= kernel_ready_s <= vm_alive_s`

#### Scenario: throttled raw launch
- **WHEN** a raw-mode `run-microvm` raises `ThrottlingException`
- **THEN** the sample records `error: "ThrottlingException"` and `retry_after`, no token is minted, no probe runs, and the batch's `throttled` count increases by one

### Requirement: The readiness probe polls at a fixed 100 ms on a bench-owned channel
The bench SHALL open its own gRPC channel per VM with the SDK transport pieces (`TransportSettings(options=…).open_channel`, `ProxyAuthPlugin(store, port=8080, access_token=None)`, `TokenStore`) using bench-owned channel options: the SDK's `CHANNEL_OPTIONS` with `grpc.initial_reconnect_backoff_ms` and `grpc.max_reconnect_backoff_ms` at 100 ms and `grpc.min_reconnect_backoff_ms` left at the SDK's 500 ms (grpc-core uses it as the connect timeout; at 100 ms no TLS handshake completes through the proxy). It SHALL call `HealthService.Health` with a 0.5 s per-RPC timeout every 100 ms; `UNAVAILABLE` and `DEADLINE_EXCEEDED` SHALL count as "not yet", `RESOURCE_EXHAUSTED` SHALL be counted in `throttled_polls` and followed by a 500 ms sleep, any other status SHALL end the sample with `error`. The probe SHALL record the return time of every poll and each readiness point SHALL carry the gap since the previous poll returned: `agent_ready_gap_s`, `kernel_ready_gap_s` on launch samples and `resume_gap_s` on explicit resume cycles. The same probe SHALL serve the resume phase by waiting for `resume_generation` greater than the value seen before the pause together with `kernel_ready`. The probe SHALL never send `x-access-token` and SHALL never print or log the JWE. The report SHALL state the measured resolution as the p50/p95 of the gap fields (for runs made before the gap fields existed, the mean poll period per sample computed from the raw data), never a nominal figure.

#### Scenario: readiness resolution is measured per sample
- **WHEN** a VM becomes reachable 2.34 s after `t0`
- **THEN** `agent_ready_s` is the return time of the first `Health` with `agent_ready` measured from `t0`, `agent_ready_gap_s` is the time since the previous poll returned (≈ 0.1 s + one RTT unless the proxy held or refused a request), and the true transition lies within that gap plus one RTT before `agent_ready_s`

#### Scenario: a refused first connection costs one poll interval
- **WHEN** the proxy refuses the first connection attempt of a VM's channel
- **THEN** the next attempt happens about 100 ms later (not the SDK's 500 ms–2 s), and a connection attempt that hangs is given the 500 ms connect timeout

#### Scenario: proxy rate limit while polling
- **WHEN** a `Health` poll fails with `RESOURCE_EXHAUSTED`
- **THEN** `throttled_polls` increases, the next poll of that VM happens 500 ms later, and the sample still completes with a `kernel_ready_s`

### Requirement: Sequential creates measure the full image twenty times
Phase a SHALL launch `--sequential` (default 20) VMs of `--template` one after another, each terminated before the next launch, each running the two phase-d cells, and SHALL summarise `api_s`, `token_s`, `agent_ready_s`, `agent_ready_gap_s`, `kernel_ready_s`, `kernel_ready_gap_s`, `agent_uptime_ms_at_ready`, `first_cell_s` and `stack_cell_s` as `n`, `p50` (median), `p95` (nearest rank: `sorted[ceil(0.95 n) − 1]`), `min` and `max` over error-free samples.

#### Scenario: twenty sequential samples
- **WHEN** phase a runs with the defaults against `rayito-base`
- **THEN** `phases.sequential.samples` has 20 entries, `phases.sequential.summary.kernel_ready_s.n` equals the number of error-free samples and its `p95` is the 19th smallest `kernel_ready_s` when all 20 succeeded

### Requirement: Bursts run through the SDK bucket and raw, in a fixed order
Phase b SHALL run the batches sdk 5, raw 5, sdk 10, raw 10, sdk 20, raw 20 (from `--bursts` × `--burst-modes`), each with `ThreadPoolExecutor(max_workers=N)` issuing N launches at once; `sdk` SHALL launch through the shared `LambdaMicrovmsControlPlane` (its 5 TPS `RunMicrovm` token bucket and standard retries), `raw` SHALL launch through a boto3 `lambda-microvms` client configured with `total_max_attempts=1` and no bucket, with tokens, probes, `connect()` and terminates still through the shared plane. Each batch SHALL record `batch_t0`, `batch_wall_s` (first `run-microvm` call → last `kernel_ready`), `throttled`, `failed`, and each sample `kernel_ready_from_batch_s`; burst VMs SHALL run only the `1+1` cell. The bench SHALL terminate a batch's VMs and sleep 30 s before the next batch.

#### Scenario: SDK burst of twenty is serialised by the bucket
- **WHEN** the sdk 20 batch runs
- **THEN** the twenty `run-microvm` calls are spaced by the 5 TPS bucket (the last `api_s` start is about 4 s after `batch_t0`), no sample has `error: "ThrottlingException"`, and `kernel_ready_from_batch_s.max` is at least `kernel_ready_s.max`

#### Scenario: raw burst records what AWS does above 5 TPS
- **WHEN** the raw 20 batch issues twenty simultaneous `run-microvm` calls
- **THEN** every call ends either as a ready sample or as an `error` sample with the AWS exception name, `throttled` equals the number of `ThrottlingException` samples, nothing is retried, and the report states whether latency degraded or requests were rejected

### Requirement: Resume cycles measure explicit and auto-resume ten times each
Phase c SHALL create two VMs of the full image and run `--resume-cycles` (default 10) cycles on each, concurrently in two threads, after `x = 42` and one warm `1+1` cell: VM A (`idle=None`) SHALL time `pause()` (`pause_s`), dwell 5 s, time `resume(wait=False)` until the probe sees the new generation with `kernel_ready` (`resume_s`), call `get_health()` once, and time `run_code("x")` (`first_cell_after_resume_s`, `kernel_alive = text == "42"`); VM B (`IdlePolicy(max_idle_seconds=60, suspended_duration_seconds=600, auto_resume=True)`) SHALL time `pause()`, dwell 5 s, time `commands.run("echo back")` (`auto_resume_s`, `stdout.strip() == "back"`), and time `run_code("x")`. A cycle whose VM is found already resumed before its timed request SHALL be recorded with `error: "not_suspended"` and excluded from the summary. Every cycle SHALL record `resume_generation`.

#### Scenario: explicit cycles
- **WHEN** VM A completes ten cycles
- **THEN** `phases.resume.explicit.samples` has ten entries with `kernel_alive: true`, strictly increasing `resume_generation`, and the summary carries `pause_s`, `resume_s` and `first_cell_after_resume_s` with `n = 10`

#### Scenario: auto-resume cycles
- **WHEN** VM B completes ten cycles
- **THEN** each `auto_resume_s` is the wall time of the `commands.run("echo back")` that woke the VM (the proxy retains the stream, no SDK reconnect), `kernel_alive` is `true` in all ten and the generation increased once per cycle

### Requirement: First-cell latency is timed after every create and every resume
The bench SHALL time `sbx.run_code("1+1", timeout=60)` (`first_cell_s`, asserted `text == "2"`) right after `connect()` on every launched VM, and in phases a and e additionally the stack cell (`numpy`, `pandas`, `matplotlib.pyplot` imports, a two-row `DataFrame`, one line plot saved as PNG into a `BytesIO`, last expression the PNG length; `stack_cell_s`, asserted `int(text) > 0`); after every resume it SHALL time `run_code("x")` (`first_cell_after_resume_s`). The cell sources SHALL be constants in the script, reproduced in the report and never logged.

#### Scenario: first cell after create
- **WHEN** a phase-a VM reaches `kernel_ready`
- **THEN** `first_cell_s` is the wall time of `run_code("1+1")` returning `"2"` and `stack_cell_s` the wall time of the stack cell returning a positive integer

#### Scenario: first cell after resume
- **WHEN** a phase-c cycle sees the new generation
- **THEN** `first_cell_after_resume_s` is the wall time of `run_code("x")` returning `"42"`

### Requirement: The slim variant repeats the sequential phase for the size correlation
When `--template-slim` is given, phase e SHALL repeat phase a (same `--sequential`, same two cells) against the slim image into `phases.sequential_slim`, and `meta.images` SHALL hold, for each variant, the launched `imageVersion` and its `memorySnapshotSizeInBytes`, `codeInstallSizeInBytes`, `diskSnapshotSizeInBytes` and `chipsetGeneration` read with `get-microvm-image-version`, `list-microvm-image-builds` and `get-microvm-image-build`. The report SHALL present both variants side by side with the snapshot read cost per launch (`bytes / 1e9 × 0.00155`) and the Δ per 100 MB of memory snapshot on `kernel_ready_s` p50, stated as a two-point interpolation.

#### Scenario: full and slim compared
- **WHEN** the run had both templates
- **THEN** `meta.images.full` and `meta.images.slim` are filled, `phases.sequential_slim.summary` exists with the same keys as `phases.sequential.summary`, and the report's section (e) shows `kernel_ready_s`, `first_cell_s`, `stack_cell_s` and the per-launch read cost for both

### Requirement: Cost, lifetime and cleanup guardrails
The bench SHALL launch every VM with `maximumDurationInSeconds=600`; SHALL terminate any tracked VM older than 300 s at every phase boundary and at exit, flagging its sample `over_budget: true`; SHALL refuse to start when more than 10 non-terminal VMs of either image exist; SHALL, on exit, `KeyboardInterrupt` or any other exception leaving the phases, terminate every VM it launched, write the JSON (with `meta.aborted: true` unless the phases completed) before any further AWS call, then list both images and print any surviving VM with its `startedAt` without terminating VMs it did not launch. A VM SHALL count as terminated only once `terminate-microvm` returned: a failed call (any exception) SHALL be logged, leave the VM pending for the watchdog and the exit sweep, and its id SHALL be printed at exit as not terminated by the run. A failure of the image lookup (`get-microvm-image-version` and the build calls) SHALL be recorded as `meta.images.<variant>.error` and SHALL NOT prevent the JSON from being written. The bench SHALL implement `--dry-run` printing the plan and a cost estimate computed with the `AWS_API_NOTES.md` §12 unit prices and a pessimistic 300 s life per VM; SHALL refuse to run when that estimate exceeds `--budget-usd` (default 5); SHALL keep `boto3` and `grpc` loggers at WARNING and never print the JWE, the access token or the `runHookPayload`.

#### Scenario: dry run under budget
- **WHEN** `--dry-run` runs with the defaults and both templates
- **THEN** it prints 112 launches, 20 cycles, the batch order and an estimate below $5, makes no AWS call, and exits 0

#### Scenario: over budget refused
- **WHEN** `--sequential 2000` is requested with `--budget-usd 5`
- **THEN** the script prints the estimate, refuses to start and exits 2

#### Scenario: nothing left running
- **WHEN** the run ends, normally or by `Ctrl-C`
- **THEN** every VM the run launched is `TERMINATING` or `TERMINATED`, the JSON exists, and the survivor listing for both images is printed

#### Scenario: a crash after a paid run still writes the JSON
- **WHEN** an exception other than `KeyboardInterrupt` leaves the phases, or the image lookup at exit raises
- **THEN** the tracked VMs are swept, the JSON is written with `meta.aborted: true` (or with `meta.images.<variant>.error` set when only the lookup failed), and the exception propagates afterwards

#### Scenario: terminate-microvm fails transiently
- **WHEN** `terminate-microvm` raises for one tracked VM during a phase or the sweep
- **THEN** the other tracked VMs are still terminated, the failed VM stays pending and is retried by the next watchdog check and the exit sweep, and its id is printed at exit if it is still not terminated

### Requirement: Raw JSON file and Markdown tables
Each run SHALL write `docs/benchmarks/raw/2026-09-cold-start-<runid>.json` (`runid` = UTC `YYYYMMDDTHHMMSSZ`) with `schema: "rayito.bench.cold-start/1"`, `meta` (`generated_at`, `run_id`, `aborted`, `region`, `client_rtt_ms` = median of five `Health` round trips on the first ready VM, `sdk_version`, `boto3_version`, `python`, `rayd_version`, `args`, `images`), `phases` (`sequential`, `bursts`, `resume.explicit`, `resume.auto`, `sequential_slim`, each with `samples` and `summary`), and `cost` (`launches`, `throttled_launches`, `suspends`, `resumes`, `vm_seconds`, `snapshot_read_gb_expected`, `snapshot_write_gb_expected`, `estimated_usd`). `--phases` SHALL write only the selected phases into a new file, never merge. The script SHALL print the report tables in Markdown (`metric | n | p50 | p95 | min | max`, one batch per row for bursts) at the end of a run and on `--report <json>` without touching AWS.

#### Scenario: file written and rendered
- **WHEN** a run finishes
- **THEN** the JSON validates against the keys above, every numeric summary value comes from error-free samples, and `--report <that file>` prints the same tables the run printed

### Requirement: The pool decision applies a rule fixed before the measurement
The report `docs/benchmarks/2026-09-cold-start.md` SHALL contain a DECISION section quoting `B20 = phases.bursts[mode=sdk,size=20].summary.kernel_ready_s.p95` and `R = phases.resume.explicit.summary.resume_s.p95` and applying: `B20 < 8.0 s` and `R < 2.0 s` ⇒ no pool for v0.1, with the suspended-pool sketch (N sandboxes created ahead with `auto_resume=True` and paused after `kernel_ready`, taken by `Sandbox.create()`, woken by their first request; cost = snapshot storage of the parked VM plus one write per park and one read per take; constraints: the 8 h `maximumDurationInSeconds` counts suspended time so slots are recycled, the `runHookPayload` token and `envs` are fixed at `/run` so the taker inherits the owner's token — the open design point —, parked VMs hold the region memory quota and appear in `list()`) recorded as an optional future change and `SPEC.md` §4 keeping the pool as a non-goal decided with data; otherwise ADR-008 "Pool de MicroVMs" in `ARCHITECTURE.md` (measured context, which pool and why, token handoff, recycling, quota, follow-up change) and `SPEC.md` §4 dropping the non-goal. The thresholds SHALL NOT be changed after the numbers exist; the report SHALL also show the p50s and the slim numbers next to the rule.

#### Scenario: under both thresholds
- **WHEN** `B20` is 6.9 s and `R` is 1.1 s
- **THEN** the DECISION says "no pool for v0.1", includes the suspended-pool sketch with every listed constraint, and `SPEC.md` §4 reads that the pool was decided against in M6 with data

#### Scenario: over a threshold
- **WHEN** `B20` is 9.5 s or `R` is 2.4 s
- **THEN** the DECISION says "pool", `ARCHITECTURE.md` gains ADR-008 with the measured context and the chosen pool kind, and the implementation is named as a separate change

### Requirement: Cost Explorer verifies the price model and records the real per-launch cost
The implementer SHALL query `ce get-cost-and-usage` (daily, `SERVICE = AWS Lambda`, grouped by `USAGE_TYPE`) covering 2026-09-14 through the day after the bench, attribute only the usage types `Lambda-MicroVM-Memory-GB-Second-ARM`, `Lambda-MicroVM-vCPU-Second-ARM`, `Lambda-MicroVM-Snapshot-Read-GB`, `Lambda-MicroVM-Snapshot-Write-GB` and `Lambda-MicroVM-Snapshot-Storage-GB-Hour`, and record in `AWS_API_NOTES.md` §12 and the report: the implied unit price of each type against §12; the GB read per launch and per resume on the bench day (`Snapshot-Read-GB / (launches + resumes)`, weighted by variant) and the resulting $ per launch of the full and slim images; the GB written per suspend and the $ per pause/resume cycle at the measured sizes with the recomputed `max_idle_seconds` break-even; the memory-GB-s ÷ vCPU-s ratio against the JSON's `vm_seconds`; whether the two image builds appear in any MicroVM line and how the storage line moved the day after publishing; the 2026-09-14 storage baseline explained or recorded as shared-account usage; and Q8 derived from the verified prices with its formula. §16 Q8 and Q22 SHALL be filled and two rows added (cold start / burst / resume / first cell full vs slim; raw burst throttling), numbered after the rows the other M6 tracks land (Q42/Q43 when this track lands first).

#### Scenario: prices reconcile
- **WHEN** the final (non-estimated) lines of 2026-09-15 are divided by their usage quantities
- **THEN** each implied price matches the §12 figure within 1 % and §12 gains a "verificado en Cost Explorer" line naming the five usage types

#### Scenario: per-launch cost recorded
- **WHEN** the bench day's `Snapshot-Read-GB` is divided by the run's launches plus resumes
- **THEN** the GB per launch is compared with `memorySnapshotSizeInBytes` (GB vs GiB, extra pages) and the $ per launch of each variant appears in §12, in Q22 and in the report

### Requirement: The measured numbers replace the estimates in the repo docs
After the run the implementer SHALL update `SPEC.md` §7 ("Coste real", "Cold start") and §4 (pool line), `README.md` (a "Coste y latencia (medido 2026-09)" table with create → `kernel_ready` p50/p95 sequential and 20-burst, `resume()` p50/p95, auto-resume p50, first cell p50, $ per launch, per cycle and per hour at 2 GB, linking the report), `MILESTONES.md` M6 (benchmark bullet replaced by the outcome and a Track D status line), `AWS_API_NOTES.md` (§12, §15 sentence, §16 Q8/Q22 and the two new rows) and `ARCHITECTURE.md` (Capa 1 variants paragraph, sidecar warm-up flag and cost; ADR-008 only if the decision says pool). No number in those docs SHALL lack a source in a raw JSON, the Cost Explorer output or the publish logs.

#### Scenario: README table
- **WHEN** a reader opens `README.md` after archive
- **THEN** the cost/latency table shows the measured p50/p95s and $ figures with the date and a link to `docs/benchmarks/2026-09-cold-start.md`


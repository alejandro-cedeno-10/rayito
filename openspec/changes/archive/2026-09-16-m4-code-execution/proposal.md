## Why

Rayito's headline feature is E2B-style `run_code()` against a warm Jupyter
kernel: rich results (`png`, `chart`, `data`, `html`), structured errors and
a server-enforced execution timeout, with the kernel state surviving
`pause()`/`resume()` later (M5). After M3 the agent still answers
`UNIMPLEMENTED` on every `CodeService` RPC, `Sandbox.run_code` raises
`NotImplementedError`, and the image ships no kernel at all. M4 is the
highest-risk milestone (`MILESTONES.md`): the snapshot must capture a warm
kernel without leaking cloned secrets or PRNG state across sandboxes, and
the whole path (sidecar, agent proxy, SDK models) has to be correct on real
AWS before M5 can build suspend/resume on top of it.

## What Changes

Milestone goal, from `MILESTONES.md` ("M4 — Ejecución de código"), decided in
full by `design.md`:

- `kernel-sidecar/`: a Python 3.12 package (`rayito_kernel_sidecar`) that
  `rayd` spawns as a child (uid 1000) and talks to over JSON lines on
  stdin/stdout. Ops `create_context`, `execute`, `interrupt`,
  `destroy_context`, `restart_context`, `list_contexts`, `quiesce`, `resume`,
  `reseed`, `ping`; events `ready`, `reply`, `started`, `stdout`, `stderr`,
  `result`, `error`, `end`, `kernel_died`. One `ipykernel` per context
  through `jupyter_client.AsyncKernelManager(transport="ipc")` under
  `/run/rayito/k/<context_id>/`, one `asyncio.Lock` per context, executions
  mapped from Jupyter messages exactly like E2B's `messaging.py`, mime
  bundles incl. `e2b/chart` and `e2b/data` produced by IPython startup
  scripts with a vendored `e2b_charts` (MIT, license kept), kernel config
  `NoColor` + `max_seq_length = 0`, warm-up startup script importing
  `numpy`/`pandas`/`matplotlib`/`scipy`/`sklearn` and rendering one PNG,
  pinned requirements (`ipykernel==6.31.0`, `ipython==9.15.0`,
  `jupyter_client==8.10.0`, `pyzmq==27.2.0`, `matplotlib==3.10.9`,
  `pandas==2.2.3`, `numpy==2.3.5`, `scipy`, `scikit-learn`).
- `rayd`: a `code` domain in `rayd-core` (sidecar protocol codec, context
  registry, execution tracker, timeout plan, kernel readiness machine, the
  `/run` rotation and `/resume` reseed rules) with the `KernelSidecar` port;
  in `rayd` the `TokioSidecarLauncher` adapter, a `SidecarSupervisor`
  (restart with backoff, kills orphaned kernels, feeds `kernel_ready`), a
  `CodeManager`, and `CodeGrpc` implementing `CreateContext`, `Execute`
  (server-stream: `started`/`stdout`/`stderr`/`result`/`error`/`end`,
  `KeepAlive` every 5 s, `timeout_ms` enforced by interrupt then restart,
  cancel-on-stream-drop → interrupt), `ListContexts`, `DestroyContext`,
  `RestartContext`; `Reattach` stays `UNIMPLEMENTED` until M5. Hooks:
  `/ready` answers 503 until the default kernel is warm (escape valve at
  300 s), `/validate` runs a real pandas + matplotlib cell and answers 503
  until it finishes, `/run` restarts the default kernel (fresh HMAC key,
  fresh PRNG seeds, the payload's `envs`) in a background task, `/resume`
  reseeds `random`/`numpy.random` in every live kernel.
  `HealthResponse.kernel_ready` becomes real.
- Python SDK: `run_code(code, context, on_stdout, on_stderr, on_result,
  on_error, envs, timeout=300, request_timeout)` → `Execution(results,
  logs, error, execution_count)` with `.text`; `Result` with every mime
  field, `formats()` and `_repr_*_`; `Chart` models parsed from
  `e2b/chart`; `ExecutionError(name, value, traceback)`; `OutputMessage`;
  `CodeContext`; `create_code_context`, `list_code_contexts`,
  `remove_code_context`, `restart_code_context`; `create()` and `connect()`
  wait for `agent_ready and kernel_ready`. Sync and async trees over
  `_code_base.py` / `_charts.py`.
- `.proto`: **no changes**. `code.proto`, `common.proto` and `health.proto`
  already carry everything; the design fixes how every field is used.
- Image: `python3.12` + the pinned scientific stack, the sidecar copied to
  `/opt/rayito/sidecar`, matplotlib font cache built at build time as
  `user`; `snapshotBuild` sizes reported before/after.
- Tests: `rayd-core` host tests for every domain rule, a `cfg(unix)`
  integration suite `crates/rayd/tests/m4_code.rs` against a stdlib-only
  fake sidecar (plus an ignored real-sidecar case), the sidecar's own
  pytest suite (protocol on any host, real kernel on Linux/CI), an
  in-process `FakeCodeService` for the SDK unit tests, and the real-AWS
  `tests/e2e/test_m4_code.py`.

Full acceptance criteria: `design.md` "Acceptance test list".

## Capabilities

### New Capabilities
- `code-execution`: the `kernel-sidecar` process and its stdio protocol,
  the `CodeService` proxy in `rayd` (contexts, executions, timeout,
  keepalive, cancellation), kernel readiness in `/ready`, `/validate`,
  `/run`, `/resume` and `Health.kernel_ready`, the snapshot-uniqueness rules
  for kernels, and the Python client's `run_code()` / code-context surface
  with its models.

### Modified Capabilities
- (none — `process-lifecycle` and `filesystem` are untouched; M4 reuses
  `UserLookup`, `UserPolicy`, `IdentitySwitch`, `build_child_env`,
  `resolve_cwd` and the `PreExecPlan` spawn posture without changing their
  contracts. `Code.Reattach` remains `UNIMPLEMENTED`; M5 modifies this
  capability to implement it.)

## Impact

- `proto/rayito/v1/*.proto`: unchanged; no regeneration needed.
- New top-level `kernel-sidecar/` (never published to PyPI): `pyproject.toml`
  (dev tooling only), `requirements.txt` (exact pins), `src/rayito_kernel_sidecar/`,
  `src/rayito_kernel_sidecar/_vendor/e2b_charts/` (+ `LICENSE`),
  `ipython/ipython_kernel_config.py`, `ipython/startup/000{1,2,3,4}_*.py`,
  `jupyter/kernels/rayito/kernel.json`, `tests/`.
- `crates/rayd-core`: new `code` module (`protocol`, `context`, `execution`,
  `timeout`, `readiness`, `hooks`, `error`, `ports`) and the `KernelSidecar`
  / `SidecarLink` / `KernelStatus` ports; `HealthSnapshot` unchanged.
- `crates/rayd`: `adapters/sidecar_process.rs` (`cfg(unix)` with an
  `Unsupported` stand-in), `code/{supervisor,manager,execute,validate}.rs`,
  `grpc/code.rs` (`PendingCodeService` removed), `grpc::router` and
  `hooks::router` gain a `CodeManager`, `main.rs` wiring and the
  `--sidecar-cmd`/`--sidecar-root`/`--socket-root`/`--no-sidecar` flags,
  `logging.rs` allowlist. `Cargo.toml`: `getrandom` (workspace, exact pin).
- `crates/rayd/tests/m1_hello.rs`, `m2_process.rs`, `m3_filesystem.rs`:
  adapt to the new `router`/`hooks::router` signatures (a `CodeManager`
  without sidecar keeps `/ready` immediate and answers `UNAVAILABLE`).
- `clients/python`: `_models.py`, `_charts.py`, `_code_base.py`,
  `_transport.py` (kernel-gate rule), `sandbox_sync/{main,code}.py`,
  `sandbox_async/{main,code}.py`, `__init__.py`, unit fake `fake_code.py`,
  e2e `test_m4_code.py`; version `0.0.4`. `create()`/`connect()` now need
  an M4 image (`kernel_ready`).
- `image/Dockerfile`, `scripts/image_zip.py` (exclusions), `Makefile`
  (`image-zip` copies `kernel-sidecar/`, `test-sidecar`, `lint` covers the
  sidecar), `scripts/hooks-sim.py` (`/ready` retry budget covers the
  300 s escape), `.github/workflows/ci.yml` (sidecar job).
- Docs: `ARCHITECTURE.md` (`CodeService` row, sidecar section, adapter and
  domain tables), `MILESTONES.md` M4 acceptance state, `SECURITY.md` (T5
  M4 ✔, new row for the uid-1000 sidecar/kernel posture), `AWS_API_NOTES.md`
  §16 measurements (snapshot sizes with the scientific stack, `run-microvm`
  → `kernel_ready`, default-kernel restart cost, `Execute` through the
  proxy with 5 s keepalives), `README.md`.
- Governed by `AWS_API_NOTES.md`: everything in memory at `/ready` is
  cloned into every MicroVM and every resume (§15, Q25), `/validate` runs
  on a throwaway VM (§8), `CLOCK_MONOTONIC` advances during suspend (Q19,
  M5 rearms), a silent stream needs keepalives to cross the proxy and count
  as traffic (Q15, Q31). M4 adds no control-plane call and no new AWS
  parameter.

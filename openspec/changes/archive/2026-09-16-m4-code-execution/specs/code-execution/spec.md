## ADDED Requirements

### Requirement: The kernel sidecar is a child of rayd speaking JSON lines over stdio
`rayd` SHALL spawn the `rayito_kernel_sidecar` Python process as its child with the sandbox's default user identity (uid 1000 unless `rayd` is not root), a session of its own, the M2 resource limits, an environment built from scratch (identity variables plus `PYTHONPATH`, `JUPYTER_PATH`, `PYTHONUNBUFFERED=1`, `PYTHONDONTWRITEBYTECODE=1`, `LANG`/`LC_ALL=C.UTF-8`) and `/home/user` as working directory. Requests SHALL travel as one JSON object per line on the sidecar's stdin (`{"id", "op", …}` with `op` in `ping`, `create_context`, `execute`, `interrupt`, `destroy_context`, `restart_context`, `list_contexts`, `reseed`, `quiesce`, `resume`) and events as one JSON object per line on its stdout (`{"event", …}` with `event` in `ready`, `reply`, `started`, `stdout`, `stderr`, `result`, `error`, `end`, `kernel_died`). Protocol version SHALL be `1`; unknown fields SHALL be ignored; an unknown event, a malformed line or a line longer than 16 MiB SHALL be treated by `rayd` as a fatal sidecar fault. The sidecar's stderr SHALL carry only its JSON log lines and SHALL never be echoed verbatim by `rayd`.

#### Scenario: golden lines agree between codecs
- **WHEN** the Rust `decode_event` and the Python `encode_event` are run over every line of `kernel-sidecar/tests/fixtures/protocol_v1.jsonl`
- **THEN** every line round-trips byte for byte and every unknown `event` value yields a protocol error

#### Scenario: sidecar runs as the sandbox user
- **WHEN** the e2e runs `ps -o user= -C python3 | sort -u` inside a sandbox created from the M4 image
- **THEN** the only user listed is `user`

### Requirement: One ipykernel per context over ipc under /run/rayito/k
Each context SHALL be backed by exactly one `ipykernel` started through `jupyter_client.AsyncKernelManager` with `transport="ipc"`, its sockets and connection file under `/run/rayito/k/<context_id>/` (directory mode `0700`, created by the sidecar; `/run/rayito/k` prepared by `rayd` at every boot as `0700` owned by the sandbox user), the kernelspec `rayito` (`python3 -m ipykernel_launcher -f {connection_file} --config=<sidecar>/ipython/ipython_kernel_config.py`), and an environment equal to the sidecar's plus `JUPYTER_RUNTIME_DIR`, `JUPYTER_DATA_DIR`, `MPLCONFIGDIR`, `OPENBLAS_NUM_THREADS=1`, `OMP_NUM_THREADS=1`, `MKL_NUM_THREADS=1` and the context's `envs` (last wins). `MPLBACKEND` SHALL NOT be set. A context SHALL serialise executions with one `asyncio.Lock` (FIFO). At most 8 contexts SHALL be live per sandbox, the default one included.

#### Scenario: ninth context refused
- **WHEN** eight contexts are live and `CreateContext` is called again
- **THEN** the RPC fails with `RESOURCE_EXHAUSTED` and the SDK raises `RateLimitException`

#### Scenario: context cwd and envs
- **WHEN** the SDK calls `create_code_context(cwd="/tmp", envs={"M4_ENV": "1"})` and runs `import os; os.getcwd()` and `os.environ['M4_ENV']` in it
- **THEN** the results' `text` are `'/tmp'` and `'1'`

### Requirement: Kernel configuration and startup scripts
Every kernel SHALL load `ipython_kernel_config.py` with `InteractiveShell.colors = "NoColor"`, `PlainTextFormatter.max_seq_length = 0`, `HistoryManager.enabled = False` and `InteractiveShellApp.exec_files` naming, in order, `0001_charts.py` (formatter for the `e2b/chart` mime type on `matplotlib.figure.Figure` through the vendored `e2b_charts`, MIT license kept), `0002_data.py` (formatter for `e2b/data` on `pandas.DataFrame`/`Series` as `to_dict(orient="list")`), `0003_images.py` (`_repr_png_`/`_repr_jpeg_` for `PIL.Image.Image` when missing) and `0004_warmup.py` (imports `numpy`, `pandas`, `matplotlib.pyplot`, `scipy.stats`, `sklearn.linear_model`, renders one PNG, runs the chart extractor once and leaves the user namespace clean). A figure the chart extractor cannot parse SHALL still yield its `image/png`.

#### Scenario: warm-up leaves no names behind
- **WHEN** a fresh kernel executes `sorted(k for k in dir() if not k.startswith('_'))`
- **THEN** the result contains none of `np`, `pd`, `plt`, `numpy`, `pandas`, `matplotlib`, `scipy`, `sklearn`

#### Scenario: chart and data mime types are produced in the kernel
- **WHEN** a kernel executes `import matplotlib.pyplot as plt; plt.plot([1, 2, 3]); plt.show()` and then `import pandas as pd; pd.DataFrame({'a': [1, 2]})`
- **THEN** the first cell's `display_data` bundle contains `image/png` and `e2b/chart`, and the second cell's `execute_result` bundle contains `text/plain`, `text/html` and `e2b/data`

### Requirement: Executions map Jupyter messages the E2B way
For an `execute` op the sidecar SHALL run the code with `store_history=True`, `allow_stdin=False`, keep only iopub and shell messages whose parent is that request, and emit: `started{execution_count}` for `execute_input`; `stdout`/`stderr{text, timestamp_unix_ns}` for `stream` with text split into chunks of at most 64 KiB on UTF-8 boundaries; `result{is_main_result, mime}` for `display_data` (`is_main_result=false`) and `execute_result` (`true`) where `mime` maps every entry of the bundle to a string (`str` values verbatim, everything else `json.dumps`, a value above 8 MiB omitted and noted under `rayito/omitted`, then the largest remaining values omitted the same way while the bundle exceeds 12 MiB in total, and any encoded event line above 15 MiB replaced by its `rayito/omitted` note so `rayd`'s 16 MiB line limit is never hit); `error{name, value, traceback}` for `error` with ANSI sequences stripped; `error{ExecutionAborted}` for an `execute_reply` with status `abort`/`aborted`; and exactly one `end{execution_count}` once both the `execute_reply` and the `idle` status have been seen, with `execution_count` from the reply. Per-execution `envs` SHALL be applied in the kernel before the cell with a silent execution and restored after it. `clear_output` SHALL be ignored and `update_display_data` treated as `display_data`.

#### Scenario: execute_result versus stream
- **WHEN** the SDK runs `x = 42`, then `x`, then `print(x)`
- **THEN** the first execution has no results, the second has `text == "42"` with `is_main_result` true, and the third has `"42"` in `"".join(logs.stdout)` and `text is None`

#### Scenario: structured error
- **WHEN** the SDK runs `1/0`
- **THEN** `error.name == "ZeroDivisionError"`, `error.value` contains `division by zero`, `error.traceback` contains `ZeroDivisionError` and no `\x1b[`, and `execution_count` is set

#### Scenario: per-execution envs restored
- **WHEN** the SDK runs `import os; os.environ.get('M4_RUN', 'unset')` with `envs={"M4_RUN": "yes"}` and then the same code without `envs`
- **THEN** the results' `text` are `'yes'` and `'unset'`

### Requirement: Execute is a server-stream with started, output, results, error, end and keepalives
`CodeService.Execute` SHALL answer with a stream where the first counted message is `started{execution_id, execution_count}`, followed by any number of `stdout`, `stderr`, `result` and at most one `error`, and finally exactly one `end`; `error` SHALL never be the last message. `seq` SHALL number the counted messages `1, 2, 3…` per execution and SHALL be `0` on `keepalive`. `rayd` SHALL emit `keepalive` after every 5 s without a message. `execution_id` SHALL be `exec-<16 hex>` generated by `rayd` after `/run` from the operating system's random source. An empty `context_id` SHALL select the default context; an unknown one SHALL fail with `NOT_FOUND` before any message; code above 1 MiB SHALL fail with `INVALID_ARGUMENT`. `ExecutionResult` fields SHALL be filled from the mime bundle as `text/plain → text`, `text/html → html`, `text/markdown → markdown`, `text/latex → latex`, `application/json → json`, `application/javascript → javascript`, `image/png → png`, `image/jpeg → jpeg`, `image/svg+xml → svg`, `application/pdf → pdf`, `e2b/chart → chart`, `e2b/data → data`, everything else into `extra`.

#### Scenario: event order and sequence numbers
- **WHEN** an integration test executes a scripted cell that prints once and returns a value
- **THEN** the stream is `started(seq 1)`, `stdout(seq 2)`, `result(seq 3, is_main_result)`, `end(seq 4)`, and any `keepalive` in between carries `seq 0`

#### Scenario: silent cell through the proxy
- **WHEN** the SDK runs `import time; time.sleep(12); 'done'` with `timeout=60` against a real MicroVM
- **THEN** the execution completes with `text == "'done'"` and the stream was never cut

#### Scenario: matplotlib result through the proxy
- **WHEN** the SDK runs `import matplotlib.pyplot as plt; plt.plot([1, 2, 3]); plt.show()` against a real MicroVM
- **THEN** `results[0].png` decodes to bytes starting with the PNG signature, `results[0].chart` is a `LineChart` with three points, and `results[0].is_main_result` is false

### Requirement: Server-enforced execution timeout interrupts, then restarts
When `timeout_ms > 0`, `rayd` SHALL start the clock when it accepts the request and, at the deadline, send `interrupt` for that execution; from then on kernel `error` events for it SHALL be dropped and the `end` SHALL be preceded by exactly one `error{name: "ExecutionTimeout", value: "execution exceeded <timeout_ms> ms", traceback: []}`. If no `end` arrives within 5 s of the interrupt, `rayd` SHALL send `restart_context` for that context; the timed-out execution SHALL still end with `ExecutionTimeout` and any other in-flight execution of that context with `error{KernelRestarted}` followed by `end{execution_count: 0}`. A context interrupted in time SHALL keep its state. `timeout_ms = 0` SHALL mean no server limit; values above 28 800 000 SHALL be clamped.

#### Scenario: sleep interrupted, state intact
- **WHEN** the SDK runs `import time; time.sleep(10)` with `timeout=2` after `x = 42` in the same context
- **THEN** within 8 s the execution ends with `error.name == "ExecutionTimeout"` and `"2000"` in `error.value`, and `run_code("x").text == "42"` afterwards

#### Scenario: hung cell restarted
- **WHEN** an integration test executes the fake sidecar's `hang 5` with `timeout_ms=500`
- **THEN** the fake receives `interrupt` and then `restart_context`, and the stream ends with `ExecutionTimeout` followed by `end{execution_count: 0}`

### Requirement: Cancelling the stream interrupts the execution
When the client drops or cancels an `Execute` stream before its `end`, `rayd` SHALL send `interrupt{context_id, execution_id}` to the sidecar and discard every later event of that execution; a queued execution that is interrupted SHALL be removed from the queue.

#### Scenario: client drops mid-execution
- **WHEN** an integration test opens `Execute` on `sleep 5` and drops the stream after `started`
- **THEN** the fake sidecar receives `interrupt` for that execution within 1 s

### Requirement: Output backpressure and truncation
Each `Execute` stream SHALL buffer at most 256 sidecar events ahead of the client; when the buffer is full the dispatcher SHALL wait up to 30 s and, if the client still has not drained, SHALL end that stream with `error{OutputTruncated}` and `end{execution_count: 0}` while the execution continues in the kernel and its later events are dropped. The sidecar SHALL bound the inbox between a kernel's ZMQ channels and its running cell (64 channel messages; the `died`/`abort` sentinels never wait) so a stalled `emit` stops draining the sockets instead of growing the sidecar's heap. Sidecar op timeouts that expire while the dispatcher is parked on a stalled client SHALL NOT count towards the consecutive-timeouts kill switch. An `Execute` stream SHALL consume the events already queued for it before it consults its timeout timers, so an `end` that arrived inside the timeout is delivered unchanged however late the client reads it.

#### Scenario: stalled client
- **WHEN** an integration test executes `big 2000` (2000 chunks of 64 KiB) with `stall_timeout` 1 s and a client that stops reading after `started`
- **THEN** the stream ends with `OutputTruncated` followed by `end{0}` and the fake sidecar keeps running

#### Scenario: op timeouts during a stall do not relaunch the sidecar
- **WHEN** an integration test stalls a `big 400` client and three `CreateContext` calls with a 200 ms op timeout expire while the dispatcher is parked
- **THEN** each call answers `UNAVAILABLE`, `sidecar_restarts` stays 0 and the next execution succeeds

#### Scenario: a queued end is not rewritten by a late read
- **WHEN** a unit test queues `started` and `end` for an execution with a 100 ms timeout and polls the stream only after the restart grace has elapsed
- **THEN** the stream yields `started` and `end` without `ExecutionTimeout` and sends neither `interrupt` nor `restart_context`

#### Scenario: a blocked emit keeps the sidecar inbox bounded
- **WHEN** a unit test pumps 100 `stream` messages into a context whose `emit` blocks after `started`
- **THEN** the inbox depth never exceeds its capacity and every chunk is emitted once the client resumes

### Requirement: Contexts can be created, listed, restarted and destroyed
`CreateContext` SHALL accept `language` `""` or `"python"` (else `INVALID_ARGUMENT`), resolve `cwd` like `ProcessService` (absolute, default the user's home or the `/run` payload `workdir`), start the kernel and return a `context_id` of the form `ctx-<12 hex>`. `ListContexts` SHALL list the default context first, then the others in creation order, each with `language == "python"` and its `cwd`. `RestartContext` SHALL keep the id, `cwd` and `envs`, shut the kernel down and start a new kernel with a new connection file (new HMAC key), ending any in-flight execution with `error{KernelRestarted}` + `end{0}`. `DestroyContext` SHALL end in-flight executions with `error{ContextDestroyed}` + `end{0}`, shut the kernel down and remove its socket directory; the default context SHALL be refused with `FAILED_PRECONDITION`. Unknown ids SHALL fail with `NOT_FOUND`.

#### Scenario: isolation between contexts
- **WHEN** the SDK runs `x = 42` in the default context and `x` in a freshly created context
- **THEN** the second execution's `error.name` is `NameError`

#### Scenario: restart loses state and keeps the id
- **WHEN** the SDK runs `y = 7` in a context, calls `restart_code_context(ctx)` and runs `y` again in it
- **THEN** the last execution's `error.name` is `NameError` and `ctx.id` still appears in `list_code_contexts()`

#### Scenario: default context protected
- **WHEN** the SDK calls `remove_code_context("default")`
- **THEN** `InvalidArgumentException` is raised and the default context still serves `run_code`

### Requirement: Kernel death converges back to a live kernel
When a kernel process exits outside `destroy`/`restart`, the sidecar SHALL end a running execution with `error{KernelDied}` + `end{0}`, emit `kernel_died{context_id, exit_code}` and restart that context's kernel with a fresh connection file. When the sidecar process itself exits, `rayd` SHALL resolve pending replies as unavailable, end every live execution with `error{KernelDied}` + `end{0}`, `SIGKILL` the process groups of every known kernel, forget every user context (their ids answer `NOT_FOUND`), report `kernel_ready=false`, and relaunch the sidecar after a backoff of `min(0.5 s × 2^(attempt−1), 30 s)` without an attempt cap; once the new sidecar reports `ready` and, if `/run` already happened, the default kernel has been restarted with the payload `envs`, `kernel_ready` SHALL be true again.

#### Scenario: kernel exits inside a cell
- **WHEN** the SDK runs `import os; os._exit(3)` and then `1+1`
- **THEN** the first execution's `error.name` is `KernelDied` and the second returns `text == "2"` within 15 s

#### Scenario: sidecar exits
- **WHEN** an integration test triggers the fake sidecar's `die` while a context created by the test is live
- **THEN** `Health.kernel_ready` becomes false, the fake is relaunched after the backoff, `Execute` on the test's context fails with `NOT_FOUND`, and `Execute` on the default context works again

### Requirement: Health.kernel_ready reflects the default kernel
`HealthResponse.kernel_ready` SHALL be true only while the sidecar is running, has reported `ready`, the default context is not being restarted and no relaunch is in progress; it SHALL be false during boot, during the `/run` rotation, after a sidecar exit and with `--no-sidecar`. `kernel_state_lost` SHALL stay false in M4. The SDK's `create()` and `connect()` SHALL wait for `agent_ready and kernel_ready` before returning; `is_running()` SHALL keep meaning `agent_ready`.

#### Scenario: ready right after create
- **WHEN** `Sandbox.create()` returns against the M4 image
- **THEN** a `Health` call reports `kernel_ready == True` and the elapsed time from `run-microvm` is at most 15 s (logged)

#### Scenario: not ready while rotating
- **WHEN** an integration test observes `Health` right after `/run` was acknowledged and before the fake sidecar answered `restart_context`
- **THEN** `kernel_ready` is false and becomes true after the reply

### Requirement: /ready waits for the warm default kernel with a 300 s escape
`POST /ready` SHALL answer `503` immediately, without changing phase, while the default kernel is not warm; `200` with the `Booting → Ready` transition once it is; and, 300 s after boot, `200` regardless with `outcome: "ready_escape"` and an error log. With `--no-sidecar` it SHALL answer `200` immediately as before.

#### Scenario: warming then ready
- **WHEN** an integration test starts `rayd` with a fake sidecar delaying `ready` by 1 s and posts `/ready` twice 1.5 s apart
- **THEN** the first answer is `503` with `outcome: "kernel_warming"` and the second is `200`

#### Scenario: no sidecar configured
- **WHEN** `rayd` runs with `--no-sidecar` and `/ready` is posted
- **THEN** it answers `200` at once and `Health.kernel_ready` is false

### Requirement: /validate runs a real pandas and matplotlib cell
The first `POST /validate` SHALL start, in the default context with a 60 s timeout, a cell that builds a `pandas.DataFrame`, calls `describe()`, plots it with `matplotlib` and returns the frame, and SHALL answer `503` with `outcome: "validating"`; later calls SHALL answer `503` while it runs and `200` once it finished, with `outcome: "validated"` or `"validate_failed"` (logged, the build is not failed).

#### Scenario: validate completes
- **WHEN** an integration test posts `/validate` repeatedly every 200 ms against a fake sidecar
- **THEN** the answers are `503` until the fake's `end` arrives and `200` with `outcome: "validated"` afterwards

### Requirement: /run rotates the default kernel and /resume reseeds
On the first accepted `/run` of a boot `rayd` SHALL, after answering `200`, send `restart_context{"default", envs: <payload envs>}` so the sandbox gets a kernel with a new connection file (new HMAC key), fresh PRNG state and the payload's environment; `kernel_ready` SHALL be false until the reply arrives and the duration SHALL be logged as `restart_ms`. On `/resume` `rayd` SHALL send `reseed`, and the sidecar SHALL run `random.seed()` and, when `numpy` is loaded, `numpy.random.seed()` silently in every live kernel, answering with the reseeded and failed context ids. Nothing random SHALL be generated by `rayd` or the sidecar before `/run` other than what the kernel processes create at start.

#### Scenario: two sandboxes differ
- **WHEN** two sandboxes are created from the same image version and each runs `import random; [random.random() for _ in range(3)]` and `import numpy; numpy.random.default_rng().random(3).tolist()`
- **THEN** the `text` results differ between the sandboxes for both cells

#### Scenario: restart request carries the payload envs
- **WHEN** an integration test posts `/run` with a payload whose `envs` is `{"M4": "1"}`
- **THEN** the fake sidecar receives `restart_context` for `default` with `envs == {"M4": "1"}` and `Health.kernel_ready` returns to true after its reply

#### Scenario: reseed changes the sequence
- **WHEN** the sidecar's real-kernel test reads `random.random()`, sends `reseed` and reads it again in the same kernel
- **THEN** the two values differ and the reply lists that context as reseeded

### Requirement: Unary status mapping and Reattach
`CreateContext`, `ListContexts`, `DestroyContext` and `RestartContext` SHALL use gRPC statuses: `INVALID_ARGUMENT` (language, cwd, id syntax, envs), `NOT_FOUND` (context), `FAILED_PRECONDITION` (default context destroy), `RESOURCE_EXHAUSTED` (8 contexts), `UNAVAILABLE` with a message starting with `kernel not ready` (no sidecar, relaunching, context not ready within 30 s, sidecar op timeout) or with the phase name (suspending/terminating), `ABORTED` (concurrent restart/destroy of the same context), `INTERNAL` (protocol fault, message without payloads). `Reattach` SHALL answer `UNIMPLEMENTED` in M4. Error messages SHALL never contain code, output, envs, cwd or tracebacks.

#### Scenario: execute while suspending
- **WHEN** `/suspend` has been acknowledged and `Execute` is called
- **THEN** the RPC fails with `UNAVAILABLE` and the SDK raises `SandboxStateException`

#### Scenario: kernel gate in the SDK
- **WHEN** `rayd` answers `UNAVAILABLE` with details `kernel not ready: no sidecar configured`
- **THEN** the SDK raises `SandboxException` without probing `Health`

### Requirement: Python SDK run_code and code contexts
`Sandbox.run_code(code, *, context=None, on_stdout=None, on_stderr=None, on_result=None, on_error=None, envs=None, timeout=300, request_timeout=None)` SHALL open `Execute` on the unary channel with `timeout_ms = round(timeout × 1000)` (`None`/`0` → no limit and no gRPC deadline, else a gRPC deadline of `timeout + 15 s`), feed every event into an `Execution(results, logs, error, execution_count)` (`keepalive` ignored; `on_stdout`/`on_stderr` receive `OutputMessage(line, timestamp, error)`; `on_result` a `Result`; `on_error` an `ExecutionError`) and return it; `Execution.text` SHALL be the `text` of the result with `is_main_result`, else `None`; kernel-level errors SHALL be data in `Execution.error`, never raised. `Result` SHALL expose `text, html, markdown, svg, png, jpeg, pdf, latex, json, javascript, data, chart, is_main_result, extra`, `formats()`, `__str__` and `_repr_*_`; `json` and `data` SHALL be parsed JSON (raw string kept if parsing fails); `chart` SHALL be parsed into `LineChart`, `ScatterChart`, `BarChart`, `PieChart`, `BoxAndWhiskerChart`, `SuperChart` or a `Chart` of type `UNKNOWN`. `ExecutionError.traceback` SHALL be the lines joined with `"\n"`. `create_code_context(*, cwd=None, language=None, envs=None, request_timeout=None) -> CodeContext(id, language, cwd)`, `list_code_contexts() -> list[CodeContext]`, `remove_code_context(context)` and `restart_code_context(context)` SHALL accept a `CodeContext` or its id, use 90 s default deadlines for create/restart, and map `NOT_FOUND` to `NotFoundException` and `FAILED_PRECONDITION` to `InvalidArgumentException`. `AsyncSandbox` SHALL expose the same surface as coroutines.

#### Scenario: acceptance sequence
- **WHEN** the SDK runs `x = 42`, then `x`, then `print(x)`, then a matplotlib plot, then `1/0`, then `time.sleep(10)` with `timeout=2`
- **THEN** `run_code("x").text == "42"`, `"42" in "".join(run_code("print(x)").logs.stdout)`, the plot's `results[0].png` and `chart` are not `None`, `error.name == "ZeroDivisionError"` with `execution_count` set, and the last `error.name == "ExecutionTimeout"`

#### Scenario: callbacks receive typed messages
- **WHEN** the SDK runs `print('a'); print('b')` with `on_stdout=seen.append`
- **THEN** every element of `seen` is an `OutputMessage` with `error is False` and a positive `timestamp`, and their `line`s joined contain `a` and `b`

#### Scenario: async parity
- **WHEN** `AsyncSandbox.connect(...)` runs `x` in the default context, creates a context, runs `2*2` in it, removes it and runs `1/0`
- **THEN** the results are `"42"`, `"4"`, no exception on removal, and `error.name == "ZeroDivisionError"`

### Requirement: Image ships the kernel stack and a warm snapshot
The `rayito-base` image SHALL install the exact pins of `kernel-sidecar/requirements.txt` (`ipykernel==6.31.0`, `ipython==9.15.0`, `jupyter_client==8.10.0`, `pyzmq==27.2.0`, `matplotlib==3.10.9`, `pandas==2.2.3`, `numpy==2.3.5`, plus the resolved exact `scipy` and `scikit-learn`) for Python 3.12 from wheels, copy the sidecar to `/opt/rayito/sidecar` (readable by all, `tests`/caches excluded), build the matplotlib font cache and the IPython profile as `user` at build time, and take the snapshot only after `/ready` reported a warm default kernel. `image-publish` SHALL record `snapshotBuild` sizes and build time; a memory snapshot above 1.2 GB SHALL trigger trimming the warm-up import list from the end (`sklearn` first, then `scipy`).

#### Scenario: font cache present in the snapshot
- **WHEN** the SDK runs `import matplotlib, glob, os; sorted(os.path.basename(p) for p in glob.glob(os.path.join(matplotlib.get_cachedir(), 'fontlist-*.json')))` in a sandbox
- **THEN** the result lists at least one `fontlist-*.json`

#### Scenario: sizes reported
- **WHEN** `make image-publish` finishes the M4 version
- **THEN** the log prints `memorySnapshotSizeInBytes`, `codeInstallSizeInBytes`, `diskSnapshotSizeInBytes` and the build seconds, and the task notes record them next to version 4.0's

### Requirement: Logging never exposes sandbox content
`rayd` and the sidecar SHALL log only allowlisted fields (`context_id`, `execution_id`, `execution_count`, `events`, `results`, mime type names, `timeout_ms`, `outcome`, `kernel_pid`, `attempt`, `backoff_ms`, `exit_code`, `warmup_ms`, `restart_ms`, `sidecar_restarts`, `sidecar_stderr_lines`, `duration_ms`, `bytes`, `chunks`, `contexts`, counts) and SHALL never log code, stdout/stderr text, mime payloads, `envs`, `cwd`, error values, traceback lines, connection files or the sidecar's raw stderr.

#### Scenario: stderr lines that are not the log schema
- **WHEN** the fake sidecar writes a plain-text line to stderr
- **THEN** `rayd` increments `sidecar_stderr_lines` and the line's text never appears in `rayd`'s output

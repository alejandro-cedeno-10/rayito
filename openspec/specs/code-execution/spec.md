# code-execution Specification

## Purpose
TBD - created by archiving change m4-code-execution. Update Purpose after archive.

## Requirements

### Requirement: The kernel sidecar is a child of rayd speaking JSON lines over stdio
`rayd` SHALL spawn the `rayito_kernel_sidecar` Python process as its child with the sandbox's default user identity (uid 1000 unless `rayd` is not root), a session of its own, the M2 resource limits, an environment built from scratch (identity variables plus `PYTHONPATH`, `JUPYTER_PATH`, `PYTHONUNBUFFERED=1`, `PYTHONDONTWRITEBYTECODE=1`, `LANG`/`LC_ALL=C.UTF-8`) and `/home/user` as working directory. Requests SHALL travel as one JSON object per line on the sidecar's stdin (`{"id", "op", …}` with `op` in `ping`, `create_context`, `execute`, `interrupt`, `destroy_context`, `restart_context`, `list_contexts`, `reseed`, `quiesce`, `resume`) and events as one JSON object per line on its stdout (`{"event", …}` with `event` in `ready`, `reply`, `started`, `stdout`, `stderr`, `result`, `error`, `end`, `kernel_died`). `create_context` SHALL carry `language` after `context_id` (field order `context_id, language, cwd, envs`), `ready` SHALL carry `languages` after `warmup_ms`, and the `reseed` reply payload SHALL carry `skipped`; a codec that does not know these fields SHALL ignore them and `rayd` SHALL treat an absent `languages` as `["python"]`. Protocol version SHALL be `1`; unknown fields SHALL be ignored; an unknown event, a malformed line or a line longer than 16 MiB SHALL be treated by `rayd` as a fatal sidecar fault. The sidecar's stderr SHALL carry only its JSON log lines and SHALL never be echoed verbatim by `rayd`.

#### Scenario: golden lines agree between codecs
- **WHEN** the Rust `decode_event` and the Python `encode_event` are run over every line of `kernel-sidecar/tests/fixtures/protocol_v1.jsonl`, including the `create_context` line with `"language":"python"`, a `create_context` with `"language":"bash"`, a `ready` with `"languages":["bash","javascript","python"]` and a `reseed` reply with `skipped`
- **THEN** every line round-trips byte for byte and every unknown `event` value yields a protocol error

#### Scenario: sidecar runs as the sandbox user
- **WHEN** the e2e runs `ps -o user= -C python3 | sort -u` inside a sandbox created from the M4 image
- **THEN** the only user listed is `user`

#### Scenario: older sidecar without languages
- **WHEN** the fake sidecar answers `ready` without a `languages` field
- **THEN** `rayd` accepts `python` and answers `UNIMPLEMENTED` to `Execute{language: "bash"}`

### Requirement: One ipykernel per context over ipc under /run/rayito/k
Each context SHALL be backed by exactly one Jupyter kernel started through `jupyter_client.AsyncKernelManager` with the transport of its language (`ipc` for `python` and `bash`; loopback TCP on `127.0.0.1` with ports chosen by `jupyter_client` for the Deno kernels of `javascript` and `typescript`, which cannot bind `ipc` endpoints, ADR-013), its connection file (holding the HMAC key) and, for `ipc`, its sockets under `/run/rayito/k/<context_id>/` (directory mode `0700`, created by the sidecar; `/run/rayito/k` prepared by `rayd` at every boot as `0700` owned by the sandbox user), the kernelspec of the context's language (`rayito` for `python`, an `ipykernel` with `python3 -m ipykernel_launcher -f {connection_file} --config=<sidecar>/ipython/ipython_kernel_config.py`; `rayito-bash`, `rayito-javascript` and `rayito-typescript` per the language catalog), and an environment equal to the sidecar's plus `JUPYTER_RUNTIME_DIR`, `JUPYTER_DATA_DIR`, `MPLCONFIGDIR`, `OPENBLAS_NUM_THREADS=1`, `OMP_NUM_THREADS=1`, `MKL_NUM_THREADS=1` and the context's `envs` (last wins), for every language, with the kernelspec's own `env` applied on top by `jupyter_client` (for the Deno kernelspecs `NO_COLOR`, `DENO_DIR`, `DENO_NO_UPDATE_CHECK`). `MPLBACKEND` SHALL NOT be set. A context SHALL serialise executions with one `asyncio.Lock` (FIFO). At most 8 contexts SHALL be live per sandbox, the default one and the lazily created per-language defaults included.

#### Scenario: ninth context refused
- **WHEN** eight contexts are live and `CreateContext` is called again
- **THEN** the RPC fails with `RESOURCE_EXHAUSTED` and the SDK raises `RateLimitException`

#### Scenario: context cwd and envs
- **WHEN** the SDK calls `create_code_context(cwd="/tmp", envs={"M4_ENV": "1"})` and runs `import os; os.getcwd()` and `os.environ['M4_ENV']` in it
- **THEN** the results' `text` are `'/tmp'` and `'1'`

#### Scenario: context envs reach a bash kernel
- **WHEN** the e2e calls `ctx = create_code_context(language="bash", envs={"M7_CTX": "1"})` and runs `echo $M7_CTX` with `context=ctx`
- **THEN** `1` is in the stdout logs and `ctx.language == "bash"`

#### Scenario: Deno contexts use loopback TCP, the others ipc
- **WHEN** the unit test calls `kernel_endpoint` with the `python`, `bash`, `javascript` and `typescript` catalog entries and a socket directory `d`
- **THEN** it returns `("ipc", "<d>/k")` for `python` and `bash` and `("tcp", "127.0.0.1")` for `javascript` and `typescript`

#### Scenario: context envs reach a TypeScript kernel
- **WHEN** the e2e calls `ctx = create_code_context(language="typescript", envs={"K": "1"})` on a `rayito-base-poly` sandbox and runs `Deno.env.get('K') === '1'` with `context=ctx`
- **THEN** the result's `text` is `true` and `ctx.language == "typescript"`

### Requirement: Kernel configuration and startup scripts
Every Python kernel SHALL load `ipython_kernel_config.py` with `InteractiveShell.colors = "NoColor"`, `PlainTextFormatter.max_seq_length = 0`, `HistoryManager.enabled = False` and `InteractiveShellApp.exec_files` naming, in order, `0001_charts.py` (formatter for the `e2b/chart` mime type on `matplotlib.figure.Figure` through the vendored `e2b_charts`, MIT license kept), `0002_data.py` (formatter for `e2b/data` on `pandas.DataFrame`/`Series` as `to_dict(orient="list")`), `0003_images.py` (`_repr_png_`/`_repr_jpeg_` for `PIL.Image.Image` when missing) and `0004_warmup.py` (imports `numpy`, `pandas`, `matplotlib.pyplot`, `scipy.stats`, `sklearn.linear_model`, renders one PNG, runs the chart extractor once and leaves the user namespace clean). A figure the chart extractor cannot parse SHALL still yield its `image/png`. Kernels of other languages SHALL load none of this.

#### Scenario: warm-up leaves no names behind
- **WHEN** a fresh kernel executes `sorted(k for k in dir() if not k.startswith('_'))`
- **THEN** the result contains none of `np`, `pd`, `plt`, `numpy`, `pandas`, `matplotlib`, `scipy`, `sklearn`

#### Scenario: chart and data mime types are produced in the kernel
- **WHEN** a kernel executes `import matplotlib.pyplot as plt; plt.plot([1, 2, 3]); plt.show()` and then `import pandas as pd; pd.DataFrame({'a': [1, 2]})`
- **THEN** the first cell's `display_data` bundle contains `image/png` and `e2b/chart`, and the second cell's `execute_result` bundle contains `text/plain`, `text/html` and `e2b/data`

#### Scenario: a bash kernel starts without the IPython config
- **WHEN** the kernel test (Linux, `bash_kernel` importable) starts a `bash` context
- **THEN** the kernelspec argv written for it contains `bash_kernel` and no `--config`, and `echo 1` yields a `stdout` event with `1`

### Requirement: Executions map Jupyter messages the E2B way
For an `execute` op the sidecar SHALL run the code with `store_history=True`, `allow_stdin=False`, keep only iopub and shell messages whose parent is that request, and emit: `started{execution_count}` for `execute_input`; `stdout`/`stderr{text, timestamp_unix_ns}` for `stream` with text split into chunks of at most 64 KiB on UTF-8 boundaries; `result{is_main_result, mime}` for `display_data` (`is_main_result=false`) and `execute_result` (`true`) where `mime` maps every entry of the bundle to a string (`str` values verbatim, everything else `json.dumps`, a value above 8 MiB omitted and noted under `rayito/omitted`, then the largest remaining values omitted the same way while the bundle exceeds 12 MiB in total, and any encoded event line above 15 MiB replaced by its `rayito/omitted` note so `rayd`'s 16 MiB line limit is never hit); `error{name, value, traceback}` for `error` with ANSI sequences stripped; `error{ExecutionAborted}` for an `execute_reply` with status `abort`/`aborted`; and exactly one `end{execution_count}` once both the `execute_reply` and the `idle` status have been seen, with `execution_count` from the reply. This mapping SHALL be the same for every kernel language, except that for the Deno languages (`javascript`, `typescript`) ANSI escape sequences SHALL also be stripped from the `text/plain` entry of every `result` bundle, as defence in depth next to the kernelspec's `NO_COLOR=1`; other bundle entries and stream text SHALL be untouched for every language. Per-execution `envs` SHALL be applied in a Python kernel before the cell with a silent execution and restored after it; for any other language they SHALL have been refused by `rayd` (`INVALID_ARGUMENT`) and the sidecar SHALL never receive them. `clear_output` SHALL be ignored and `update_display_data` treated as `display_data`.

#### Scenario: execute_result versus stream
- **WHEN** the SDK runs `x = 42`, then `x`, then `print(x)`
- **THEN** the first execution has no results, the second has `text == "42"` with `is_main_result` true, and the third has `"42"` in `"".join(logs.stdout)` and `text is None`

#### Scenario: structured error
- **WHEN** the SDK runs `1/0`
- **THEN** `error.name == "ZeroDivisionError"`, `error.value` contains `division by zero`, `error.traceback` contains `ZeroDivisionError` and no `\x1b[`, and `execution_count` is set

#### Scenario: per-execution envs restored
- **WHEN** the SDK runs `import os; os.environ.get('M4_RUN', 'unset')` with `envs={"M4_RUN": "yes"}` and then the same code without `envs`
- **THEN** the results' `text` are `'yes'` and `'unset'`

#### Scenario: bash stdout maps like python
- **WHEN** the e2e runs `echo hi` with `language="bash"`
- **THEN** the execution has `hi` in `"".join(logs.stdout)`, `error is None` and `execution_count` set

#### Scenario: Deno plain text is stripped, other kernels untouched
- **WHEN** the unit test feeds an `execute_result` whose bundle is `{"text/plain": "\x1b[33m42\x1b[39m"}` through `run_execution` once with `strip_plain_text_ansi=True` and once with the default
- **THEN** the first `result.mime["text/plain"]` is `42` and the second keeps the escape sequences

#### Scenario: TypeScript outputs map like python
- **WHEN** the e2e runs, with `language="typescript"` on a `rayito-base-poly` sandbox, `const x: number = 40 + 2; x`, `console.log('out')`, `console.error('err')`, ``Deno.jupyter.html`<b>hi</b>` ``, `throw new Error('boom')` and `await new Promise((r) => setTimeout(() => r(7), 200))`
- **THEN** the first `text == "42"` without `\x1b`, `out` is in the stdout logs, `err` is in the stderr logs, `results[0].html == "<b>hi</b>"`, `error.name == "Error"` with `error.value == "boom"`, and the last `text == "7"`

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
When `timeout_ms > 0`, `rayd` SHALL start the clock when it accepts the request and, at the deadline measured on the running clock (suspended time excluded), send `interrupt` for that execution; from then on kernel `error` events for it SHALL be dropped and the `end` SHALL be preceded by exactly one `error{name: "ExecutionTimeout", value: "execution exceeded <timeout_ms> ms", traceback: []}`. If no `end` arrives within 5 s of running time after the interrupt, `rayd` SHALL send `restart_context` for that context; the timed-out execution SHALL still end with `ExecutionTimeout` and any other in-flight execution of that context with `error{KernelRestarted}` followed by `end{execution_count: 0}`. A context interrupted in time SHALL keep its state. `timeout_ms = 0` SHALL mean no server limit; values above 28 800 000 SHALL be clamped. The deadlines SHALL be owned by the execution recorder, not by the `Execute` stream, so they keep running after the stream is closed.

#### Scenario: sleep interrupted, state intact
- **WHEN** the SDK runs `import time; time.sleep(10)` with `timeout=2` after `x = 42` in the same context
- **THEN** within 8 s the execution ends with `error.name == "ExecutionTimeout"` and `"2000"` in `error.value`, and `run_code("x").text == "42"` afterwards

#### Scenario: hung cell restarted
- **WHEN** an integration test executes the fake sidecar's `hang 5` with `timeout_ms=500`
- **THEN** the fake receives `interrupt` and then `restart_context`, and the stream ends with `ExecutionTimeout` followed by `end{execution_count: 0}`

#### Scenario: execution deadline survives a simulated pause
- **WHEN** an integration test executes `sleep 5` with `timeout_ms=1500`, posts `/suspend` at 0.5 s, sleeps 2 s and posts `/resume`
- **THEN** the fake receives `interrupt` about 1 s after the `/resume`, not at the `/resume`

### Requirement: Cancelling the stream interrupts the execution
When the client drops or cancels the originating `Execute` stream before its `end`, `rayd` SHALL send `interrupt{context_id, execution_id}` to the sidecar and the execution's later events SHALL be recorded but no longer delivered to that subscriber; a queued execution that is interrupted SHALL be removed from the queue. An `Execute` stream closed by `/suspend` SHALL be detached instead: no `interrupt` is sent and the execution keeps running and being recorded. Dropping a `Reattach` stream SHALL never interrupt.

#### Scenario: client drops mid-execution
- **WHEN** an integration test opens `Execute` on `sleep 5` and drops the stream after `started`
- **THEN** the fake sidecar receives `interrupt` for that execution within 1 s

#### Scenario: suspend does not interrupt
- **WHEN** an `Execute` on `sleep 3` is open and `/suspend` is posted
- **THEN** the stream ends with `UNAVAILABLE suspending`, the fake sidecar receives `quiesce` but no `interrupt`, and after `/resume` a `Reattach` delivers the `end`

### Requirement: Output backpressure and truncation
Each subscriber of an execution (`Execute` or `Reattach`) SHALL buffer at most 256 events ahead of its client; when the buffer is full the recorder SHALL NOT wait: it SHALL detach that subscriber alone, immediately, with `error{OutputTruncated}` and `end{execution_count: 0}` while the execution continues, keeps being recorded in its 4 MiB ring (from which the client may `Reattach` at `last_seq + 1`) and keeps being delivered to the other subscribers. The recorder SHALL never await a subscriber and SHALL always drain the sidecar dispatcher's channel for its execution, so a slow client never parks the recorder, the sidecar's stdout or the ops issued meanwhile. The sidecar SHALL bound the inbox between a kernel's ZMQ channels and its running cell (64 channel messages; the `died`/`abort` sentinels never wait). Sidecar op timeouts that expire while the dispatcher is parked SHALL NOT count towards the consecutive-timeouts kill switch. The recorder SHALL consume the events already queued for an execution before it consults the timeout timers, so an `end` that arrived inside the timeout is delivered unchanged however late a client reads it.

#### Scenario: stalled client
- **WHEN** an integration test executes `big 2000` (2000 chunks of 64 KiB) with a client that stops reading after `started`
- **THEN** the stream ends with `OutputTruncated` followed by `end{0}` once the client has fallen a full queue behind, the fake sidecar keeps running and was never parked, and the ring retains the last 4 MiB of chunks

#### Scenario: a stuck subscriber costs nobody else anything
- **WHEN** a unit test leaves one of two subscribers unread while the fake sidecar emits far more events than a queue holds and a `create_context` is issued meanwhile
- **THEN** the other subscriber receives every event and the `end`, the op is answered, `dispatch_blocked` stays false and no time is spent waiting on the stuck subscriber

#### Scenario: a queued end is not rewritten by a late read
- **WHEN** a unit test queues `started` and `end` for an execution with a 100 ms timeout and polls the subscriber only after the restart grace has elapsed
- **THEN** the stream yields `started` and `end` without `ExecutionTimeout` and sends neither `interrupt` nor `restart_context`

#### Scenario: a blocked emit keeps the sidecar inbox bounded
- **WHEN** a unit test pumps 100 `stream` messages into a context whose `emit` blocks after `started`
- **THEN** the inbox depth never exceeds its capacity and every chunk is emitted once the client resumes

### Requirement: Contexts can be created, listed, restarted and destroyed
`CreateContext` SHALL accept `language` `""`, `"python"`, `"bash"`, `"javascript"` or `"typescript"` (any other value `INVALID_ARGUMENT`; a known language absent from the sidecar's `ready.languages` `UNIMPLEMENTED`), resolve `cwd` like `ProcessService` (absolute, default the user's home or the `/run` payload `workdir`), start the kernel of that language and return a `context_id` of the form `ctx-<12 hex>`. `ListContexts` SHALL list the default context first, then the others in creation order (lazily created per-language defaults included at their creation slot), each with its real `language` (`python`, `bash`, `javascript` or `typescript`) and its `cwd`. `RestartContext` SHALL keep the id, `language`, `cwd` and `envs`, shut the kernel down and start a new kernel with a new connection file (new HMAC key), ending any in-flight execution with `error{KernelRestarted}` + `end{0}`. `DestroyContext` SHALL end in-flight executions with `error{ContextDestroyed}` + `end{0}`, shut the kernel down and remove its socket directory; the default context SHALL be refused with `FAILED_PRECONDITION` (`default-bash`, `default-javascript` and `default-typescript` are not protected). Unknown ids SHALL fail with `NOT_FOUND`.

#### Scenario: isolation between contexts
- **WHEN** the SDK runs `x = 42` in the default context and `x` in a freshly created context
- **THEN** the second execution's `error.name` is `NameError`

#### Scenario: restart loses state and keeps the id
- **WHEN** the SDK runs `y = 7` in a context, calls `restart_code_context(ctx)` and runs `y` again in it
- **THEN** the last execution's `error.name` is `NameError` and `ctx.id` still appears in `list_code_contexts()`

#### Scenario: default context protected
- **WHEN** the SDK calls `remove_code_context("default")`
- **THEN** `InvalidArgumentException` is raised and the default context still serves `run_code`

#### Scenario: explicit bash context
- **WHEN** the host test calls `CreateContext{language: "bash"}` against the fake sidecar whose `ready` lists `bash`
- **THEN** the sidecar received `create_context` with `"language":"bash"`, the returned id matches `ctx-<12 hex>` and `ListContexts` shows it with `language == "bash"` after `default`

#### Scenario: language default can be destroyed and comes back
- **WHEN** the host test runs `Execute{language: "bash"}`, `DestroyContext{"default-bash"}` and `Execute{language: "bash"}` again
- **THEN** the destroy succeeds and the sidecar received two `create_context` lines for `default-bash`

#### Scenario: restarting a TypeScript context loses its state
- **WHEN** the e2e runs `let w = 9` in a `typescript` context, calls `restart_code_context(ctx)` and runs `w` in it again
- **THEN** the last execution's `error.name` is `ReferenceError` and `ctx.id` still appears in `list_code_contexts()`

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
On the first accepted `/run` of a boot `rayd` SHALL, after answering `200`, send `restart_context{"default", envs: <payload envs>}` so the sandbox gets a kernel with a new connection file (new HMAC key), fresh PRNG state and the payload's environment; `kernel_ready` SHALL be false until the reply arrives and the duration SHALL be logged as `restart_ms`; no other context SHALL be rotated (per-language defaults do not exist before `/run`). On `/resume` `rayd` SHALL send `reseed`; the sidecar SHALL answer immediately, reseeding inline (`random.seed()` and, when `numpy` is loaded, `numpy.random.seed()` in a silent cell) every Python context that is `ready` and idle, scheduling a background reseed that takes its turn after the running cell for every Python context that is `ready` but busy (listed as `deferred`, completion logged as `reseeded_deferred` with `context_id` and `outcome`), listing Python contexts that are `restarting` or `dead` as `failed`, and listing every non-Python context as `skipped` without touching it; the reply SHALL be `{"reseeded": [...], "deferred": [...], "failed": [...], "skipped": [...]}` and `rayd` SHALL log the four counts. `rayd` SHALL treat `reseed` as an advisory op: a timeout on it SHALL be logged with `advisory_op_timeout: true` and SHALL NOT count towards the sidecar's consecutive-timeouts kill switch. Nothing random SHALL be generated by `rayd` or the sidecar before `/run` other than what the kernel processes create at start.

#### Scenario: two sandboxes differ
- **WHEN** two sandboxes are created from the same image version and each runs `import random; [random.random() for _ in range(3)]` and `import numpy; numpy.random.default_rng().random(3).tolist()`
- **THEN** the `text` results differ between the sandboxes for both cells

#### Scenario: restart request carries the payload envs
- **WHEN** an integration test posts `/run` with a payload whose `envs` is `{"M4": "1"}`
- **THEN** the fake sidecar receives `restart_context` for `default` with `envs == {"M4": "1"}` and no other `restart_context`, and `Health.kernel_ready` returns to true after its reply

#### Scenario: reseed changes the sequence
- **WHEN** the sidecar's real-kernel test reads `random.random()`, sends `reseed` and reads it again in the same kernel
- **THEN** the two values differ and the reply lists that context as reseeded

#### Scenario: reseed replies while a cell runs
- **WHEN** the sidecar's real-kernel test starts `time.sleep(3)` and sends `reseed` one second later
- **THEN** the reply arrives within 1 s with `deferred: ["default"]` and `reseeded: []`, and after the cell ends `random.random()` differs from the value read before the cell

#### Scenario: advisory timeout is not counted
- **WHEN** the fake sidecar delays its `reseed` reply by 20 s across a `/resume`
- **THEN** `rayd` logs `advisory_op_timeout: true`, `consecutive_timeouts` stays 0, the sidecar is not killed and the next `Execute` succeeds

#### Scenario: three pauses during long cells
- **WHEN** the e2e pauses and resumes the sandbox three times while a 25 s cell is in flight each time
- **THEN** `get_health().kernel_ready` is true after the third resume and `run_code("import random; random.random()")` succeeds without a sidecar restart

#### Scenario: bash context is skipped on resume
- **WHEN** the fake `/resume` fires with a `bash` fake context and `default` live
- **THEN** the reseed reply lists `default` under `reseeded` and the bash context under `skipped`

### Requirement: Unary status mapping and Reattach
`CreateContext`, `ListContexts`, `DestroyContext` and `RestartContext` SHALL use gRPC statuses: `INVALID_ARGUMENT` (language name, cwd, id syntax, envs), `UNIMPLEMENTED` (a known language the image does not ship, message naming `rayito-base-poly`), `NOT_FOUND` (context), `FAILED_PRECONDITION` (default context destroy), `RESOURCE_EXHAUSTED` (8 contexts), `UNAVAILABLE` with a message starting with `kernel not ready` (no sidecar, relaunching, context not ready within 30 s, sidecar op timeout) or with the phase name (suspending/terminating), `ABORTED` (concurrent restart/destroy of the same context), `INTERNAL` (protocol fault, message without payloads). `Execute` SHALL use the same table for everything that fails before its first message, plus `INVALID_ARGUMENT` for `language` combined with `context_id` and for per-execution `envs` on a non-Python context. `Reattach` SHALL be served with the statuses of its own requirement (`NOT_FOUND`, `INVALID_ARGUMENT`, `OUT_OF_RANGE`, `RESOURCE_EXHAUSTED`, `UNAVAILABLE`). Error messages SHALL never contain code, output, envs, cwd or tracebacks.

#### Scenario: execute while suspending
- **WHEN** `Execute` is called while the phase is `Suspending`
- **THEN** the RPC fails with `UNAVAILABLE` and message `suspending` before any event

#### Scenario: kernel gate in the SDK
- **WHEN** `Execute` fails with `UNAVAILABLE` `kernel not ready: relaunching`
- **THEN** the SDK raises `SandboxNotReadyException` and does not retry

#### Scenario: reattach on an unknown id
- **WHEN** `Reattach` is called with an execution id that was never issued
- **THEN** the RPC fails with `NOT_FOUND`

#### Scenario: unimplemented language names the poly image
- **WHEN** `CreateContext{language: "javascript"}` is sent to a `rayd` whose sidecar announced `["bash", "python"]`
- **THEN** the RPC fails with `UNIMPLEMENTED` and the message contains `rayito-base-poly` and no other request data

### Requirement: Python SDK run_code and code contexts
`Sandbox.run_code(code, *, language=None, context=None, on_stdout=None, on_stderr=None, on_result=None, on_error=None, envs=None, timeout=300, request_timeout=None)` SHALL normalise `language` (case-insensitive; `js` → `javascript`, `ts` → `typescript`; `None`/`""` → not sent; anything outside `python`, `bash`, `javascript`, `typescript` → `InvalidArgumentException` before any call), raise `InvalidArgumentException` when both `language` and `context` are given, set `ExecuteRequest.language` only when a language was given, open `Execute` on the unary channel with `timeout_ms = round(timeout × 1000)` (`None`/`0` → no limit and no gRPC deadline, else a gRPC deadline of `timeout + 15 s`), feed every event into an `Execution(results, logs, error, execution_count)` (`keepalive` ignored; `on_stdout`/`on_stderr` receive `OutputMessage(line, timestamp, error)`; `on_result` a `Result`; `on_error` an `ExecutionError`) and return it; `Execution.text` SHALL be the `text` of the result with `is_main_result`, else `None`; kernel-level errors SHALL be data in `Execution.error`, never raised. `Result` SHALL expose `text, html, markdown, svg, png, jpeg, pdf, latex, json, javascript, data, chart, is_main_result, extra`, `formats()`, `__str__` and `_repr_*_`; `json` and `data` SHALL be parsed JSON (raw string kept if parsing fails); `chart` SHALL be parsed into `LineChart`, `ScatterChart`, `BarChart`, `PieChart`, `BoxAndWhiskerChart`, `SuperChart` or a `Chart` of type `UNKNOWN`. `ExecutionError.traceback` SHALL be the lines joined with `"\n"`. `create_code_context(*, cwd=None, language=None, envs=None, request_timeout=None) -> CodeContext(id, language, cwd)` SHALL accept the same language names (normalised the same way, `None` → `python`), and with `list_code_contexts() -> list[CodeContext]`, `remove_code_context(context)` and `restart_code_context(context)` SHALL accept a `CodeContext` or its id, use 90 s default deadlines for create/restart, and map `NOT_FOUND` to `NotFoundException` and `FAILED_PRECONDITION` to `InvalidArgumentException`. `AsyncSandbox` SHALL expose the same surface as coroutines.

#### Scenario: acceptance sequence
- **WHEN** the SDK runs `x = 42`, then `x`, then `print(x)`, then a matplotlib plot, then `1/0`, then `time.sleep(10)` with `timeout=2`
- **THEN** `run_code("x").text == "42"`, `"42" in "".join(run_code("print(x)").logs.stdout)`, the plot's `results[0].png` and `chart` are not `None`, `error.name == "ZeroDivisionError"` with `execution_count` set, and the last `error.name == "ExecutionTimeout"`

#### Scenario: callbacks receive typed messages
- **WHEN** the SDK runs `print('a'); print('b')` with `on_stdout=seen.append`
- **THEN** every element of `seen` is an `OutputMessage` with `error is False` and a positive `timestamp`, and their `line`s joined contain `a` and `b`

#### Scenario: async parity
- **WHEN** `AsyncSandbox.connect(...)` runs `x` in the default context, creates a context, runs `2*2` in it, removes it and runs `1/0`
- **THEN** the results are `"42"`, `"4"`, no exception on removal, and `error.name == "ZeroDivisionError"`

#### Scenario: language normalisation and exclusivity
- **WHEN** the unit test calls `run_code("echo 1", language="JS")`, `run_code("echo 1", language="r")` and `run_code("echo 1", language="bash", context="default")` against the fake `rayd`
- **THEN** the first sends `language == "javascript"` on the wire, and the other two raise `InvalidArgumentException` with no request reaching the fake

#### Scenario: python cells send no language
- **WHEN** the unit test calls `run_code("x")` and `run_code("x", language=None)`
- **THEN** neither `ExecuteRequest` has `language` set (`HasField("language")` is false)

#### Scenario: async language parity
- **WHEN** `AsyncSandbox.run_code("echo hi", language="bash")` and `AsyncSandbox.create_code_context(language="bash")` run against the fake
- **THEN** both requests carry `language == "bash"` and the returned `CodeContext.language == "bash"`

#### Scenario: typescript and its alias on the wire
- **WHEN** the unit test calls `run_code("1", language="ts")`, `run_code("1", language="TypeScript")` and `create_code_context(language="ts")` against the fake `rayd`, and then `run_code("1", language="tsx")`
- **THEN** the first three requests carry `language == "typescript"` and the returned `CodeContext.language == "typescript"`, and the last raises `InvalidArgumentException` with no request reaching the fake

### Requirement: Image ships the kernel stack and a warm snapshot
The `rayito-base` image SHALL install the exact pins of `kernel-sidecar/requirements.txt` (`ipykernel==6.31.0`, `ipython==9.15.0`, `jupyter_client==8.10.0`, `pyzmq==27.2.0`, `matplotlib==3.10.9`, `pandas==2.2.3`, `numpy==2.3.5`, plus the resolved exact `scipy` and `scikit-learn`) for Python 3.12 from wheels, copy the sidecar to `/opt/rayito/sidecar` (readable by all, `tests`/caches excluded, `requirements-poly.txt` included), build the matplotlib font cache and the IPython profile as `user` at build time, and take the snapshot only after `/ready` reported a warm default kernel. The same `Dockerfile` SHALL contain one conditional layer that runs only when `/opt/rayito/sidecar/kernels_variant` holds `poly` (installing the pins of `kernel-sidecar/requirements-poly.txt` with `pip check`, verifying `import bash_kernel` as `user` and the `rayito-bash` template, and installing the Deno 2.9.7 `aarch64-unknown-linux-gnu` binary, verified against its pinned sha256, at `/opt/rayito/deno/deno` with the `rayito-javascript` and `rayito-typescript` templates checked; no Node.js, `ijavascript` or compiler, Q57, Q61), so that the `full` artifact installs nothing new. `image-publish` SHALL record `snapshotBuild` sizes and build time; a `rayito-base` rebuilt from this Dockerfile SHALL report `memorySnapshotSizeInBytes` within 20 MB and `codeInstallSizeInBytes` (net of the `rayd` binary size change) within 10 MB of the previous version. The warm-up import list SHALL be decided by the measured rule of the `image-lifecycle` capability (`scipy.stats` and `sklearn.linear_model` leave the warm-up iff their combined RSS delta exceeds 100 MB; `numpy`, `pandas` and `matplotlib.pyplot` always stay because `/validate` and the acceptance tests use them), replacing the former 1.2 GB trimming knob.

#### Scenario: font cache present in the snapshot
- **WHEN** the SDK runs `import matplotlib, glob, os; sorted(os.path.basename(p) for p in glob.glob(os.path.join(matplotlib.get_cachedir(), 'fontlist-*.json')))` in a sandbox
- **THEN** the result lists at least one `fontlist-*.json`

#### Scenario: sizes reported
- **WHEN** `make image-publish` finishes the M6 version
- **THEN** the log prints `memorySnapshotSizeInBytes`, `codeInstallSizeInBytes`, `diskSnapshotSizeInBytes` and the build seconds, and the task notes record them next to version 10.0's

#### Scenario: packages stay importable after trimming
- **WHEN** the warm-up no longer imports `scipy` and `sklearn` and a user cell runs `import scipy.stats, sklearn.linear_model`
- **THEN** the cell succeeds

#### Scenario: rayito-base unchanged by the poly layer
- **WHEN** `rayito-base` is republished from the Dockerfile that carries the conditional layer and its `snapshotBuild` is compared with version 17.0 (`928 100 352` B memory)
- **THEN** `|memory delta| ≤ 20 MB`, `|code install delta net of the rayd binary| ≤ 70 MB` (10 MB in M7; from M9 on git-core, Q76, and the `rayd` growth since 17.0 are inside it), and a sandbox from it has no `bash_kernel` importable (the conditional layer was a no-op; the builder publishes no Dockerfile log)

#### Scenario: rayito-base carries no Deno
- **WHEN** the e2e runs `test -e /opt/rayito/deno` through `commands.run` on a sandbox of the `rayito-base` rebuilt from the Dockerfile with the Deno lines
- **THEN** the command exits non-zero (`CommandExitException`) and `run_code("1", language="typescript")` fails with `UNIMPLEMENTED` naming `rayito-base-poly`

### Requirement: Logging never exposes sandbox content
`rayd` and the sidecar SHALL log only allowlisted fields (`context_id`, `execution_id`, `execution_count`, `events`, `results`, mime type names, `timeout_ms`, `outcome`, `kernel_pid`, `attempt`, `backoff_ms`, `exit_code`, `warmup_ms`, `restart_ms`, `sidecar_restarts`, `sidecar_stderr_lines`, `duration_ms`, `bytes`, `chunks`, `contexts`, `language`, `languages`, `lazy`, `skipped`, counts) and SHALL never log code, stdout/stderr text, mime payloads, `envs`, `cwd`, error values, traceback lines, connection files or the sidecar's raw stderr.

#### Scenario: stderr lines that are not the log schema
- **WHEN** the fake sidecar writes a plain-text line to stderr
- **THEN** `rayd` increments `sidecar_stderr_lines` and the line's text never appears in `rayd`'s output

#### Scenario: lazy creation logs the language only
- **WHEN** a bash cell triggers the lazy creation of `default-bash`
- **THEN** `rayd`'s log line has `context_id`, `language = "bash"` and `lazy = true` and contains none of the cell's text

### Requirement: Executions are recorded in a 4 MiB ring and outlive their stream
Every execution SHALL be recorded by `rayd` independently of the `Execute` stream that started it: its counted events (`started`, `stdout`, `stderr`, `result`, `error`, `end`) SHALL be kept in a per-execution ring of at most 4 MiB (cost = text length, sum of a result's mime strings, error value plus traceback; oldest evicted first; an event larger than the ring evicts everything and is not retained), at most 8 subscribers SHALL be attached to an execution, an ended execution SHALL stay available for 30 s of running time with at most 32 ended executions retained (oldest evicted), and the `Execute` stream SHALL be one subscriber among others. The sidecar's events for an execution SHALL be drained into the ring whether or not any subscriber is reading.

#### Scenario: ring replay bounds
- **WHEN** a unit test pushes counted events past 4 MiB into an `ExecuteRing`
- **THEN** `replay_from(oldest_seq)` returns the retained tail, `replay_from(oldest_seq − 1)` and `replay_from(next_seq + 1)` fail with `ReplayOutOfRange`, and `replay_from(0)` returns nothing

#### Scenario: ended execution retained then reaped
- **WHEN** an execution ended 31 s of running time ago and the reaper runs
- **THEN** `Reattach` on it fails with `NOT_FOUND`, and before the reaper it replayed through `end`

### Requirement: Reattach replays and follows an execution
`CodeService.Reattach{context_id, execution_id, from_seq}` SHALL fail with `UNAVAILABLE` while the phase is `Suspending` or `Terminating`, `INVALID_ARGUMENT` for a malformed `execution_id`, `NOT_FOUND` for an unknown, expired or mismatched (`context_id`) execution, `OUT_OF_RANGE` when `from_seq` is evicted or beyond the next `seq`, and `RESOURCE_EXHAUSTED` at the ninth subscriber, all before any message; otherwise it SHALL replay the retained events with `seq >= from_seq` (`0` → none), follow live events without gap or duplicate, emit `keepalive` every 5 s of silence, and end after `end` (immediately after the replay for an ended execution). Dropping a `Reattach` stream SHALL never interrupt the execution.

#### Scenario: reattach after the stream was closed by a suspend
- **WHEN** an integration test has an `Execute` of `sleep 3` closed by `/suspend`, posts `/resume` and opens `Reattach(context_id, execution_id, from_seq: last_seq + 1)`
- **THEN** the stream delivers the remaining events and the `end`, and the fake sidecar never received `interrupt`

#### Scenario: stalled origin, healthy reattach
- **WHEN** the origin `Execute` client of `big 2000` stops reading with `stall_timeout` 1 s while a `Reattach` subscriber keeps reading
- **THEN** the origin stream ends with `error{OutputTruncated}` + `end{0}` and the `Reattach` subscriber receives every chunk and the real `end`

#### Scenario: unknown execution from the SDK
- **WHEN** the SDK's `run_code` reattaches to an execution the agent no longer retains
- **THEN** the agent answers `NOT_FOUND` and the SDK raises `SandboxException`

### Requirement: Execute selects a per-language default context and creates it lazily
`ExecuteRequest` SHALL carry `optional string language = 5` with the canonical values `python`, `bash`, `javascript` and `typescript`. When `context_id` is absent and `language` is absent, empty or `python`, `rayd` SHALL execute on `default`; when `context_id` is absent and `language` is `bash`, `javascript` or `typescript`, `rayd` SHALL execute on the context `default-bash` / `default-javascript` / `default-typescript`, creating it first if it is not live (same `cwd` and `envs` defaults as the rotated `default`: the `/run` payload `workdir`/`envs` or the home) under a per-language lock so that concurrent first executions start exactly one kernel; a lazily created context SHALL count toward the 8-context cap, SHALL be logged as `context created` with `language` and `lazy: true`, and SHALL NOT be started before `/run`. When `context_id` is present and `language` is non-empty, `Execute` SHALL fail with `INVALID_ARGUMENT`. An unknown language name SHALL fail with `INVALID_ARGUMENT`; a known language the running image does not ship (absent from the sidecar's `ready.languages`) SHALL fail with `UNIMPLEMENTED` and a message naming `rayito-base-poly`. `Execute{context_id: "default-bash"}` before the lazy creation SHALL be `NOT_FOUND`. `DestroyContext` on `default-bash` / `default-javascript` / `default-typescript` SHALL be allowed (only `default` is protected) and the next `Execute{language}` SHALL re-create the context. Per-execution `envs` on a context whose language is not `python` SHALL fail with `INVALID_ARGUMENT` before the sidecar is contacted (the set/restore cells are Python source).

#### Scenario: bash cell through the poly image
- **WHEN** the e2e runs `run_code("echo hi", language="bash")` on a sandbox created from `RAYITO_TEMPLATE_POLY`
- **THEN** `"hi"` is in `"".join(execution.logs.stdout)`, `execution.error is None`, and `list_code_contexts()` contains a context with `id == "default-bash"` and `language == "bash"`

#### Scenario: typescript and javascript cells through the poly image
- **WHEN** the e2e runs `run_code("const x: number = 40 + 2; x", language="typescript")`, then `run_code("x + 1", language="typescript")`, then `run_code("let y = 40 + 2; y", language="javascript")` and `run_code("y", language="js")` on a sandbox created from `RAYITO_TEMPLATE_POLY`
- **THEN** the texts are `42`, `43`, `42` and `42`, none contains `\x1b`, the first-cell and second-cell latencies are reported, and `list_code_contexts()` contains `default-typescript` with `language == "typescript"` and `default-javascript` with `language == "javascript"`

#### Scenario: javascript is a known language no image ships
- **WHEN** the e2e runs `run_code("1 + 1", language="javascript")` on a sandbox created from `RAYITO_TEMPLATE_POLY`, the case that M7 answered with `UNIMPLEMENTED` because `ijavascript` could not be built (`AWS_API_NOTES.md` Q57)
- **THEN** the reservation is lifted: the Deno kernel (Q61) answers `execution.text == "2"` with `execution.error is None`, no `UNIMPLEMENTED` is raised, and `javascript` stays `UNIMPLEMENTED` naming `rayito-base-poly` only on images that do not ship it (scenario "Deno languages not shipped by the image")

#### Scenario: one lazy kernel under concurrency
- **WHEN** two `Execute{language: "bash"}` arrive at the same time on a host test against the fake sidecar whose `ready` listed `bash`
- **THEN** exactly one `create_context` line with `"language":"bash"` and `"context_id":"default-bash"` was sent to the sidecar before the two `execute` lines

#### Scenario: language and context are exclusive
- **WHEN** `Execute{context_id: "default", language: "bash"}` is sent
- **THEN** the RPC fails with `INVALID_ARGUMENT` and the SDK raises `InvalidArgumentException` (TypeScript: `InvalidArgumentError`) before any stream event

#### Scenario: language not shipped by the image
- **WHEN** the e2e runs `run_code("echo hi", language="bash")` on a sandbox created from `rayito-base` (`RAYITO_TEMPLATE`)
- **THEN** the SDK raises `InvalidArgumentException` with `grpc_code == UNIMPLEMENTED` and `rayito-base-poly` in the message, and no context was created

#### Scenario: per-execution envs are python-only
- **WHEN** `run_code("echo $A", language="bash", envs={"A": "1"})` is called
- **THEN** `InvalidArgumentException` is raised and the fake sidecar received no `execute` line

#### Scenario: bash timeout follows the generic rule
- **WHEN** the e2e runs `run_code("sleep 30", language="bash", timeout=2)` and then `run_code("echo back", language="bash")`
- **THEN** the first has `error.name == "ExecutionTimeout"` and the second prints `back`

#### Scenario: Deno languages not shipped by the image
- **WHEN** the e2e runs `run_code("1", language="javascript")` and `run_code("1", language="typescript")` on a sandbox created from `rayito-base` (`RAYITO_TEMPLATE`)
- **THEN** each raises `InvalidArgumentException` with `grpc_code == UNIMPLEMENTED` and `rayito-base-poly` in the message, and `list_code_contexts()` still lists only `default`

#### Scenario: one lazy typescript kernel on the host
- **WHEN** a host test sends `Execute{language: "typescript"}` to `rayd` whose fake sidecar announced `python,javascript,typescript`
- **THEN** exactly one `create_context` line with `"language":"typescript"` and `"context_id":"default-typescript"` precedes the `execute` line, and `ListContexts` shows `default-typescript` after `default`

### Requirement: The sidecar resolves a kernelspec per language and announces what the image ships
The sidecar SHALL keep a catalog of four languages, each with a kernel name, a no-op probe cell, a transport and whether `text/plain` results are stripped of ANSI: `python` → `rayito` (template `python3 -m ipykernel_launcher -f {connection_file} --config=<sidecar>/ipython/ipython_kernel_config.py`, probe `pass`, `ipc`), `bash` → `rayito-bash` (template `python3 -m bash_kernel -f {connection_file}`, probe `:`, `ipc`), `javascript` → `rayito-javascript` and `typescript` → `rayito-typescript` (both with template `/opt/rayito/deno/deno jupyter --kernel --conn {connection_file}` and `env` `NO_COLOR=1`, `DENO_DIR=${HOME}/.cache/deno`, `DENO_NO_UPDATE_CHECK=1`, probe `void 0`, loopback `tcp`, `text/plain` stripped; Deno 2.9.7 replaces `ijavascript`, which cannot be installed on the al2023 ARM64 builder without a compiler, AWS_API_NOTES.md Q57, Q61), the shipped templates under `kernel-sidecar/jupyter/kernels/<kernel_name>/kernel.json` with `interrupt_mode: signal` for `python` and `bash` and `interrupt_mode: message` for the two Deno kernels (Deno answers `interrupt_request` on the control channel and installs no `SIGINT` handler). At start `install_kernelspecs` SHALL write, with the existing rewrites (`/opt/rayito/sidecar` → `--sidecar-root`, `python3` → the running interpreter), only the specs whose template exists and that are available: `argv[0]` resolves on `PATH` or is an existing absolute path, every absolute path in `argv` exists, and a `python3 -m <module>` spec has an importable `<module>`; `python` SHALL always be available. The `ready` event SHALL list the available names, sorted, under `languages`. `create_context` SHALL accept `language` (absent = `python`), refuse an unknown name with `invalid_argument` `unknown language` and an unavailable one with `invalid_argument` `language not installed`, and start the language's kernel with its probe cell instead of `pass`. Non-Python kernels SHALL receive no IPython configuration, startup scripts, formatters or warm-up; their Jupyter messages SHALL be mapped to events by the same rules as Python. `reseed` SHALL skip contexts whose language is not `python` and list them under `skipped`; `resume` SHALL probe every context regardless of language.

#### Scenario: availability follows the filesystem
- **WHEN** the unit test installs the templates into a `tmp_path` sidecar root with `bash_kernel` importable but no `/opt/rayito/deno/deno`
- **THEN** `install_kernelspecs` writes `rayito` and `rayito-bash` only and the `ready` event carries `"languages": ["bash", "python"]`

#### Scenario: unknown and unavailable languages refused by the sidecar
- **WHEN** `create_context` arrives with `language: "r"` and then with `language: "javascript"` on a sidecar whose catalog resolved only `python` and `bash`
- **THEN** the replies are `invalid_argument` `unknown language` and `invalid_argument` `language not installed`, and no kernel was started

#### Scenario: reseed skips non-python contexts
- **WHEN** a fake `bash` context and the `default` Python context are live and `reseed` arrives
- **THEN** the reply lists `default` under `reseeded` and the bash context under `skipped`, and `rayd` logs the four counts

#### Scenario: Deno specs follow the binary
- **WHEN** the unit test copies the templates into a `tmp_path` sidecar root, points `argv[0]` of `rayito-javascript` and `rayito-typescript` at `tmp_path/"deno"`, and runs `install_kernelspecs` with and without that file
- **THEN** with the file both specs are written, each with `interrupt_mode == "message"` and the three `env` entries, and `ready` lists `javascript`, `python` and `typescript`; without it neither is written

#### Scenario: an unknown name is still unknown
- **WHEN** `create_context` arrives with `language: "ts"` on any sidecar
- **THEN** the reply is `invalid_argument` `unknown language` and no kernel was started

### Requirement: Deno kernels are accepted against a real rayito-base-poly publish
The change SHALL close only after `rayito-base-poly` and `rayito-base` are published from the Dockerfile with the Deno lines (`--base-image-version` pinned), and `clients/python/tests/e2e/test_m9_deno_kernels.py`, the rest of `test_m7_poly_kernels.py` and the TypeScript poly e2e pass against real AWS. The `snapshotBuild` numbers SHALL be checked as follows:
- The Deno cost is computed pairwise against the same-session pair `rayito-base-poly` 3.0 (`921 780 224` / `1 323 397 120` B) and `rayito-base` 18.0 (`938 098 688` / `1 320 202 240` B), so the `rayd` binary cancels out.
- Deno's code install cost SHALL lie between 110 MB and 160 MB (`codeInstallSizeInBytes` is not the sum of file bytes: the ≈ 85 MB binary measured 132-137 MB).
- Its memory cost SHALL lie within ±40 MB, because Deno is not warmed and a pairwise delta adds the snapshot noise of four builds.
- The rebuilt `rayito-base` SHALL stay within memory ±20 MB of the previous `rayito-base`, and within 50 MB of code install net of the `rayd` binary delta (git-core of `m9-e2b-v2-surface` D16, Q76, adds 38-41 MB against a pre-M9 version).

The median `kernel_ready_s` of five sequential launches of `rayito-base-poly` SHALL NOT exceed that of `rayito-base` by more than 1.0 s. A JavaScript variable SHALL survive `pause()`/`resume()` with `Health.kernel_state_lost == false`. An endless loop with `timeout=2` SHALL end with `ExecutionTimeout`, and the next cell of that context SHALL work. The measured numbers (build seconds, the six sizes, the Deno cost, both `kernel_ready_s` medians, the first- and second-cell latencies, the Deno kernel RSS, whether `NO_COLOR` alone removed the escapes, and which timeout path ran) SHALL be recorded in an `AWS_API_NOTES.md` §16 row with placeholders only.

#### Scenario: sizes within the bands
- **WHEN** `test_deno_snapshot_sizes` runs with `RAYITO_POLY_SIZES`, `RAYITO_BASE_SIZES`, `RAYITO_BASE_PREVIOUS_SIZES` and `RAYITO_RAYD_BYTES_DELTA` set from the two publishes
- **THEN** `110 000 000 ≤ deno_code ≤ 160 000 000`, `|deno_memory| ≤ 40 000 000`, the `rayito-base` memory delta is at most 20 MB and its code install delta net of `rayd` at most 50 MB, and all six numbers are printed

#### Scenario: sizes skipped without inputs
- **WHEN** the test runs without one of those variables
- **THEN** it is skipped with a reason naming the variables and creates no MicroVM

#### Scenario: a JavaScript variable survives pause and resume
- **WHEN** the e2e runs `globalThis.kept = 42` with `language="javascript"`, then `pause()` and `resume()`, and runs `kept` with `language="javascript"`
- **THEN** the text is `42`, `get_health().kernel_state_lost` is false and `resume_generation` increased

#### Scenario: endless loop times out and the context recovers
- **WHEN** the e2e runs `while (true) {}` with `timeout=2` in a `typescript` context, then `1 + 1` in the same context
- **THEN** the first ends with `error.name == "ExecutionTimeout"`, the second returns `text == "2"`, and whether earlier state survived is reported

#### Scenario: kernel_ready unchanged
- **WHEN** `scripts/bench_cold_start.py --phases a --sequential 5` runs against `rayito-base` and against `rayito-base-poly` in one session
- **THEN** the poly median `kernel_ready_s` is at most the base median plus 1.0 s and both medians are recorded

## MODIFIED Requirements

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

## ADDED Requirements

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

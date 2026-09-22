## Context

State after M3 (accepted 2026-09-15 against real AWS, image `rayito-base`
4.0): `rayd` serves `HealthService`, `ProcessService` and
`FilesystemService` behind the `AccessTokenLayer`; `PtyService` and
`CodeService` are `Pending*` stubs answering `UNIMPLEMENTED`. `rayd` runs as
root with capped capabilities and spawns children as `user` (uid 1000)
through `TokioProcessSpawner` (`PreExecPlan`: `setsid`, `setrlimit`,
identity switch), `NixUserLookup`, `UserPolicy` (`RAYITO_ALLOW_ROOT=1`),
`build_child_env` (environment from scratch) and `resolve_cwd`. The hooks
router answers the six hooks; `/ready` is immediate, `/validate` is a no-op,
`/run` installs the token digest once per boot, `/suspend` and `/resume`
only move the phase machine (their real checklists are M5).
`HealthResponse.kernel_ready` and `kernel_state_lost` are always `false`.
The Python SDK has `Sandbox`/`AsyncSandbox` with `commands` and `files`,
two gRPC channels (unary + long streams), `_call_unary` (one proxy-403
re-mint), `_open_stream`, `translate_rpc_error`, `_stream_failure` and an
in-process fake `rayd` for unit tests; `Sandbox.run_code` raises
`NotImplementedError` and `create()` polls `Health` until `agent_ready`
only.

Measured facts that shape this design (`AWS_API_NOTES.md` §4, §8, §9, §15,
§16):

- The image build runs `CMD`, waits for `/ready` to answer 200 and takes a
  memory + disk snapshot **with every process alive**; a `/validate` call
  then runs on a throwaway VM restored from that snapshot so AWS can sample
  the pages it touches (§4, §8). Nothing done in `/validate` persists.
- Everything in memory at `/ready` is identical in every MicroVM and every
  resume: `random.random()` evaluated at import time gave the same value in
  two VMs of the same version; `os.urandom` differs (Q25). The Jupyter
  connection file's HMAC key generated before `/ready` is therefore shared
  by every sandbox of a version unless it is rotated after `/run` (T5).
- The probe image with Python 3.12 + `ipykernel` preloaded snapshotted at
  672–678 MB of memory and took 135–145 s to build; `rayito-base` 4.0 (no
  kernel) is at 572 MB. AWS estimates ≈ 1 s per 500 MB of snapshot read on
  run/resume (Q21, §15). `run-microvm` → `agent_ready` measured 1.97–2.36 s
  in M1–M3.
- An `ipykernel` over ZMQ `ipc://` survives suspend/resume with its
  variables (Q7): the design only has to keep the sockets under a path
  that lives in the snapshot's disk/tmpfs and never close them on
  `/suspend` (M5).
- `CLOCK_MONOTONIC` advances during suspend (Q19): a tokio deadline armed
  for an execution fires during a pause. M4 documents it; M5 rearms.
- A stream with no bytes does not count as endpoint traffic and a silent
  stream is not cut by the proxy for at least 30 s with pings (Q15, Q31):
  `Execute` emits `KeepAlive` every 5 s so a long cell keeps the VM out of
  its idle policy and the client sees progress.
- Inside the VM `rayd` is root without `CAP_SYS_RESOURCE`; `RLIMIT_NOFILE`
  is 1024/1024 for children, `RLIMIT_NPROC` 512 applies per uid (Q20, Q30).
  Kernels, their threads and the sidecar all count against uid 1000's 512.
- `/run` must answer in < 2 s (budget); `runHookPayload` is the only
  per-VM channel and carries `envs` that the kernels must see (ADR-004).

Constraints: `openspec/project.md` hard rules (no invented AWS parameters,
`.proto` is the source of truth, hexagonal boundaries, ARM64 musl, security
defaults ship now, `clippy` pedantic, no `unwrap` outside tests, identifiers
in English, no inline comments in bodies, never log code, output, mime
payloads, envs, tracebacks or tokens; the sidecar is never published to
PyPI).

## Goals / Non-Goals

**Goals:**

- `CodeService` complete on real AWS except `Reattach`: contexts,
  streaming executions with E2B-identical result semantics, server-enforced
  timeout, cancellation, keepalives, bounded memory per stream.
- A warm default kernel captured in the snapshot, with the cloned-state
  problem closed: fresh HMAC key and fresh PRNG seeds in every sandbox
  after `/run`, reseed after `/resume`, and the cost of the rotation
  measured and recorded.
- `Health.kernel_ready` honest at every moment (boot, `/run` rotation,
  sidecar crash, context restart) and part of `create()`/`connect()`
  readiness.
- Python SDK `run_code()` and the code-context methods with E2B naming and
  models, sync and async identical.
- Every rule below has a host-side unit test, a `cfg(unix)` integration
  test, a sidecar test, an SDK unit test against the fake, or an e2e
  assertion.

**Non-Goals:**

- `Code.Reattach`, the per-execution 4 MiB ring, the `/suspend` stream
  close with `StreamError{code:"suspending"}`, `kernel_info` probes and
  `kernel_state_lost` after resume: M5. M4 ships the sidecar ops M5 needs
  (`quiesce`, `resume`) but `rayd` only calls `reseed` from `/resume`.
- Languages other than Python (`CreateContextRequest.language` accepts
  `""` or `"python"`; anything else → `INVALID_ARGUMENT`).
- A per-context output socket (the single stdio pipe is a known
  serialisation point, D6); consolidating the sidecar in Rust (M6 option,
  ADR-002).
- `stdin` for executions (`allow_stdin=False`), `%matplotlib` GUI backends,
  widgets/comms, `input()` support.
- Any change to `create-microvm-image`/`run-microvm` parameters or hook
  timeouts already declared in `scripts/publish_image.py`.
- The TypeScript client and the E2B compatibility shim (M6).

## Decisions

### D1. Proto usage: no edits

`code.proto`, `common.proto` and `health.proto` already carry every field
M4 needs. Wire mapping used by both sides:

| SDK call | RPC | Request | Response / notes |
|---|---|---|---|
| `run_code(code, context=...)` | `Execute` (server-stream) | `ExecuteRequest{context_id?, code, timeout_ms, envs}` | `ExecuteEvent` sequence: `keepalive`* → `started` → (`stdout`\|`stderr`\|`result`\|`error`\|`keepalive`)* → `end`; exactly one `started` and one `end` per execution; at most one `error`; `error` is never the last message |
| `create_code_context(cwd, language, envs)` | `CreateContext` | `CreateContextRequest{language, cwd?, envs}` | `CreateContextResponse{context_id}` |
| `list_code_contexts()` | `ListContexts` | `ListContextsRequest{}` | `ListContextsResponse{contexts: [ContextInfo{context_id, language, cwd}]}`, default context first, then by creation order |
| `remove_code_context(ctx)` | `DestroyContext` | `DestroyContextRequest{context_id}` | `DestroyContextResponse{}` |
| `restart_code_context(ctx)` | `RestartContext` | `RestartContextRequest{context_id}` | `RestartContextResponse{}`; the same `context_id` keeps working with a fresh kernel |
| (M5) | `Reattach` | `ReattachRequest` | `UNIMPLEMENTED` ("Reattach arrives in M5") |
| `create()`/`connect()` readiness | `Health` | `HealthRequest{}` | `kernel_ready` real (D9) |

Field semantics fixed here (the proto comments stay as they are):

- `ExecuteRequest.context_id` empty → the default context (`"default"`,
  D3). A `context_id` that is not registered → `NOT_FOUND`. Syntax: 1–64
  characters of `[A-Za-z0-9_-]`, else `INVALID_ARGUMENT`.
- `ExecuteRequest.code` larger than `MAX_CODE_BYTES = 1 MiB` →
  `INVALID_ARGUMENT`. Empty code is accepted (the kernel answers with
  `started` + `end` and no output).
- `ExecuteRequest.timeout_ms`: `0` = no server limit. Otherwise the clock
  starts when `rayd` accepts the request (queueing behind another execution
  of the same context counts) and D7 applies. Values above
  `MAX_EXECUTE_TIMEOUT_MS = 28 800 000` are clamped (a sandbox never lives
  longer, ADR-007).
- `ExecuteRequest.envs`: set in the kernel process for this execution only
  (D5), restored afterwards. Validated like `commands.run` envs (`_payload.
  validated_envs`).
- `ExecuteEvent.seq`: `1, 2, 3…` for `started`, `stdout`, `stderr`,
  `result`, `error`, `end`; `keepalive` messages carry `seq = 0` and are
  never counted (M5's ring stores only counted events).
- `ExecutionStarted.execution_id`: `exec-<16 hex>` generated by `rayd` from
  `getrandom` at request time (never before `/run`, never by the sidecar).
  `execution_count` is Jupyter's `execute_input.execution_count`.
- `OutputChunk.text` is one Jupyter `stream` message's text, split by the
  sidecar into pieces of at most 64 KiB (UTF-8 boundary respected);
  `timestamp_unix_ns` is the sidecar's `time.time_ns()` when the message
  was received. Chunks are not lines.
- `ExecutionResult`: `is_main_result = true` only for `execute_result`;
  mime mapping D5. `json`, `chart` and `data` are JSON documents
  (`json.dumps` of the Jupyter value); `png`/`jpeg`/`pdf` are the base64
  strings Jupyter already carries; `extra` holds every other mime type
  verbatim (non-string values serialised with `json.dumps`).
- `ExecutionError`: `name`/`value`/`traceback` from Jupyter's `ename`,
  `evalue`, `traceback` (ANSI stripped, D5). Synthetic errors produced by
  `rayd` or the sidecar use the closed name set `ExecutionTimeout`,
  `KernelDied`, `KernelRestarted`, `ContextDestroyed`, `ExecutionAborted`,
  `OutputTruncated`, each with an empty `traceback`.
- `ExecutionEnd.execution_count`: Jupyter's `execute_reply.execution_count`
  when the kernel answered; `0` when `rayd` or the sidecar ended the
  execution synthetically (the SDK keeps the value from `started`).
- `CreateContextRequest.language`: `""` or `"python"`; `cwd` resolved with
  `resolve_cwd(request, defaults.workdir, home)` and `validate_cwd` from
  the process domain (absolute, no NUL; a missing directory makes the kernel
  fail to start → `INVALID_ARGUMENT` "cwd does not exist"); `envs` become
  part of the kernel process environment (D4).
- `HealthResponse.kernel_ready`: D9. `kernel_state_lost` stays `false` in
  M4.
- `KeepAlive` on `Execute` every **5 s** of silence (`common.proto` already
  documents the 5 s during `Execute`).

Alternatives considered: a `StreamError` field in `ExecuteEvent` for
sidecar failures (rejected: `ExecutionError{KernelDied}` + `ExecutionEnd`
already expresses it without a proto change, and E2B surfaces the same
thing as an execution error); `execution_id` in `ExecuteRequest` chosen by
the client (rejected: `rayd` owns ids so M5's `Reattach` can trust them).

### D2. Sidecar protocol (`rayd-core::code::protocol`)

JSON lines over the sidecar's stdin (requests) and stdout (events). One
JSON object per line, UTF-8, no embedded newlines (`json.dumps` default
escaping), protocol version `1`. `rayd` accepts lines up to
`MAX_SIDECAR_LINE_BYTES = 16 MiB` (`LinesCodec::new_with_max_length`); a
longer line is a protocol error that restarts the sidecar (D8). The
sidecar therefore never writes one: `SidecarServer.emit` measures every
encoded line and, above `MAX_EVENT_LINE_BYTES = 15 MiB`, sends the same
`result` with `mime = {"rayito/omitted": "result: <n> bytes"}` (or the
same `error` with its value replaced and an empty traceback) and logs
`bytes`. The sidecar's stderr is its log (D8), never protocol.

Requests (`rayd` → sidecar), every one `{"id": <u64 ≥ 1>, "op": "…", …}`
with `id` unique per sidecar instance:

| `op` | Fields | Reply payload |
|---|---|---|
| `ping` | — | `{"kernel_ready": bool, "contexts": n}` |
| `create_context` | `context_id`, `cwd`, `envs` (object) | `{"kernel_pid": n}` |
| `execute` | `context_id`, `execution_id`, `code`, `envs` | no `reply`; the execution events below, all carrying this `id` |
| `interrupt` | `context_id`, `execution_id` (optional) | `{}` — running execution: `SIGINT` to the kernel; queued execution: removed from the queue and ended (D7); unknown: no-op |
| `destroy_context` | `context_id` | `{}` |
| `restart_context` | `context_id`, `envs` (object, replaces the context's envs) | `{"kernel_pid": n}` |
| `list_contexts` | — | `{"contexts": [{"context_id", "language", "cwd", "kernel_pid", "state"}]}` |
| `reseed` | — | `{"reseeded": [context_id…], "failed": [context_id…]}` |
| `quiesce` | — | `{}` after every pending stdout line is flushed (M5 calls it from `/suspend`) |
| `resume` | — | `{"contexts": [{"context_id", "alive": bool}]}` after a `kernel_info` probe with 5 s timeout per kernel, at most 8 kernels probed concurrently (M5 calls it from `/resume`) |

Events (sidecar → `rayd`), every one `{"event": "…", …}`:

| `event` | Fields | When |
|---|---|---|
| `ready` | `v` (1), `default_context_id`, `kernel_pid`, `warmup_ms` | first line after the default context is warm (D9); once per sidecar process |
| `reply` | `id`, `ok` (bool), `payload` (object) or `error: {"code", "message"}` | answer to every op except `execute` |
| `started` | `id`, `execution_id`, `execution_count` | Jupyter `execute_input` |
| `stdout` / `stderr` | `id`, `execution_id`, `text`, `timestamp_unix_ns` | Jupyter `stream` |
| `result` | `id`, `execution_id`, `is_main_result`, `mime` (object mime → string) | Jupyter `display_data` / `execute_result` |
| `error` | `id`, `execution_id`, `name`, `value`, `traceback` (array of strings) | Jupyter `error`, or synthetic |
| `end` | `id`, `execution_id`, `execution_count` | D5 end rule |
| `kernel_died` | `context_id`, `exit_code` (int or null), `execution_id` (optional) | the kernel process exited outside `destroy`/`restart` |

Reply error codes (closed set): `not_found` (context), `invalid_argument`,
`kernel_dead` (a context whose kernel is being restarted could not serve
the op in time), `busy` (`destroy`/`restart` collided with another
`destroy`/`restart` of the same context), `internal`. `rayd` maps them in
D10.

`rayd-core::code::protocol` owns the Rust side: `SidecarRequest{id, op:
SidecarOp}`, `SidecarOp` (one variant per op with the fields above),
`SidecarEvent` (one variant per event), `ReplyPayload`, `SidecarErrorCode`,
`encode_request(&SidecarRequest) -> String` (one line, trailing `\n`
added by the adapter), `decode_event(&str) -> Result<SidecarEvent,
ProtocolError{Malformed, UnknownEvent(String), MissingField(&'static str)}>`.
Unknown fields are ignored on both sides; unknown events are a
`ProtocolError` (never silently dropped). Tests cover a golden line per
request and per event.

The Python side (`rayito_kernel_sidecar/protocol.py`) mirrors it with
`TypedDict`s, `decode_request(line) -> Request` and
`encode_event(event) -> str`, and the same golden lines in
`kernel-sidecar/tests/test_protocol.py` so both codecs agree byte for byte
on the fixtures in `kernel-sidecar/tests/fixtures/protocol_v1.jsonl`
(the Rust tests read the same file).

Alternatives considered: a Unix socket with a length-prefixed binary
framing (rejected: ADR-002 fixes JSON lines over stdio; the sidecar is a
child whose lifetime `rayd` already owns through the pipe); MessagePack
(rejected: no dependency in `rayd-core`, JSON is debuggable).

### D3. Contexts and identifiers (`rayd-core::code::context`)

- `ContextId` newtype over `String`; `DEFAULT_CONTEXT_ID = "default"`.
  User contexts get `ctx-<12 hex>` from `getrandom`, generated by `rayd` at
  `CreateContext` time (always after `/run`; nothing random is generated
  before `/ready`).
- `MAX_CONTEXTS = 8` live contexts per sandbox including the default (the
  `/resume` probe budget of `ARCHITECTURE.md`; each kernel is ≈ 150–250 MB
  resident with the warm-up, so 8 is also what 2 GB tolerates). The 9th
  `CreateContext` → `RESOURCE_EXHAUSTED` ("context limit reached (8);
  destroy one first").
- `ContextRegistry` (pure, `BTreeMap<ContextId, ContextEntry{info:
  ContextInfo{context_id, language: "python", cwd}, envs: BTreeMap, state:
  ContextState{Starting, Ready, Restarting, Dead}, kernel_pid: Option<u32>,
  in_flight: u32}>`): `register`, `get`, `remove` (refuses the default with
  `CodeError::DefaultContextProtected`), `set_state`, `set_kernel_pid`,
  `list()` (default first, then insertion order), `kernel_pids()` (for D8),
  `clear()` (sidecar death).
- The default context exists from boot (created by the sidecar at start,
  D9) with `cwd = /home/user` (the `home` of `user`) and no envs until
  `/run` delivers the payload's `envs` (D11). It cannot be destroyed
  (`DestroyContext("default")` → `FAILED_PRECONDITION` "the default context
  cannot be destroyed; use RestartContext") so `kernel_ready` (D9) always
  has a referent.
- `RestartContext` keeps `context_id`, `cwd` and `envs` and loses every
  variable: the sidecar shuts the kernel down (`shutdown_kernel(now=True)`)
  and starts a **new** `AsyncKernelManager` with a new connection file
  (new HMAC key, new socket directory), never `restart_kernel()` which
  reuses the `Session` key.
- Per-execution state: `ExecutionId` newtype (`exec-<16 hex>`),
  `ExecutionTracker` (D6).

### D4. Sidecar process: identity, environment, kernels

**Who runs what.** The sidecar runs as **`user` (uid 1000, gid 1000)**,
spawned by `rayd` with the M2 posture: `PreExecPlan` (`setsid`, `setrlimit
NPROC 512 / NOFILE clamped / CORE 0`, `setgid`/`setuid` under
`IdentitySwitch::Enforce`; `KeepCurrent` on a dev box or CI), environment
built from scratch, `cwd = /home/user`, stdin/stdout piped, stderr piped.
Kernels are children of the sidecar and inherit uid 1000, so:

- the connection file (`0600`, written by `jupyter_client` with
  `secure_write`) and the ZMQ `ipc://` sockets are readable by the kernel
  without any chown dance;
- sandbox code (uid 1000) can read the connection file and reach its own
  kernel's sockets, which gives it nothing it does not already have (it
  runs *inside* that kernel as the same uid); it can also kill the sidecar
  or a kernel, which D8/D9 turn into a restart with `kernel_ready=false`
  meanwhile — a sandbox can only hurt itself;
- root is never involved after the spawn (T6), and `rayd` never talks to a
  kernel directly.

`rayd` prepares `/run/rayito/k` at boot (`mkdir -p`, `chown 1000:1000`,
`chmod 0700`; `/run` may be a tmpfs, so this is done at every boot, not in
the Dockerfile) before spawning the sidecar. `/run/rayito` is already in
the M3 deny list, so `FilesystemService` never serves those files.

**Sidecar spawn spec** (`rayd-core::code::sidecar_spawn_spec(identity:
&ProcessIdentity, config: &SidecarConfig) -> SpawnSpec`, pure, unit
tested): `program = config.python` (`"python3"`), `args = ["-m",
"rayito_kernel_sidecar", "--socket-root", config.socket_root,
"--sidecar-root", config.sidecar_root, "--default-cwd", identity.home,
"--default-context-id", "default"]`, env from `build_child_env(identity,
&BTreeMap::new(), &sidecar_env)` where `sidecar_env = {PYTHONPATH:
"<sidecar_root>/src", JUPYTER_PATH: "<sidecar_root>/jupyter",
PYTHONUNBUFFERED: "1", PYTHONDONTWRITEBYTECODE: "1", LANG: "C.UTF-8",
LC_ALL: "C.UTF-8"}` (so `PATH`, `HOME`, `USER`, `LOGNAME` come from the
identity as for any child), `stdin = StdinMode::Piped`, `limits =
ResourceLimits::default()`. The `/run` payload's `envs` are **not** in the
sidecar's environment (the sidecar starts before `/run`); they reach the
kernels through the ops (D11).

**Kernel launch** (`rayito_kernel_sidecar/kernels.py`, `KernelContext`):

- `AsyncKernelManager(kernel_name="rayito", transport="ipc",
  ip="/run/rayito/k/<context_id>/k", connection_file=
  "/run/rayito/k/<context_id>/kernel.json")`, the socket directory created
  `0700` by the sidecar; `start_kernel(cwd=<context cwd>, env=<kernel env>)`.
  `kernel_name="rayito"` resolves through `JUPYTER_PATH` to
  `kernel-sidecar/jupyter/kernels/rayito/kernel.json`:
  `argv = ["python3", "-m", "ipykernel_launcher", "-f",
  "{connection_file}", "--config=/opt/rayito/sidecar/ipython/
  ipython_kernel_config.py"]`, `language = "python"`,
  `interrupt_mode = "signal"`. The config path is rewritten from
  `--sidecar-root` at sidecar start (the kernelspec is generated into
  `<socket_root>/kernelspec/kernels/rayito/kernel.json` and that directory
  is prepended to `JUPYTER_PATH` for the kernel), so a dev checkout works
  without `/opt/rayito`.
- Kernel environment = the sidecar's own environment (already minimal) +
  `JUPYTER_RUNTIME_DIR=/run/rayito/k/<context_id>`,
  `JUPYTER_DATA_DIR=<home>/.local/share/jupyter`,
  `MPLCONFIGDIR=<home>/.cache/matplotlib`, `OPENBLAS_NUM_THREADS=1`,
  `OMP_NUM_THREADS=1`, `MKL_NUM_THREADS=1` + the context's `envs` (last
  wins; a user may override the thread caps). `MPLBACKEND` is **never**
  set: `ipykernel` installs `module://matplotlib_inline.backend_inline`
  itself when the variable is absent, which is what makes `plt.show()`
  produce a `display_data` PNG.
- `ipython_kernel_config.py`: `c.InteractiveShell.colors = "NoColor"`,
  `c.PlainTextFormatter.max_seq_length = 0`, `c.HistoryManager.enabled =
  False` (no sqlite in the snapshot), `c.InteractiveShellApp.exec_files =
  [<sidecar_root>/ipython/startup/0001_charts.py, 0002_data.py,
  0003_images.py, 0004_warmup.py]` (absolute paths derived from the config
  file's own location, run in order after IPython's own startup),
  `c.IPKernelApp.capture_fd_output = True` (default, kept explicit so C
  level prints reach `stdout`).
- Startup scripts (every kernel, so a new context is as warm as the
  default one):
  - `0001_charts.py`: registers an IPython formatter for the mime type
    `e2b/chart` (`BaseFormatter` subclass with `format_type = "e2b/chart"`,
    `print_method = "_repr_e2b_chart_"`) and defines
    `matplotlib.figure.Figure._repr_e2b_chart_` as
    `rayito_kernel_sidecar._vendor.e2b_charts.chart_figure_to_chart(self).to_dict()`
    (returns `None`, i.e. no mime entry, when the extractor raises;
    the exception is swallowed so a chart the extractor cannot parse still
    yields its PNG). Because `matplotlib_inline`'s `show()` goes through
    `display()`, every figure flushed at the end of a cell carries
    `image/png` + `e2b/chart`.
  - `0002_data.py`: formatter for `e2b/data`, `pandas.DataFrame.
    _repr_e2b_data_ = lambda self: self.to_dict(orient="list")`
    (`numpy` scalars converted with `.item()` in a `default=` hook when
    serialised, D5); `Series` likewise via `to_frame()`.
  - `0003_images.py`: gives `PIL.Image.Image` a `_repr_png_` and
    `_repr_jpeg_` that encode through `save(BytesIO(), format=…)` when
    the installed Pillow lacks them, so an image as the last expression
    yields `png`/`jpeg`. Guarded by `try: import PIL` (Pillow arrives as a
    matplotlib dependency).
  - `0004_warmup.py`: inside a function that is deleted afterwards (the
    user namespace stays clean, `sys.modules` stays warm): `import numpy,
    pandas, matplotlib, matplotlib.pyplot as plt, scipy, scipy.stats,
    sklearn, sklearn.linear_model`; builds one figure (`plt.plot([0, 1])`),
    renders it to a `BytesIO` with `savefig(format="png")`, calls
    `chart_figure_to_chart` on it and `plt.close(fig)`; one `pandas.
    DataFrame({"a": [1.0]}).describe()`; `numpy.linalg.inv` of a 2×2. This
    touches the font cache, the Agg renderer, the chart extractor and the
    BLAS pages so the snapshot (and `/validate`'s prefetch sampling)
    contains them.
- Warm = `await kc.wait_for_ready(timeout=120)` (the shell answers
  `kernel_info` only after `exec_files` ran) followed by one silent
  `execute("pass", silent=True, store_history=False)` whose `idle` was seen.
  The sidecar logs `warmup_ms` per kernel start.
- Interrupt = `await km.interrupt_kernel()` (`SIGINT` to the kernel).
  Destroy = `await km.shutdown_kernel(now=True)`, `cleanup_resources()`,
  `shutil.rmtree` of the socket directory.
- Kernel death watch: one task per context awaiting
  `km.provisioner.wait()`; when it returns outside `destroy`/`restart`, the
  context emits `error{KernelDied}` + `end{0}` for a running execution,
  `kernel_died{context_id, exit_code}`, and **restarts the kernel
  itself** (fresh connection file, same cwd/envs) so a context always
  converges back to a live kernel; `execute` on a context in
  `Starting`/`Restarting` waits for it (≤ `CONTEXT_READY_TIMEOUT = 30 s`,
  then reply `kernel_dead`).

Alternatives considered: sidecar as root with `Popen(user=1000)` for the
kernels (rejected: root-owned `0600` connection files the kernel cannot
read, and a root process parsing sandbox-influenced output); kernels via
the `ProcessService` registry (rejected: `commands.list()` would show
kernels and `commands.kill` could race the sidecar; kernels are the
sidecar's, `ps` shows them anyway); `IPYTHONDIR` pointing at a read-only
profile under `/opt` (rejected: IPython creates and chmods profile
subdirectories at startup; the explicit `--config` + `exec_files` route
has no write-path assumptions and the user's `~/.ipython` is created at
build time by the first kernel, inside the snapshot).

### D5. Executions: the E2B mapping (`rayito_kernel_sidecar/executions.py`)

`execute` handling in the sidecar, one task per request:

1. Look the context up (`not_found`), wait for `Ready` (≤ 30 s, else
   `kernel_dead`), then `async with context.lock` (FIFO; a queued execution
   can be cancelled by `interrupt{execution_id}` and then emits
   `end{execution_count: 0}` only — `rayd` decides the error, D7).
2. If `envs` is non-empty: silent cell `import os as _o; _p = {k:
   _o.environ.get(k) for k in <keys>}; _o.environ.update(<envs>); del _o`
   (`silent=True, store_history=False`; its iopub traffic is filtered out
   by parent `msg_id`), and after the user cell a silent restore cell that
   puts back the previous values and deletes the keys that did not exist.
3. `msg_id = kc.execute(code, store_history=True, allow_stdin=False,
   stop_on_error=True)`; then consume `kc.get_iopub_msg()` and
   `kc.get_shell_msg()` concurrently, keeping only messages whose
   `parent_header.msg_id == msg_id`:
   - `execute_input` → `started{execution_count}`.
   - `stream` → `stdout`/`stderr` by `content.name`, text split into
     ≤ 64 KiB chunks, `timestamp_unix_ns = time.time_ns()`.
   - `display_data` / `execute_result` → `result{is_main_result:
     msg_type == "execute_result", mime}` where `mime` maps every entry of
     `content.data`: values that are `str` pass through, anything else is
     `json.dumps(value, default=<numpy scalar → .item(), else str>)`. A
     value longer than `MAX_MIME_VALUE_BYTES = 8 MiB` is omitted and
     recorded as `mime["rayito/omitted"] = "<mime>: <n> bytes"`; then,
     while the bundle as a whole exceeds `MAX_RESULT_BYTES = 12 MiB`
     (several large representations of one object, e.g. `image/png` +
     `image/jpeg`), the largest remaining value is omitted the same way,
     so one `result` always fits one line (D2).
     `update_display_data` is treated as `display_data`. `clear_output` is
     ignored (E2B ignores it too).
   - `error` → `error{name: ename, value: evalue, traceback: [strip_ansi(l)
     for l in traceback]}` (the config already disables colour; the regex
     `\x1b\[[0-9;]*[A-Za-z]` is defence in depth).
   - shell `execute_reply`: remember `content.execution_count`;
     `status == "abort"` or `"aborted"` → `error{ExecutionAborted, "the
     kernel aborted the request before running it"}`; `status ==
     "error"` adds nothing (the iopub `error` already carried it).
   - iopub `status{execution_state: "idle"}` for this parent → mark idle.
   - `end{execution_count}` is emitted when **both** the `execute_reply`
     and the `idle` status have been seen (either order), with
     `execution_count` from the reply (fallback: `execute_input`'s, else
     0). Everything after `end` for this `msg_id` is dropped.
4. If the kernel dies mid-execution (D4 watch): `error{KernelDied, "kernel
   process exited (code n)"}` + `end{0}`. If the context is restarted or
   destroyed while executions are running or queued: each gets
   `error{KernelRestarted | ContextDestroyed}` + `end{0}` before the reply
   to the `restart_context`/`destroy_context` op.

`rayd-core::code::execution::ExecutionTracker` mirrors this on the agent
side and produces the domain `ExecuteOutput` events (`Started`, `Stdout`,
`Stderr`, `Result`, `Error`, `End`, each with its `seq`): `on_event(&
SidecarEvent) -> Vec<ExecuteOutput>` assigns `seq`, drops events after
`End`, drops a kernel `Error` once `timed_out` is set and, on `End` after a
timeout, prepends `Error{ExecutionTimeout}` (D7). `ExecuteOutput::Result`
carries `ResultBundle{is_main_result, text, html, markdown, latex, json,
javascript, png, jpeg, svg, pdf, chart, data, extra}` built by
`ResultBundle::from_mime(BTreeMap<String, String>)` with the mapping
`text/plain → text`, `text/html → html`, `text/markdown → markdown`,
`text/latex → latex`, `application/json → json`, `application/javascript →
javascript`, `image/png → png`, `image/jpeg → jpeg`, `image/svg+xml →
svg`, `application/pdf → pdf`, `e2b/chart → chart`, `e2b/data → data`,
anything else → `extra` (unit tested per mime type, including
`rayito/omitted` landing in `extra`).

### D6. `Execute` in `rayd`: stream, backpressure, cancellation

`CodeManager::execute(input: ExecuteInput{context_id: Option<String>,
code, timeout_ms, envs}) -> Result<ExecuteStream, CodeError>`:

1. Phase gate (`session.stream_gate()` → `NotAcceptingStreams` →
   `UNAVAILABLE`), sidecar link present and ready (D9; a context in
   `Starting`/`Restarting` is awaited ≤ 30 s, then `KernelNotReady`),
   context lookup (`ContextNotFound`), code size, envs validation.
2. `execution_id` from `getrandom`; `tokio::sync::mpsc::channel::<
   SidecarEvent>(EXECUTE_QUEUE_CAPACITY = 256)` registered in the
   supervisor's dispatch table under the request `id`; `execute` op sent
   through the link.
3. `ExecuteStream` (hand-written `Stream`, fields in drop order: receiver,
   tracker, timeout schedule, supervisor handle, in-flight guard) maps each
   `SidecarEvent` through the tracker into `ExecuteOutput`s, runs the D7
   timeout schedule, ends after `End`, and on `Drop` before `End` sends
   `interrupt{context_id, execution_id}` and unregisters the channel
   (events that arrive later are counted and dropped by the dispatcher).
   Every poll drains what the sidecar already queued before it consults
   the timers: a cell that ended inside its timeout is never rewritten
   into `ExecutionTimeout`, nor its context restarted, only because the
   client (HTTP/2 window, a slow callback) read the `End` late.
   `grpc/code.rs` wraps it in `KeepAliveStream` at
   `StreamSettings.execute_keepalive_interval` (5 s, shrunk in tests).
4. Backpressure: the dispatcher `try_send`s into the per-execution channel;
   when it is full it waits up to `STALL_TIMEOUT = 30 s` (`send_timeout`),
   during which the sidecar's stdout is not read (the pipe fills, the
   sidecar's writer blocks, the kernel's ZMQ buffers absorb the rest). If
   the client still has not drained after 30 s the dispatcher unregisters
   the execution, and the stream emits `Error{OutputTruncated, "client did
   not consume output for 30 s"}` + `End{0}` and closes; the execution
   keeps running to completion in the kernel and its later events are
   dropped. Memory per `Execute` stream ≤ 256 events × 64 KiB text (mime
   payloads up to 12 MiB are the exception, one per `result`). On the
   sidecar side the per-context inbox between the ZMQ channel pumps and
   the running cell is bounded too (`INBOX_CAPACITY = 64` channel
   messages; the `died`/`abort` sentinels never wait), so a stalled
   `emit` stops the pumps and the kernel's ZMQ high-water marks hold the
   rest instead of the sidecar's heap.
   Known consequence: while one stream stalls, every other context's
   output shares the same stdout pipe and waits; documented in
   `ARCHITECTURE.md`, a per-context channel is an M6 option. Replies
   wait behind the stall too, so op timeouts that expire while the
   dispatcher is parked (`dispatch_blocked`) are logged but never counted
   towards the three-timeouts kill switch of D8, and the counter restarts
   when the stall ends.
5. `in_flight` counter per context (registry) so `RestartContext` and
   `DestroyContext` know whether to expect synthetic ends.

### D7. Timeout: interrupt, then restart

`rayd-core::code::timeout::plan_timeout(timeout_ms: u64) ->
Option<TimeoutSchedule{interrupt_at: Duration, restart_at: Duration}>`
with `restart_at = interrupt_at + INTERRUPT_GRACE (5 s)`; `0` → `None`.
`ExecuteStream` arms two `tokio::time::Sleep`s from the moment
`CodeManager::execute` accepted the request:

- `interrupt_at`: `tracker.mark_timed_out()`; send `interrupt{context_id,
  execution_id}`. From now on every kernel `error` for this execution is
  dropped; the `end` that follows (the kernel raised `KeyboardInterrupt`
  and went idle, or the queued execution was removed) is preceded by one
  `Error{name: "ExecutionTimeout", value: "execution exceeded <timeout_ms>
  ms", traceback: []}`. The context keeps its state: a `time.sleep(10)`
  interrupted at 2 s leaves `x` alive.
- `restart_at` (only if `End` has not arrived): send `restart_context{
  context_id, envs}`; the sidecar cancels the execution with `error{
  KernelRestarted}` + `end{0}`, which the tracker rewrites into
  `Error{ExecutionTimeout}` + `End{0}`; other in-flight executions of that
  context get `Error{KernelRestarted}` + `End{0}` unchanged. State is lost
  (a cell stuck in a C extension is the only way to reach this branch).
- Both timers use `tokio::time` (monotonic). Q19 says the monotonic clock
  advances across suspend, so a paused sandbox may see a spurious
  interrupt after resume; M5's `/resume` checklist rearms deadlines. M4
  documents it in `ARCHITECTURE.md`.

The SDK's gRPC deadline for the stream is `timeout + 15 s` (D12), so the
server's own end always arrives first.

### D8. Sidecar supervisor, readiness and crash handling

`rayd::code::supervisor::SidecarSupervisor` owns one sidecar instance at a
time behind the `KernelSidecar` port:

```rust
pub trait KernelSidecar: Send + Sync {
    fn launch(&self, spec: &SpawnSpec, events: SidecarEventSink,
              exited: SidecarExitSink) -> Result<Box<dyn SidecarLink>, SpawnError>;
}
pub type SidecarEventSink = Box<dyn Fn(SidecarEvent) + Send + Sync>;
pub type SidecarExitSink  = Box<dyn FnOnce(Option<i32>) + Send>;
pub trait SidecarLink: Send + Sync {
    fn pid(&self) -> Pid;
    fn send(&self, line: &str) -> Result<(), SidecarIoError>;   // enqueue one encoded request
    fn kill(&self);                                             // SIGKILL the sidecar's process group
}
pub trait KernelStatus: Send + Sync {                           // read by HealthGrpc and the hooks
    fn kernel_ready(&self) -> bool;
}
pub enum SidecarIoError { Closed, QueueFull }
```

`TokioSidecarLauncher` (`cfg(unix)`, `adapters/sidecar_process.rs`) spawns
with `tokio::process::Command` + the M2 `PreExecPlan` (made `pub(crate)`),
pipes stdin/stdout/stderr, runs a writer task draining an
`mpsc::channel(1024)` of lines into stdin (`QueueFull` when the sidecar
stops reading), a reader task decoding stdout with `LinesCodec` (16 MiB)
into `decode_event` and the sink (a `ProtocolError` is logged with the
line length only and counts as a fatal fault: `kill()`), a stderr task
that parses the sidecar's JSON log lines (`{"level", "msg", <allowlisted
fields>}`) and re-emits them through `tracing` at the same level with only
the allowlisted fields (any line that is not that JSON schema is counted in
`sidecar_stderr_lines` and dropped: never echoed), and a wait task calling
`exited(status.code())`. `UnsupportedSidecarLauncher` otherwise.

`rayd-core::code::readiness::SidecarState` (pure machine, unit tested):
`Disabled` (no sidecar configured) → `Starting{attempt, since}` →
`Warming{attempt}` (process up, `ready` not yet seen) → `Ready` (default
context warm) → `Rotating` (D11 restart in progress) → `Ready`;
`Exited{attempt, backoff_until}` on process exit from any state;
`restart_delay(attempt) = min(0.5 s × 2^(attempt-1), 30 s)` with no
attempt cap; `kernel_ready(&self) -> bool` is `true` only in `Ready`;
`ready_hook_decision(state, boot_elapsed: Duration) -> ReadyDecision{Ok,
Retry, Escape}`: `Ok` when `Disabled` or `Ready`, `Escape` when
`boot_elapsed ≥ READY_ESCAPE = 300 s`, else `Retry`.

Supervisor loop: launch → wait for `ready` (≤ `SIDECAR_READY_TIMEOUT =
180 s`, else `kill` and count as an exit) → serve. On exit: every pending
reply resolves `SidecarUnavailable`, every registered execution channel
receives a synthetic `error{KernelDied, "the kernel sidecar exited"}` +
`end{0}`, the registry's `kernel_pids()` each get `SIGKILL` on their
process group (kernels start in their own session, so they survive their
parent's death otherwise), the registry is cleared (user contexts are
gone: their ids answer `NOT_FOUND` from now on), `kernel_ready` drops, the
backoff sleeps, and the loop relaunches. The new sidecar recreates the
default context on its own (warm, no envs); if `/run` already happened,
`rayd` sends `restart_context{"default", envs: <payload envs>}` right
after the new `ready` and only then enters `Ready` (the same rotation as
D11, so a relaunched sandbox never serves a kernel without the payload
environment). Three consecutive op timeouts (D10) also `kill()` the
sidecar, except the ones that expire while the dispatcher is parked on a
stalled `Execute` client (D6): those are a consequence of the design, not
an unresponsive sidecar. Restarts are logged with `attempt`, `backoff_ms`,
`exit_code`, `sidecar_restarts`.

Ops other than `execute` go through `SidecarSupervisor::call(op,
timeout) -> Result<ReplyPayload, CodeError>` (oneshot per `id`): `ping`
5 s, `create_context` 60 s, `restart_context` 60 s, `destroy_context`
15 s, `interrupt` 5 s, `list_contexts` 5 s, `reseed` 15 s, `quiesce` 5 s,
`resume` 50 s. `rayd` sends no periodic `ping` in M4 (the pipe's EOF is
the liveness signal); `ping` exists for `scripts/hooks-sim.py` and M5.

### D9. `Health.kernel_ready`, `/ready`, `/validate`

- `kernel_ready = supervisor.state().kernel_ready()`, i.e. the sidecar is
  up, has reported `ready`, the default context is not restarting (D11)
  and no relaunch is in progress. `HealthGrpc` gets an `Arc<dyn
  KernelStatus>` (implemented by `CodeManager`) and fills the builder's
  `kernel_ready`; `kernel_state_lost` stays `false`.
- `/ready`: `ready_hook_decision(state, boot_elapsed)`: `Retry` → **503**
  immediately with body `{"hook":"ready","outcome":"kernel_warming",…}`
  and no phase transition (AWS retries); `Ok` → the existing transition +
  200; `Escape` → transition + 200 with `outcome: "ready_escape"` and a
  `tracing::error!` (the snapshot will carry a kernel that never warmed;
  `kernel_ready` stays honest and the e2e catches it). With
  `--no-sidecar` the state is `Disabled` and `/ready` behaves as in M3.
- `/validate`: the first call starts `CodeManager::validate()` (a
  background task executing `VALIDATE_CELL` in the default context with
  `timeout_ms = 60 000`, bypassing the phase gate because the VM is in
  `Ready`, not `Running`) and answers **503** with `outcome:
  "validating"`; further calls answer 503 while it runs and **200** once
  it finished, with `outcome: "validated"` or `"validate_failed"` (the
  error name is logged; the build is not failed on purpose: `/validate`
  is a prefetch hint on a throwaway VM and the e2e is the real gate).
  `VALIDATE_CELL` = `import pandas as pd, numpy as np; import
  matplotlib.pyplot as plt; df = pd.DataFrame({"x": np.arange(50), "y":
  np.random.default_rng(0).random(50)}); df.describe(); plt.plot(df.x,
  df.y); plt.show(); df` — the same paths the acceptance test exercises
  (DataFrame `e2b/data`, PNG, `e2b/chart`).
- `create()`/`connect()` in the SDK poll until `agent_ready and
  kernel_ready` (D12). `is_running()` stays `agent_ready`.

### D10. gRPC status mapping (server side)

| Domain error | gRPC status |
|---|---|
| `InvalidContextId`, `CodeTooLarge{max}`, `InvalidLanguage`, `InvalidCwd`, invalid `envs`, sidecar reply `invalid_argument` | `INVALID_ARGUMENT` |
| `ContextNotFound`, sidecar reply `not_found` | `NOT_FOUND` |
| `DefaultContextProtected` | `FAILED_PRECONDITION` |
| `TooManyContexts{max}` | `RESOURCE_EXHAUSTED` |
| `KernelNotReady{reason}` (no sidecar, sidecar relaunching, context not ready within 30 s, sidecar reply `kernel_dead`), `SidecarUnavailable` (op timeout or link closed), `NotAcceptingStreams{phase}` | `UNAVAILABLE`; message `"kernel not ready: <reason>"` for the kernel cases (the SDK's kernel-gate rule keys on the `kernel not ready` prefix, D12), the phase for the gate as in M2 |
| sidecar reply `busy` | `ABORTED` ("context is being restarted; retry") |
| `Unsupported` (non-unix host) | `UNIMPLEMENTED` |
| `Reattach` | `UNIMPLEMENTED` ("Reattach arrives in M5") |
| `SidecarProtocol`, `Internal` | `INTERNAL` (message without payloads) |
| missing/invalid `x-access-token` | `UNAUTHENTICATED` (layer, unchanged) |

`Execute` statuses apply only before the first message; after `started`
the only outcomes are the in-stream `ExecutionError`s and a clean end
(there is no trailing error status in M4).

### D11. Snapshot uniqueness: `/run` rotation and `/resume` reseed

Rule 3 of `ARCHITECTURE.md` "Qué captura el snapshot": the pre-warmed
kernel's HMAC key, PRNG state and everything else in its memory are
cloned into every sandbox of a version. Decision: **`/run` restarts the
default kernel**, in a background task spawned after the hook's 200 (the
hook budget is < 2 s; the restart takes seconds):

1. `hooks::run` → `session.run(...)` → on `RunOutcome::Installed` (only the
   first `/run` of the boot) → `code.spawn_run_rotation(defaults.envs)`.
2. `SidecarState::Rotating` (`kernel_ready=false`); `restart_context{
   "default", envs: payload envs}`; the new kernel gets a new connection
   file (new key), a fresh process (Python's `random` and `numpy.random`
   seed themselves from `os.urandom` at import: nothing to reseed), the
   payload `envs`, and runs the startup scripts again (warm-up from the hot
   page cache); when its reply arrives → `Ready`. Duration logged as
   `restart_ms` and measured end to end by the e2e as `run-microvm →
   kernel_ready` (budget D15).
3. Contexts other than the default cannot exist before `/run` (nothing
   accepts `CreateContext` before the token is installed), so restarting
   the default is the whole rotation.

`/resume` (M4 part of the checklist, the rest is M5): `code.spawn_resume_
reseed()` → `reseed` op → the sidecar runs, in every live kernel, the
silent cell `import random as _r; _r.seed()\nimport sys as _s\nif
"numpy" in _s.modules: _s.modules["numpy"].random.seed()\ndel _r, _s`
and replies with the lists of reseeded/failed contexts (logged as counts).
The M4 e2e does not pause/resume; the reseed op is covered by the
sidecar's real-kernel test (the value of `random.random()` differs before
and after `reseed`).

Alternative considered: keep the warm kernel and accept the shared key
(the sockets are `ipc://` under a `0700` directory, only reachable from
inside the same VM by the same uid, so a shared key gives an attacker
nothing across VMs). Rejected for M4 because `ARCHITECTURE.md` rule 3 and
`SECURITY.md` T5 are written, the payload `envs` need a kernel restart
anyway, and the cost is what M4 exists to measure. If the e2e measures
`run-microvm → kernel_ready` above **8 s p50**, the rotation is kept and
an ADR is opened for M6 with the numbers (options: overlap the restart
with `/run` by pre-spawning a second kernel before `/ready`, or drop the
rotation on the shared-key argument); M4 does not change behaviour on
its own.

### D12. SDK modules, models and signatures

New/changed modules in `clients/python/src/rayito/`:

- `_models.py` adds:
  - `CodeContext(id: str, language: str, cwd: str)` (frozen).
  - `OutputMessage(line: str, timestamp: int, error: bool)` (frozen;
    `timestamp` unix ns; `error=True` for stderr; `__str__` → `line`).
  - `Logs(stdout: list[str], stderr: list[str])` (mutable dataclass, one
    entry per `OutputChunk`).
  - `ExecutionError(name: str, value: str, traceback: str)` (frozen;
    `traceback` is the proto's lines joined with `"\n"`).
  - `Result`: `text, html, markdown, svg, png, jpeg, pdf, latex, javascript:
    str | None`, `json: Any` (parsed; the raw string kept when `json.loads`
    fails), `data: Any` (parsed `e2b/data`, usually `dict[str, list]`),
    `chart: Chart | None`, `is_main_result: bool`, `extra: dict[str, str]`,
    `raw: dict[str, str]` (mime → string as received, for `formats()` and
    `to_json`); `formats() -> list[str]` (names of the non-`None` fields in
    the order above plus `extra` keys), `__str__` → `text or ""`,
    `__repr__` → `Result(formats=[…], is_main_result=…)`, `_repr_html_`,
    `_repr_markdown_`, `_repr_svg_`, `_repr_png_`, `_repr_jpeg_`,
    `_repr_pdf_`, `_repr_latex_`, `_repr_json_`, `_repr_javascript_`
    returning the field (base64 strings for images, as E2B).
  - `Execution(results: list[Result], logs: Logs, error: ExecutionError |
    None, execution_count: int | None)` with `text` property (the `text`
    of the result whose `is_main_result` is true, else `None`) and
    `to_json()`.
- `_charts.py` (pure): `ChartType(StrEnum)` = `LINE`, `SCATTER`, `BAR`,
  `PIE`, `BOX_AND_WHISKER`, `SUPERCHART`, `UNKNOWN`; `ScaleType(StrEnum)`
  = `LINEAR`, `DATETIME`, `CATEGORICAL`, `LOG`, `SYMLOG`, `LOGIT`,
  `FUNCTION`, `FUNCTIONLOG`, `ASINH`; `Chart(type, title, elements)`,
  `Chart2D(Chart)(x_label, y_label, x_unit, y_unit)`, `PointData(label,
  points: list[tuple[float | str, float | str]])`,
  `PointChart(Chart2D)(x_ticks, x_tick_labels, x_scale, y_ticks,
  y_tick_labels, y_scale, elements: list[PointData])`,
  `LineChart(PointChart)`, `ScatterChart(PointChart)`, `BarData(label,
  group, value)`, `BarChart(Chart2D)(elements: list[BarData])`,
  `PieData(label, angle, radius, autopct)`, `PieChart(Chart)(elements:
  list[PieData])`, `BoxAndWhiskerData(label, min, first_quartile, median,
  third_quartile, max, outliers)`, `BoxAndWhiskerChart(Chart2D)(elements)`,
  `SuperChart(Chart)(elements: list[Chart])`; `parse_chart(document: str |
  dict) -> Chart` dispatching on `type` (unknown → `Chart(type=UNKNOWN,
  title, elements=[])`, never raises on missing keys: defaults `None`/`[]`).
  Exported from `rayito`.
- `_code_base.py` (pure, shared):
  - `DEFAULT_CODE_TIMEOUT_SECONDS = 300.0`, `CODE_STREAM_GRACE_SECONDS =
    15.0`, `MAX_CODE_BYTES = 1_048_576`, `MAIN_RESULT_MIME = "text/plain"`.
  - `validate_code(code: str) -> str` (must be `str`, ≤ 1 MiB UTF-8),
    `resolve_context_id(context: CodeContext | str | None) -> str | None`,
    `validate_language(language: str | None) -> str` (`None`/`""`/
    `"python"` → `"python"`, else `InvalidArgumentException`).
  - `build_execute_request(code, context_id, envs, timeout) ->
    code_pb2.ExecuteRequest` (`timeout_to_ms` reused from `_process_base`),
    `build_create_context_request(language, cwd, envs)`.
  - `execute_deadline(timeout: float | None) -> float | None` =
    `timeout + 15` when `timeout` > 0 else `None`.
  - `result_from_proto(result: code_pb2.ExecutionResult) -> Result`
    (parses `json`, `data`, `chart`), `error_from_proto`,
    `context_from_proto`.
  - `ExecutionBuilder(on_stdout, on_stderr, on_result, on_error)`:
    `feed(event: code_pb2.ExecuteEvent) -> bool` (returns `True` on
    `end`): `keepalive` ignored; `started` → `execution_count`; `stdout`/
    `stderr` → `logs` + callback with `OutputMessage`; `result` →
    `results` + callback; `error` → `execution.error` + callback; `end` →
    `execution_count` if non-zero; a second `started` or any event after
    `end` raises `SandboxException("protocol violation")`; callback
    exceptions propagate to the caller (E2B behaviour). `finish() ->
    Execution` (raises `SandboxException` if the stream ended without
    `end`).
- `_transport.py`: `KERNEL_GATE_PREFIX = "kernel not ready"`,
  `is_kernel_gate(exc)` (`UNAVAILABLE` whose details start with the
  prefix); `is_stream_reset` returns `False` for it; `translate_rpc_error`
  maps it to `SandboxException(f"el kernel no está listo: {details}")`.
  No other table changes.
- `sandbox_sync/code.py`:

```python
class CodeClient:                       # internal; the public surface lives on Sandbox
    def __init__(self, sandbox: Sandbox) -> None: ...
    def run_code(self, code: str, *, context: CodeContext | str | None = None,
                 on_stdout: Callable[[OutputMessage], None] | None = None,
                 on_stderr: Callable[[OutputMessage], None] | None = None,
                 on_result: Callable[[Result], None] | None = None,
                 on_error: Callable[[ExecutionError], None] | None = None,
                 envs: Mapping[str, str] | None = None,
                 timeout: float | None = 300, request_timeout: float | None = None) -> Execution
    def create_context(self, *, cwd: str | None = None, language: str | None = None,
                       envs: Mapping[str, str] | None = None,
                       request_timeout: float | None = None) -> CodeContext
    def list_contexts(self, *, request_timeout: float | None = None) -> list[CodeContext]
    def remove_context(self, context: CodeContext | str, *, request_timeout=None) -> None
    def restart_context(self, context: CodeContext | str, *, request_timeout=None) -> None
```

  `Sandbox.run_code`, `Sandbox.create_code_context`,
  `Sandbox.list_code_contexts`, `Sandbox.remove_code_context`,
  `Sandbox.restart_code_context` delegate to it with the same signatures
  (E2B has them on the sandbox root, so does `SPEC.md`).
- `sandbox_async/code.py`: `AsyncCodeClient` with the same names as
  coroutines over `grpc.aio`; callbacks stay synchronous callables (E2B
  parity; an `async` callback is not awaited, documented).
- `sandbox_sync/main.py` / `sandbox_async/main.py`: `_code` stub on the
  unary channel registered in `_unary_stubs`; `_wait_until_ready` requires
  `response.agent_ready and response.kernel_ready`; the `run_code`
  `NotImplementedError` stub disappears (`pty` keeps it).
- `__init__.py` exports `CodeContext`, `Execution`, `ExecutionError`,
  `Logs`, `OutputMessage`, `Result`, `Chart`, `ChartType`, `LineChart`,
  `ScatterChart`, `BarChart`, `PieChart`, `BoxAndWhiskerChart`,
  `SuperChart`, `PointData`, `BarData`, `PieData`, `BoxAndWhiskerData`,
  `ScaleType`.

Semantics fixed here:

- Channels: `run_code` opens `Execute` on the **unary** channel and consumes
  it in the calling thread (like foreground `commands.run`); the four
  context RPCs are unary on the same channel. The stream channel stays for
  background work (M5's `Reattach` may move there).
- Deadlines: context RPCs use `request_timeout` or the sandbox's 60 s
  (`create_context` and `restart_context` default to **90 s**, a kernel
  start may take seconds on a cold page cache); `Execute` uses
  `execute_deadline(timeout)`; `timeout=None` or `0` → no deadline and
  `timeout_ms = 0`.
- Proxy 403 on `Execute` is retried once **only before the first
  message** (`_open_stream`), like every other stream; a 403 after
  `started` cannot happen (the proxy evaluates the token when the
  connection opens).
- `run_code` never raises for a kernel-level error: `Execution.error` is
  data (`ZeroDivisionError`, `ExecutionTimeout`, `KernelDied`…). It raises
  `NotFoundException` (unknown context), `InvalidArgumentException`,
  `SandboxException` (kernel gate, protocol violation), `TimeoutException`
  (client deadline, which the server's own end normally precedes),
  `AuthenticationException`, and the M2 stream-reset classification.
- `remove_code_context` on the default context raises
  `InvalidArgumentException` (`FAILED_PRECONDITION`); on an unknown id
  `NotFoundException`.
- `Execution.text` is `None` when no `execute_result` arrived (a cell that
  only prints).

### D13. Tests

**`rayd-core` (host, Windows and Linux)** — `cargo test -p rayd-core`:
`protocol` golden encode/decode against `kernel-sidecar/tests/fixtures/
protocol_v1.jsonl` (one line per op and per event, unknown event →
`ProtocolError`, unknown fields ignored, missing required field);
`ContextRegistry` (default first in `list`, 9th → `TooManyContexts`,
`remove("default")` refused, `kernel_pids`, `clear`); `ContextId`
validation; `ResultBundle::from_mime` for every mime type + `extra` +
`rayito/omitted`; `ExecutionTracker` (seq numbering, keepalive not
counted, events after `End` dropped, kernel `Error` dropped after
`mark_timed_out`, `ExecutionTimeout` prepended to `End`, `KernelDied`
synthetic); `plan_timeout` (0 → `None`, interrupt/restart offsets, clamp
at 8 h); `SidecarState` (transitions, `restart_delay` sequence 0.5, 1, 2,
4, 8, 16, 30, 30…, `kernel_ready` only in `Ready`, `ready_hook_decision`
incl. the 300 s escape and `Disabled`); `sidecar_spawn_spec` (program,
args, env from scratch with `PYTHONPATH`/`JUPYTER_PATH`, identity, cwd);
`run_rotation_plan` and `reseed_cell` constants; `CodeError` `Display`
never quotes code, envs or ids.

**`rayd` integration, `cfg(unix)`** — `crates/rayd/tests/m4_code.rs`
(`#![cfg(unix)]`, in-process routers on `127.0.0.1:0`, `/run` installed
through the hooks router, `IdentitySwitch::KeepCurrent` when not root,
`--sidecar-cmd python3 tests/fixtures/fake_sidecar.py` — a **stdlib-only
fake sidecar** speaking protocol v1 with scripted behaviours: `x = 42` /
`x` / `print(x)` / `1/0` / `plot` / `df` mirror the SDK fake (D13 Python),
`sleep <s>` obeys `interrupt`, `hang <s>` ignores `interrupt` (exercises
the restart branch), `die` exits the fake sidecar with code 3, `kernel-die`
emits `kernel_died` then restarts the context, `big <n>` emits `n` stdout
chunks of 64 KiB, `omit` returns `rayito/omitted`; a `--warmup-ms` flag
delays `ready`; `execute_keepalive_interval` 200 ms, `stall_timeout` 1 s,
`interrupt_grace` 1 s injected through `CodeSettings`/`StreamSettings`):
`/ready` answers 503 while the fake delays `ready` and 200 after;
`Health.kernel_ready` false → true; `/validate` 503 then 200 with
`validated`; `/run` triggers `restart_context("default", envs)` on the
fake (asserted through its stdout log file) and `kernel_ready` dips then
recovers; `Execute` event order and `seq` (1..n, keepalive 0) for the
scripted cells; empty `context_id` → default; unknown → `NOT_FOUND`;
code > 1 MiB → `INVALID_ARGUMENT`; `timeout_ms=500` on `sleep 5` →
`ExecutionTimeout` + `End{0}` within 2 s and the fake received
`interrupt`; `timeout_ms=500` on `hang 5` → `restart_context` received,
`ExecutionTimeout`, `End{0}`; client drops the stream mid-`sleep` → the
fake receives `interrupt` within 1 s; keepalive at 200 ms on a silent
cell; `big 2000` with a client that stops reading → `OutputTruncated` +
`End{0}` after `stall_timeout`; concurrent `Execute` on two contexts
interleave (both `started` before either `end`); `CreateContext` × 8 then
the 9th → `RESOURCE_EXHAUSTED`; `ListContexts` order; `DestroyContext`
default → `FAILED_PRECONDITION`, user context → gone from `ListContexts`,
`Execute` on it → `NOT_FOUND`; `RestartContext` while a cell runs →
`KernelRestarted` + `End{0}`; `die` → executions get `KernelDied`,
`kernel_ready` false, the fake is relaunched after the backoff, user
contexts answer `NOT_FOUND`, the default works again; `kernel-die` →
`KernelDied` on the running execution and the next `Execute` works;
`Reattach` → `UNIMPLEMENTED`; `Execute` while `/suspend` was acknowledged
→ `UNAVAILABLE`; `--no-sidecar` → `/ready` 200 immediately,
`kernel_ready` false, `Execute` → `UNAVAILABLE` with the `kernel not
ready` prefix. One `#[ignore]` test `real_sidecar_round_trip` runs the
real `kernel-sidecar` (`RAYITO_SIDECAR_ROOT` set, pins installed; the
Docker loop) and asserts `x = 42` / `x` → `42`, a PNG + chart result and
`1/0` → `ZeroDivisionError`. `m1_hello.rs`, `m2_process.rs`,
`m3_filesystem.rs` adapt to the new `router`/`hooks::router` signatures
with `CodeManager::disabled()`.

**Sidecar** — `kernel-sidecar/tests/`: `test_protocol.py` (host: golden
fixtures, chunking of stream text at 64 KiB on UTF-8 boundaries, ANSI
strip, mime serialisation with numpy scalars and the 8 MiB omission,
`kernel_died` event shape); `test_server.py` (host: the request dispatcher
against a `FakeKernelContext` — queueing under the lock, `interrupt` of a
queued execution, `restart` cancelling in-flight executions with
`KernelRestarted`, `destroy` with `ContextDestroyed`, `ready` emitted once,
unknown op → `reply{error: invalid_argument}`); `test_kernel.py` (marker
`kernel`, Linux only, real `ipykernel` with the pinned requirements: the
startup scripts run (`e2b/chart` and `e2b/data` mime types appear for a
figure and a DataFrame, `_repr_png_` for a PIL image), `x = 42` then `x`
→ `text/plain "42"` with `is_main_result`, `print(x)` → `stdout` and no
`result`, `1/0` → `error{ZeroDivisionError}` with a non-empty traceback
without ANSI, `execute_count` increments, per-execution `envs` visible in
`os.environ` during the cell and gone after, `interrupt` of
`time.sleep(10)` ends within 2 s with the kernel alive and `x` intact,
`restart_context` yields a different `kernel_pid` and a different HMAC
key in the connection file and loses `x`, `reseed` changes the next
`random.random()`, `resume` reports every context alive, `kernel_died`
after `os._exit(0)` in a cell followed by a working execution, warm-up
`warmup_ms` logged). `uv run --with-requirements requirements.txt pytest`
in the CI `sidecar` job (ubuntu-24.04, Python 3.12; the sidecar is pure
Python so x86 wheels are fine); `ruff check`, `ruff format --check`,
`mypy src` clean.

**Python SDK unit** — `clients/python/tests/unit/fake_code.py`:
`FakeCodeService` registered by the `fake_rayd` fixture as
`RaydEndpoint.code` (every RPC checks `x-access-token` and records the
client port): contexts (`default` + `ctx-<n>`), `CreateContext` validates
language, 9th → `RESOURCE_EXHAUSTED`; `DestroyContext("default")` →
`FAILED_PRECONDITION`; `Execute` on an unknown context → `NOT_FOUND`;
`Execute` interprets scripted code: `x = 42` → `started(1)` + `end(1)`;
`x` → `result{text:"42", is_main_result}`; `print(x)` → `stdout "42\n"`;
`1/0` → `error{ZeroDivisionError, "division by zero", 3 lines}`; `plot` →
`result{png: <1×1 PNG base64>, chart: <line chart JSON>}` + `keepalive`
first; `df` → `result{text, html, data: {"a":[1,2]}}`; `stderr` →
`stderr` chunk; `many` → three results, the second main; `sleep` with
`timeout_ms > 0` → `ExecutionTimeout` after `min(timeout, 0.2 s)`;
`kernel-die` → `KernelDied` + `end(0)`; `bad-json` → `result{json:
"{not json"}`; `omitted` → `extra{"rayito/omitted": …}`; `envs-echo` →
`stdout` with the request's `envs` as JSON (so the SDK's `envs` are
asserted); records `timeout_ms`. Tests (`test_code_base.py`,
`test_charts.py`, `test_code_sync.py`, `test_code_async.py`):
`execute_deadline` (300 → 315, `None`/0 → `None`), `validate_code`,
`resolve_context_id` for `CodeContext`/`str`/`None`, request building
(`timeout_ms`, `envs`, empty `context_id`), `result_from_proto` for every
field incl. parsed `json`/`data`, raw string kept on bad JSON, `chart`
parsing for line/scatter/bar/pie/box/superchart/unknown fixtures (`tests/
unit/fixtures/charts/*.json`), `formats()` order, `_repr_*_`,
`ExecutionBuilder` (callbacks with `OutputMessage`, `error` stored not
raised, keepalive ignored, protocol violations), `Execution.text`,
`to_json`; against the fake: the acceptance sequence (`x = 42` / `x` /
`print(x)` / `1/0` / `plot` / `df`), `timeout=1` → `error.name ==
"ExecutionTimeout"` and `timeout_ms == 1000` recorded, callbacks called in
order, `envs` forwarded, `create_code_context` → `CodeContext`, `list`,
`remove` (default → `InvalidArgumentException`, unknown →
`NotFoundException`), `restart`, `Execute` uses the unary channel, 403
re-mint before the first message (Stubber expects one extra
`create_microvm_auth_token`), `create()` waits for `kernel_ready`
(`FakeRayd.kernel_not_ready_calls`), kernel-gate `UNAVAILABLE` →
`SandboxException` without a `Health` probe, async parity for every case.
`uv run pytest tests/unit`, `ruff check`, `ruff format --check`, `mypy
src` all clean.

**e2e (real AWS)** — `clients/python/tests/e2e/test_m4_code.py`; see
"Acceptance test list".

### D14. Image, packaging and dev loop

`image/Dockerfile` (M4):

- `dnf install` unchanged (`python3.12`, `python3.12-pip` already
  present); no compilers: every pin ships `manylinux_aarch64` cp312
  wheels. `python3.12 -m pip install --no-cache-dir -r
  /opt/rayito/sidecar/requirements.txt` as root into the system
  site-packages, then `python3.12 -m pip check`.
- `COPY kernel-sidecar/ /opt/rayito/sidecar/` (the zip excludes `tests`,
  `__pycache__`, `.venv`, `.pytest_cache`, `.ruff_cache`, `uv.lock`,
  `*.pyc` via `scripts/image_zip.py`'s new exclusion list; `make image-zip`
  copies `kernel-sidecar/` into `image/kernel-sidecar/` after `rayd`);
  `chmod -R a+rX /opt/rayito/sidecar`. `/opt/rayito` is already denied to
  `FilesystemService`.
- Build-time warm run as `user` so the snapshot's home already has the
  matplotlib font cache and the IPython profile directory: `su user -c
  "MPLCONFIGDIR=/home/user/.cache/matplotlib python3.12 -c 'import
  matplotlib.pyplot, pandas, numpy, scipy, sklearn, ipykernel,
  jupyter_client, zmq; import matplotlib.font_manager as fm;
  fm.fontManager'" && test -f /home/user/.cache/matplotlib/fontlist-*.json`
  and `su user -c "PYTHONPATH=/opt/rayito/sidecar/src python3.12 -c
  'import rayito_kernel_sidecar, rayito_kernel_sidecar._vendor.e2b_charts'"`.
- Sanity `RUN` extended with `python3 -m ipykernel --version` and the
  kernelspec file present.
- `CMD` unchanged (`rayd` as root; it spawns the sidecar as `user`). No
  `RAYITO_ALLOW_ROOT`.
- Publish with `image-publish` (new version, three-state gate). Record
  `snapshotBuild.memorySnapshotSizeInBytes`, `codeInstallSizeInBytes`,
  `diskSnapshotSizeInBytes` and the build time next to 4.0's
  (572 235 776 B / 436 166 656 B / 23 502 848 B, 123.9 s) in the tasks
  notes, `MILESTONES.md` and `AWS_API_NOTES.md` §16. Knob if the memory
  snapshot exceeds **1.2 GB** or `run-microvm → kernel_ready` exceeds the
  D15 budget: trim `0004_warmup.py` from the end (`sklearn` first, then
  `scipy`) and republish; the imports stay installed either way.

`kernel-sidecar/` layout: `pyproject.toml` (name `rayito-kernel-sidecar`,
`version = "0.0.4"`, `requires-python = ">=3.12"`, dev group `pytest`,
`ruff`, `mypy`, `uv_build`; **never published**), `requirements.txt` (the
exact pins of the proposal plus the exact `scipy`, `scikit-learn`,
`matplotlib-inline`, `pillow` versions resolved at task time and recorded
there, one `==` per line), `src/rayito_kernel_sidecar/{__init__, __main__,
protocol, kernels, executions, server, logging}.py`,
`src/rayito_kernel_sidecar/_vendor/e2b_charts/` (the `e2b_charts` package
from PyPI vendored verbatim with its `LICENSE` and a `VENDORED.md` naming
the version and sha256 of the sdist), `ipython/ipython_kernel_config.py`,
`ipython/startup/0001_charts.py … 0004_warmup.py`,
`jupyter/kernels/rayito/kernel.json`, `tests/`. `ruff` config mirrors the
client's; `mypy --strict` on `src` excluding `_vendor`.

`rayd` flags: `--sidecar-cmd "<program> <args…>"` (default `python3 -m
rayito_kernel_sidecar`), `--sidecar-root <dir>` (default
`/opt/rayito/sidecar`), `--socket-root <dir>` (default `/run/rayito/k`),
`--no-sidecar` (state `Disabled`: `/ready` immediate, `kernel_ready`
false, `CodeService` → `UNAVAILABLE`); the sidecar user is the M2 default
user (`user`, uid 1000) resolved through `NixUserLookup`, `KeepCurrent`
on non-root hosts.

`Makefile`: `image-zip` copies `kernel-sidecar/`; `test-sidecar` (`cd
kernel-sidecar && uv run --with-requirements requirements.txt pytest`);
`lint` adds `uvx ruff check kernel-sidecar`; `test` runs `test-sidecar`
after `test-python`; `dev-run` (`cargo run -p rayd -- --sidecar-root
kernel-sidecar` under WSL2 with the pins installed in
`kernel-sidecar/.venv`). `scripts/hooks-sim.py`: `READY_RETRY_BUDGET_S`
330 (covers the 300 s escape) and `--only validate` polls until 200 like
`/ready`. CI: `sidecar` job (ubuntu-24.04, `astral-sh/setup-uv`, Python
3.12, `make test-sidecar`, `ruff`, `mypy`); the `build` job zips the
sidecar into the artifact.

### D15. Performance budgets (checked in e2e/integration, logged, asserted where marked)

| Budget | Value | Where |
|---|---|---|
| `run-microvm` → `Health.kernel_ready` (includes the `/run` rotation) | ≤ 8 s p50 target, **asserted ≤ 15 s**, logged as `kernel_ready_s` | e2e |
| `restart_ms` of the default kernel in `/run` | logged in `rayd`; target ≤ 3 s | rayd log, recorded in `AWS_API_NOTES.md` §16 |
| first `run_code("x = 42")` after create | ≤ 2 s (asserted) | e2e |
| 20 sequential `run_code("1+1")` | total ≤ 15 s, p95 ≤ 0.75 s (asserted total, p95 logged) | e2e |
| matplotlib cell (`plt.plot` + `show`) | ≤ 5 s (asserted) | e2e |
| pandas `describe()` + `df` cell | ≤ 3 s (asserted) | e2e |
| `create_code_context()` | ≤ 8 s (asserted), logged | e2e |
| `restart_code_context()` | ≤ 8 s (asserted) | e2e |
| `timeout=2` on `time.sleep(10)` | `ExecutionTimeout` within 8 s (asserted), typically ≈ 2.5 s | e2e |
| a silent 12 s cell (`time.sleep(12)`) | `KeepAlive` ≥ 2 observed by the fake-free client (the SDK counts keepalives in debug logs); stream survives (asserted end arrives) | e2e |
| memory snapshot with the scientific stack | logged; knob at 1.2 GB (D14) | `image-publish` |
| image build time | logged (M3: 123.9 s) | `image-publish` |
| memory per `Execute` stream | ≤ 256 queued events | by construction (D6) |
| sidecar warm-up (`warmup_ms` at boot, inside the build VM) | logged; the `/ready` escape at 300 s is the hard ceiling | rayd/sidecar logs |

### D16. Logging allowlist and secrecy

`logging.rs` doc gains: `context_id`, `execution_id`, `execution_count`,
`events`, `results`, `mime_types` (names only, e.g. `image/png`),
`timeout_ms`, `outcome`, `kernel_pid`, `attempt`, `backoff_ms`,
`exit_code`, `warmup_ms`, `restart_ms`, `sidecar_restarts`,
`sidecar_stderr_lines`, `duration_ms`, `bytes`, `chunks`, `contexts`,
`reseeded`, `failed`. Never: code, stdout/stderr text, mime payloads,
`envs` keys or values, `cwd`, error values, traceback lines, connection
files, the sidecar's raw stderr. The sidecar's own logger (`logging.py`)
emits JSON lines on stderr with the same allowlist (`level`, `msg`,
fields) and a `code_len`/`text_len` at most; its `msg` strings are fixed
literals.

### D17. Docs alignment

`ARCHITECTURE.md`: `CodeService` row (5 s keepalive, 256-event queue with
`OutputTruncated` after 30 s, 8-context cap, `interrupt → restart` at
+5 s, cancel-on-drop, `Reattach` M5), "Kernel sidecar" section rewritten
to D2/D4/D5/D8/D11 (uid 1000 sidecar, kernelspec `rayito`, `--config` +
`exec_files`, warm-up list, `/run` restart of the default kernel with the
measured cost, the stdio serialisation caveat, Q19 note on deadlines),
domain table row `code` and ports table (`KernelSidecar`, `SidecarLink`,
`KernelStatus`), adapters list (`TokioSidecarLauncher`), hooks table
(`/ready` 503 + escape, `/validate` 503 until done, `/run` rotation task,
`/resume` reseed). `MILESTONES.md` M4: acceptance snippet aligned with the
e2e and the "Estado de aceptación" paragraph with every number of D15.
`SECURITY.md`: T5 → "M4 ✔" with the restart-in-`/run` mechanism; new row
T12 "kernel sidecar and kernels as uid 1000: sandbox code can read its own
connection file, kill the sidecar or a kernel → no escalation, `rayd`
restarts with `kernel_ready=false`, 8 kernels max, `RLIMIT_NPROC` shared
with the sandbox's processes". `AWS_API_NOTES.md` §16: new rows Q34
(snapshot sizes and build time with the scientific stack), Q35
(`run-microvm → kernel_ready` and the default-kernel `restart_ms`), Q36
(`Execute` server-stream through the proxy: 5 s keepalives on a 12 s
silent cell, 64 KiB chunks). `README.md`: `run_code` example marked as
available.

## Risks / Trade-offs

- [Snapshot size and cold start] Warm `numpy`/`pandas`/`matplotlib`/
  `scipy`/`sklearn` may push the memory snapshot well past the probe's
  672 MB and add ≈ 1 s per 500 MB to every run/resume → Mitigation: sizes
  and `kernel_ready_s` measured in the e2e and recorded; the D14 knob trims
  the warm-up list; installed packages stay importable either way.
- [Default-kernel restart in `/run`] Adds seconds between `agent_ready`
  and `kernel_ready` on every sandbox → Mitigation: measured (D15); the
  decision rule of D11 opens an ADR at 8 s p50 instead of silently
  changing behaviour; the SDK polls `Health` at 0.25–2 s so no time is
  wasted once ready.
- [Interrupt does not reach C extensions] A cell stuck inside BLAS or a
  `while True: pass` in a C loop ignores `SIGINT` → Mitigation: the +5 s
  restart branch, tested with the fake sidecar's `hang`; state loss on that
  branch is documented.
- [Single stdout pipe] A stalled client on one context delays every
  context's output for up to 30 s → Mitigation: `OutputTruncated` after
  30 s, 256-event queue, documented; per-context channels are an M6 option
  if the e2e ever shows cross-context stalls.
- [Sidecar killed by sandbox code] Same uid → `kill -9` works →
  Mitigation: supervisor relaunch with backoff, orphan kernels killed,
  `kernel_ready=false` meanwhile, executions end with `KernelDied`; a
  sandbox only hurts itself (T12).
- [`RLIMIT_NPROC 512` shared] Eight kernels × ≈ 6 threads + sidecar + BLAS
  threads eat into the sandbox's own budget → Mitigation: 8-context cap,
  `OPENBLAS_NUM_THREADS=1`/`OMP_NUM_THREADS=1` defaults, documented.
- [`e2b_charts` vs matplotlib 3.10] The vendored extractor may not parse
  every figure → Mitigation: the formatter swallows extractor errors (PNG
  still arrives), the real-kernel test covers line/bar/pie/scatter/box
  figures, the version and sha are recorded.
- [`--config` + `exec_files` route] If IPython ignores `extra_config_file`
  for the kernel app → Mitigation: the real-kernel test asserts the
  formatters exist; fallback without a design change is `--IPKernelApp.
  exec_files=[...]` on the kernelspec `argv`.
- [Monotonic deadlines across suspend (Q19)] An execution timeout armed
  before `pause()` fires on resume → Mitigation: documented now; M5's
  `/resume` checklist rearms; M4's e2e never pauses.
- [`/validate` never failing the build] A broken kernel is not caught by
  the build → Mitigation: `kernel_ready` stays false and `create()` fails
  in the e2e minutes later; the log line says why.
- [Kernel-level `envs` per execution via silent cells] Adds two round trips
  per `run_code(envs=…)` and is visible to `os.environ` readers only →
  Mitigation: only when `envs` is non-empty; documented as E2B-equivalent.

## Migration Plan

1. Merge; CI green (`buf lint`, `buf breaking`, `cargo fmt/clippy/test`,
   sidecar job, `pytest tests/unit`, `ruff`, `mypy`, ARM64 build with the
   sidecar in the zip).
2. `make image-zip` + `image-publish` → new `rayito-base` version;
   three-state gate; `snapshotBuild` sizes recorded.
3. `RAYITO_E2E=1 RAYITO_TEMPLATE=<arn> make test-e2e` → `test_m1_hello.py`,
   `test_m2_processes.py`, `test_m3_filesystem.py`, `test_m4_code.py` green
   (M1–M3 now also wait for `kernel_ready` in `create()`).
4. Rollback: `update-microvm-image-version --status INACTIVE` on the new
   version. The 0.0.4 SDK **requires** an M4 image (`create()` waits for
   `kernel_ready`); pin the SDK to 0.0.3 against a 4.0 image.
5. Acceptance agent archives the change.

## Open Questions

None blocking. To measure during e2e/publish and record in
`AWS_API_NOTES.md` §16 and `MILESTONES.md`: memory/disk snapshot sizes
and build time with the scientific stack; `run-microvm → kernel_ready`
and the `/run` restart cost (`restart_ms`); first-cell and matplotlib-cell
latencies through the proxy; whether the vendored `e2b_charts` parses the
acceptance figure on matplotlib 3.10.9 (the `chart` assertion).

## Acceptance test list

`clients/python/tests/e2e/test_m4_code.py`, marker `e2e`, one `sandbox`
fixture (`maximumDurationInSeconds=900`, no `idlePolicy`,
`ingress=["ALL_INGRESS"]`; the conftest prints `run-microvm → Health
agent_ready and kernel_ready` in seconds, the M4 `kernel_ready_s`), one
test function of numbered blocks as in M2/M3 plus one two-sandbox block:

1. Readiness: right after `create()`, `sbx._probe_health(5).kernel_ready is True` (via the public `is_running()` plus a `Health` call through the stub); `kernel_ready_s` logged and asserted ≤ 15 s.
2. `sbx.run_code("x = 42")` timed (≤ 2 s): `results == []`, `error is None`, `execution_count == 1`, `text is None`.
3. `r = sbx.run_code("x")`: `r.text == "42"`, `r.results[0].is_main_result is True`, `r.results[0].formats() == ["text"]`, `r.execution_count == 2`.
4. `r = sbx.run_code("print(x)")`: `"42" in "".join(r.logs.stdout)`, `r.text is None`, `r.results == []`; `r = sbx.run_code("import sys; print('e', file=sys.stderr)")`: `"e" in "".join(r.logs.stderr)`, `r.logs.stdout == []`.
5. Callbacks: `seen = []`; `sbx.run_code("print('a'); print('b')", on_stdout=seen.append)`: `"".join(m.line for m in seen)` contains `a` and `b`, every `m` is an `OutputMessage` with `error is False` and `timestamp > 0`.
6. Matplotlib (timed ≤ 5 s): `r = sbx.run_code("import matplotlib.pyplot as plt; plt.plot([1, 2, 3]); plt.show()")`: `r.error is None`, `r.results[0].png is not None`, `base64.b64decode(r.results[0].png)[:8] == b"\x89PNG\r\n\x1a\n"`, `r.results[0].chart is not None`, `r.results[0].chart.type is ChartType.LINE`, `isinstance(r.results[0].chart, LineChart)`, `len(r.results[0].chart.elements[0].points) == 3`, `r.results[0].is_main_result is False`, `"png" in r.results[0].formats()` and `"chart" in r.results[0].formats()`.
7. Pandas (timed ≤ 3 s): `r = sbx.run_code("import pandas as pd; df = pd.DataFrame({'a': [1, 2], 'b': [3.5, 4.5]}); df")`: `r.text` contains `a` and `b`, `r.results[0].html is not None`, `r.results[0].data == {"a": [1, 2], "b": [3.5, 4.5]}`, `r.results[0].is_main_result is True`; `sbx.run_code("df.describe()").results[0].data is not None`.
8. Errors: `e = sbx.run_code("1/0")`: `e.error is not None`, `e.error.name == "ZeroDivisionError"`, `"division by zero" in e.error.value`, `"ZeroDivisionError" in e.error.traceback`, `"\x1b[" not in e.error.traceback`, `e.execution_count is not None`, `e.results == []`; `errors = []`; `sbx.run_code("raise ValueError('boom')", on_error=errors.append)`: `errors[0].name == "ValueError"`.
9. Timeout (timed): `t = sbx.run_code("import time; time.sleep(10)", timeout=2)`: elapsed ≤ 8 s, `t.error.name == "ExecutionTimeout"`, `"2000" in t.error.value`, `t.execution_count is not None`; then `sbx.run_code("x").text == "42"` (interrupted, not restarted: state intact) and `sbx.run_code("1+1").text == "2"`.
10. Silent cell through the proxy: `r = sbx.run_code("import time; time.sleep(12); 'done'", timeout=60)` completes with `r.text == "'done'"` (keepalives every 5 s kept the stream alive; elapsed logged).
11. Contexts: `ctx = sbx.create_code_context()` timed (≤ 8 s): `ctx.id != "default"`, `ctx.language == "python"`, `ctx.cwd == "/home/user"`; `sbx.run_code("x", context=ctx).error.name == "NameError"` (isolation); `sbx.run_code("y = 7", context=ctx)`; `sbx.run_code("y", context=ctx.id).text == "7"`; `ids = [c.id for c in sbx.list_code_contexts()]`: `ids[0] == "default"` and `ctx.id in ids`; `ctx2 = sbx.create_code_context(cwd="/tmp", envs={"M4_ENV": "1"})`: `sbx.run_code("import os; os.getcwd()", context=ctx2).text == "'/tmp'"`, `sbx.run_code("import os; os.environ['M4_ENV']", context=ctx2).text == "'1'"`.
12. Per-execution envs: `sbx.run_code("import os; os.environ.get('M4_RUN', 'unset')", envs={"M4_RUN": "yes"}).text == "'yes'"`; `sbx.run_code("import os; os.environ.get('M4_RUN', 'unset')").text == "'unset'"` (restored; the default keeps the cell producing an `execute_result`, since IPython emits none for a `None` value).
13. Restart and remove: `sbx.restart_code_context(ctx)` timed (≤ 8 s); `sbx.run_code("y", context=ctx).error.name == "NameError"`; `sbx.remove_code_context(ctx)`; `ctx.id not in [c.id for c in sbx.list_code_contexts()]`; `with pytest.raises(NotFoundException): sbx.run_code("1", context=ctx)`; `with pytest.raises(InvalidArgumentException): sbx.remove_code_context("default")`; `with pytest.raises(NotFoundException): sbx.remove_code_context("ctx-000000000000")`; `sbx.remove_code_context(ctx2)`.
14. Sequential budget: 20 × `sbx.run_code("1+1")`, all `.text == "2"`, total ≤ 15 s, p95 logged; `sbx.commands.run("id -u").stdout.strip() == "1000"` still works (channels shared); `sbx.commands.run("ps -o user= -C python3 | sort -u").stdout.strip() == "user"` (sidecar and kernels run as `user`).
15. Kernel death: `d = sbx.run_code("import os; os._exit(3)")`: `d.error.name == "KernelDied"`; then `sbx.run_code("1+1").text == "2"` within 15 s (the context converged back) and `sbx.run_code("x").error.name == "NameError"` (state lost, expected).
16. Health during rotation is not re-tested (measured in block 1); `sbx.get_metrics().mem_used_bytes > 0`.
17. Async parity: `asyncio.run(...)` with `AsyncSandbox.connect(sbx.sandbox_id, access_token=sbx.access_token)` (waits for `kernel_ready`): `(await sbx.run_code("x")).text == "42"`; `ctx = await sbx.create_code_context()`; `(await sbx.run_code("2*2", context=ctx)).text == "4"`; `await sbx.remove_code_context(ctx)`; `e = await sbx.run_code("1/0")`: `e.error.name == "ZeroDivisionError"`.
18. Two sandboxes, distinct RNG: with a second `Sandbox.create(...)` (same fixture parameters, killed in `finally`): `a = sbx.run_code("import random; [random.random() for _ in range(3)]").text`, `b = other.run_code(<same>).text`, `a != b`; likewise `numpy.random.default_rng().random(3).tolist()` differ; both sandboxes report `kernel_ready` (the `/run` rotation happened in each).
19. Teardown: `sbx.kill() is True`; `get_info().state in TERMINAL_STATES` within 30 s (M1's poll helper).

Timings pasted into `MILESTONES.md` M4 "Estado de aceptación" and
`AWS_API_NOTES.md` §16: `kernel_ready_s` (both sandboxes), first-cell,
matplotlib, pandas, 20-cell p95, `create_code_context`,
`restart_code_context`, timeout latency, the 12 s silent cell, plus the
image's `snapshotBuild` sizes and build time from `image-publish` and
`restart_ms` from the `rayd` log (CloudWatch, when
`RAYITO_EXECUTION_ROLE_ARN` is set).

Cost: one image version (+$0.037) and two MicroVMs of ≈ 4 min and ≈ 1 min
(< $0.03).

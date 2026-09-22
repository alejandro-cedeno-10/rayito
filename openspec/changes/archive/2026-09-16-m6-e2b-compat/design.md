## Context

State after M5 (accepted 2026-09-16 against real AWS, image `rayito-base`
10.0, SDK `rayito` 0.0.5): the Python SDK exposes `Sandbox`/`AsyncSandbox`
with `create`, `connect`, `kill`, `list`, `get_info`, `is_running`,
`get_host`, `pause`, `resume`, `get_health`, `get_metrics`, `commands`,
`files`, `pty`, `run_code` and the code contexts; 429 unit tests over an
in-process fake `rayd` (`tests/unit/conftest.py` `FakeRayd` +
`fake_process.py`, `fake_filesystem.py`, `fake_code.py`, `fake_pty.py`) and
a botocore `Stubber` control plane; `ruff` and `mypy --strict` clean over
`src` only (`files = ["src"]` in `pyproject.toml`; `uv run mypy src tests`
reports 34 errors in 16 test files). `rayd` parses the run payload in
`rayd-core::run_payload` (`RunPayloadWire` with `serde` defaults and **no**
`deny_unknown_fields`, so an unknown key is ignored — verified in code),
keeps `RunDefaults{envs, user, workdir}` in `SandboxSession` and answers
`Health` from `HealthSnapshot{agent_ready, kernel_ready, agent_version,
uptime, sandbox_id, resume_generation, clock_offset_ms, kernel_state_lost}`
(`crates/rayd/src/grpc/health.rs::to_response`). There is no
`openspec/specs/*` capability for the sandbox lifecycle (M1 predates
OpenSpec); the lifecycle surface lives in code and `ARCHITECTURE.md` "Capa
3". CI (`.github/workflows/ci.yml`) runs `buf lint`, `cargo fmt/clippy/test`,
`uvx ruff check`, `pytest tests/unit`; there is no release workflow and no
docs site. E2B users import `from e2b_code_interpreter import Sandbox` (code
interpreter) or `from e2b import Sandbox` (base SDK) and use the surface
inventoried in `docs/research/2026-09-compass-report-deltas.md`.

Measured facts that shape this design (`AWS_API_NOTES.md`):

- `runHookPayload` is the **only** per-VM input channel and is capped at
  **4096 characters** (Q11, `ValidationException` at 4097). MicroVMs cannot
  be tagged (§1) and `list-microvms` items carry only `microvmId`, `state`,
  `imageArn`, `imageVersion`, `startedAt` (`docs/aws-api/model_summary.md`);
  `get-microvm` adds `endpoint`, `idlePolicy`, `maximumDurationInSeconds`,
  `stateReason` and nothing user-defined. There is no `UpdateMicrovm`
  (ADR-007) and no presigned-URL primitive for the endpoint: every request
  to the proxy needs `x-aws-proxy-auth` (JWE, 60 min) and, on the agent,
  `x-access-token`.
- `Health` is the only RPC without `x-access-token` (ADR-004); the proxy
  JWE gates it. `CreateMicrovmAuthToken` 50 TPS, `GetMicrovm` 100 TPS (§11),
  a JWE mint takes ≈ 0.2–0.4 s, a `Health` ≈ 1 RTT.
- A unary RPC counts as traffic for the idle policy (Q15: "con 1 mensaje/s la
  VM sigue RUNNING"). A `Health` sent to a `SUSPENDED` VM with
  `autoResumeEnabled` **resumes it** (Q4, Q40) and one suspend/resume cycle
  costs ≈ $0.011 (SPEC.md §7).
- Every measured launch (M0–M5) used `ingressNetworkConnectors:
  [ALL_INGRESS]` and `egressNetworkConnectors: [INTERNET_EGRESS]`; whether
  a MicroVM without any egress connector can still reach the internet is
  **not measured** (this change measures it as Q42).
- Cold start `run-microvm → kernel_ready` 2.36 s (M3 pass), `pause()` →
  `SUSPENDED` 1.37–1.49 s, resume-to-`Health` ≈ 1.1–1.6 s with auto-resume,
  `write` 0.65 MB/s and `read` 6.71 MB/s at 2 GB (M3), $0.126/h at 2 GB /
  1 vCPU, snapshot write $0.0038/GB, read $0.00155/GB, storage $0.08/GB-month
  with a one-week minimum per image version (SPEC.md §7, MILESTONES.md).
  These are the numbers the cost page publishes.

## Goals / Non-Goals

**Goals**

- G1. `rayito.e2b`: a drop-in module for `e2b_code_interpreter` and `e2b`
  imports. Every E2B name that has a Rayito equivalent works with E2B's
  kwargs; every E2B feature without an AWS primitive raises
  `UnimplementedError` naming the feature and the reason (Dormice pattern,
  SPEC.md §5). No silent partial behaviour: an ignored kwarg warns, a
  missing feature raises, a lossy mapping is documented on the object
  (`raw_state`, `metadata=None`).
- G2. Per-sandbox `metadata` that survives the process that created the
  sandbox: readable through `connect()`, `get_info()` and `list()` from any
  IAM principal that can mint a JWE for the VM, with the cost model stated
  (O(n) `Health` probes for `list(metadata=)`).
- G3. A releasable `rayito` 0.1.0: metadata, changelog, README, wheel
  checks, a Trusted-Publishing workflow that is exercised as a dry run, and
  a docs site skeleton that builds strictly in CI.
- G4. `mypy --strict` over `src` **and** `tests` as a lint gate, with no
  loss of strictness in `src`.

**Non-Goals**

- The TypeScript client, cgroup slices, IMDS blocking, egress allowlist,
  hook origin validation, the cold-start burst benchmark and the sidecar
  consolidation: other M6 tracks.
- Emulating `set_timeout`, presigned upload/download URLs, metrics history,
  E2B templates, `fork`/snapshots, sandbox `cpu`/`memory` params, E2B's
  `auto_pause`, `mcp` and `network` options of `beta_create`, or the
  `e2b` CLI. They raise `UnimplementedError`.
- A client-side metadata store (local file, DynamoDB) or a control-plane
  service: rejected — not shared across machines, and SPEC.md §4 forbids a
  control-plane service in M1–M6.
- Updating metadata after `create()`: there is no channel (no
  `UpdateMicrovm`, `/run` is accepted once per boot).
- Publishing to PyPI or registering the Trusted Publisher: manual steps
  listed in `tasks.md` as out-of-CI, not performed by this change.
- Shadowing the real `e2b` / `e2b_code_interpreter` distributions
  (installing a package named `e2b`): the drop-in is at import-path level
  (`rayito.e2b`), never by hijacking another distribution's name.

## Decisions

### D1. Metadata lives in the run payload and is echoed by `Health`

**Options considered.** (a) Client-only: keep metadata in the creating
process — rejected, `list()`/`connect()` from another process could not read
it, and E2B's main use of metadata is exactly cross-process lookup.
(b) Client-side store (file/DynamoDB) — rejected (Non-Goals). (c) Payload +
echo through a token-protected RPC — rejected: `list()` is called by an
account principal that does not hold every sandbox's access token, so a
token-gated echo cannot serve `list(metadata=)`. (d) **Payload + echo in
`Health`** — chosen: `Health` is already the tokenless, JWE-gated readiness
probe, the SDK already calls it at readiness, and any principal able to
mint a JWE for the VM can already terminate it, so exposing non-secret
labels to that principal adds nothing (E2B exposes metadata to any holder
of the API key).

**Wire shape.** Payload version stays `1`; a new optional key:

```json
{"v":1,"token_sha256":"<64 hex>","user":"user","workdir":"/home/user",
 "envs":{...},"metadata":{"<key>":"<value>", ...}}
```

`metadata` is omitted when empty (like `envs`). The whole serialised payload
must stay ≤ `RUN_HOOK_PAYLOAD_MAX_CHARS` (4096); the client-side error
names both `envs` and `metadata` and suggests moving large values to a
file. Keys and values are `str`; keys non-empty; no other limit (the budget
is the limit). Order-independent: the SDK serialises with `sort_keys`.

**`rayd`.** `RunPayloadWire.metadata: BTreeMap<String, String>` with
`#[serde(default)]`; `RunPayload.metadata`; `SandboxSession` stores it next
to `defaults` (`metadata: Mutex<BTreeMap<String, String>>`, replaced on the
single accepted `/run`, cleared never); `SandboxSession::metadata() ->
BTreeMap<String, String>`; `HealthSnapshot.metadata: BTreeMap<String,
String>` filled by `session.health()`; `HealthGrpc::to_response` copies it
into `HealthResponse.metadata`. `rayd` 10.0 already ignores the key
(`RunPayloadWire` has no `deny_unknown_fields`), so an SDK 0.1.0 against an
older image simply reads an empty map — documented as "metadata needs image
≥ M6". Logging: `/run` logs `metadata_keys: <count>` only, never keys or
values (SECURITY.md hygiene; metadata is client data).

**Proto.** `HealthResponse.metadata = 11` (`map<string, string>`; fields 9 and 10 are `imds_blocked` and `hook_anomalies` of the parallel `m6-hardening` change, so the three additive fields never collide), comment:
"Metadata del runHookPayload tal cual; vacío antes de /run o si el payload
no lo trae". Additive; `buf breaking` clean; Python regenerated with
`python scripts/gen_python.py`; Rust at `cargo build`.

### D2. SDK metadata surface

- `Sandbox.create(..., metadata: Mapping[str, str] | None = None)` (sync
  and async). `_payload.build_run_hook_payload(metadata=)` +
  `validated_metadata` (same rules as `validated_envs`, error messages in
  Spanish naming the offending key). `LaunchPlan` unchanged in shape.
- `SandboxHealth.metadata: dict[str, str]` (from the proto map;
  `health_from_proto` copies it). `sbx.metadata -> dict[str, str]`: the
  map seen in the **last** `Health` (readiness or reconnect); metadata is
  immutable per sandbox so the cache is authoritative once `agent_ready`
  was `True`. `_record_health` stores it every time (cheap, keeps
  `connect()` and `resume()` paths uniform).
- `SandboxInfo.metadata: dict[str, str] | None = None` and
  `SandboxListItem.metadata: dict[str, str] | None = None`. `None` means
  "not read from the agent" (a `get-microvm` alone never knows it); `{}`
  means "read, and empty". Instance `sbx.get_info()` returns the refreshed
  `get-microvm` info with `metadata=self.metadata` (no extra RPC). Class
  variant `Sandbox.get_info(sandbox_id, *, read_metadata=True, ...)`:
  `get-microvm`; if `read_metadata` and `state == "RUNNING"`, mint a JWE
  for port 8080 (`create_auth_token`), send one `Health` with a 5 s
  deadline over a dedicated channel that is closed afterwards, and fill
  `metadata` when `agent_ready` is `True` (else `None`); any other state
  → `None` without touching the endpoint (a probe would wake a suspended
  VM). `AsyncSandbox` mirrors it with `to_thread` for boto3 and
  `grpc.aio` for the probe.
- `Sandbox.list(..., metadata: Mapping[str, str] | None = None)`:
  - Without `metadata`: unchanged (items with `metadata=None`).
  - With `metadata`: `states` must be `None` or a subset of `{"RUNNING"}`,
    otherwise `InvalidArgumentException("list(metadata=) sólo filtra
    sandboxes RUNNING: los metadatos viven en el agente y sondear un
    sandbox suspendido lo despertaría")`. The listing pages
    `list-microvms` with `states=("RUNNING",)`, then **for each item, in
    order and sequentially**: `get-microvm` (endpoint and current state;
    an item no longer `RUNNING` is skipped), `create_auth_token(port 8080)`,
    `Health` (5 s deadline, dedicated channel closed after the call). The
    item is yielded, with `metadata` filled, iff `agent_ready` is `True` and
    every requested pair matches (`metadata_matches(candidate, wanted)`:
    subset equality on the requested keys, exact string comparison). A VM
    whose `Health` answers `agent_ready=False` (still booting) is skipped
    and logged at `info` ("sandbox %s aún arrancando: sin metadatos");
    a `Health` that fails (`UNAVAILABLE`, deadline, `UNAUTHENTICATED`
    from the proxy after one re-mint) raises `SandboxException` naming the
    sandbox id — the caller must never receive a silently incomplete
    list. `ResourceNotFoundException` from `get-microvm` (terminated
    between calls) is skipped.
  - Cost, documented in the docstring and the docs: `n_running × (1
    GetMicrovm + 1 CreateMicrovmAuthToken + 1 Health)` ≈ `n_running × 0.5–1 s`
    at ≈ 90 ms RTT; every probed sandbox counts the `Health` as traffic for
    its idle policy (Q15), so listing by metadata postpones auto-suspend of
    every `RUNNING` sandbox by one idle window. Sequential on purpose: it
    keeps the two-channel rule per sandbox trivially true and stays far
    below the 50/100 TPS quotas; a concurrent variant is a later
    optimisation, not a contract.
  - The probe is factored as `_read_metadata(plane, info, transport,
    request_timeout) -> dict[str, str] | None` shared by the class
    `get_info` and `list`, in `sandbox_sync/main.py` and
    `sandbox_async/main.py`; the pure parts (`metadata_matches`,
    `list_states_for_metadata(states)`, `metadata_from_health(response)`)
    live in `_sandbox_base.py`.
- Metadata is **not** part of `SandboxInfo.__eq__` concerns for callers:
  it is a plain field. Nothing in the SDK ever logs metadata values.
- `RAYITO_METADATA` env var: not added (no precedent for envs either).

### D3. `rayito.e2b` package layout and delegation model

```
src/rayito/e2b/
  __init__.py     public names (Sandbox, AsyncSandbox, models, exceptions, charts)
  exceptions.py   E2B exception names + NotEnoughSpaceException, TemplateException, UnimplementedError
  _compat.py      pure mapping helpers, warnings, unimplemented() — no I/O
  _models.py      SandboxInfo, ListedSandbox, SandboxState, SandboxQuery, SandboxMetrics, PtySize, SandboxPaginator, AsyncSandboxPaginator
  _sync.py        Sandbox, Filesystem, Pty (wrappers over rayito.Sandbox)
  _async.py       AsyncSandbox, AsyncFilesystem, AsyncPty
```

**Composition, not inheritance.** `rayito.e2b.Sandbox` holds a native
`rayito.Sandbox` (`self._native`). Reasons: E2B's v1 constructor
`Sandbox(template=..., timeout=...)` performs the create, which clashes with
the native `__init__(info=, access_token=, ...)` used by `_open`; and the
shim must present E2B-shaped `SandboxInfo`/`SandboxMetrics`/`PtySize`
without breaking the native types. `commands` is passed through unchanged
(`self.commands = native.commands`: same names and kwargs — `run`, `list`,
`kill`, `send_stdin`, `connect`; native extras `stdin`, `tag`, `close_stdin`
are harmless supersets). `files` and `pty` are thin wrappers (D5). The
native sandbox is reachable as `sbx.native` for users who want Rayito-only
features (`get_health`, `access_token`, `endpoint`, `pty.create(shell=)`).

**Constructor and classmethods (sync; async identical with `await`).**

```python
class Sandbox:
    def __init__(self, template=None, timeout=None, metadata=None, envs=None,
                 api_key=None, domain=None, debug=False, sandbox_id=None,
                 request_timeout=None, proxy=None, secure=True,
                 allow_internet_access=True, *, region=None, session=None,
                 template_version=None, execution_role_arn=None,
                 allowed_ports=None, ingress=None, logging="disabled",
                 access_token=None, ready_timeout=90.0, reconnect_timeout=60.0,
                 keep_on_failure=False, control_plane=None, transport=None)
    @classmethod create(cls, **same) -> Self            # E2B ≥ 1.5 style
    @classmethod connect(cls, sandbox_id, *, api_key=None, domain=None,
                         debug=False, request_timeout=None, proxy=None,
                         **native connect kwargs) -> Self
    @classmethod beta_create(cls, *, auto_pause=None, network=None, mcp=None, **same)
    @classmethod list(cls, *, api_key=None, query: SandboxQuery | None = None,
                      state: Sequence[SandboxState] | None = None, limit=None,
                      next_token=None, domain=None, debug=False,
                      request_timeout=None, **native list kwargs) -> SandboxPaginator
```

`sandbox_id` given to the constructor connects (E2B v1 behaviour) and
ignores the create-only kwargs with one warning listing them. `create()`
is `cls(**kwargs)`; `connect()` is `cls(sandbox_id=..., ...)`; both end in
`_bind(native)`. `beta_create` raises `UnimplementedError` when
`auto_pause`, `network` or `mcp` is not `None`, else behaves like `create`.

### D4. Kwarg mapping (the `_compat.map_create_kwargs` table)

| E2B kwarg | Rayito | Rule |
|---|---|---|
| `template` | `template` | passed as given (image name or ARN); `None` → `RAYITO_TEMPLATE` (native resolution). E2B default names (`base`, `code-interpreter-v1`) are **not** special-cased: a missing image fails loudly with the native `NotFoundException`. |
| `timeout` | `timeout` | `None` → **300** (E2B's default) — a Rayito life, not an idle timer; `idle=None` always (E2B sandboxes never auto-pause). A `timeout > 28800` raises the native `SandboxLifetimeException`. Documented as the visible difference: E2B extends with `set_timeout`, Rayito cannot (ADR-007). |
| `metadata` | `metadata` | D2. |
| `envs` | `envs` | as is. |
| `allow_internet_access` | `egress` | `True` → `["INTERNET_EGRESS"]`; `False` → `()` (no egress connector). See D11 for the measurement that guards this mapping. |
| `secure` | — | `True`: nothing (Rayito is always token-gated). `False`: `RayitoCompatWarning("secure=False ignorado: Rayito exige x-access-token en todo RPC")`. |
| `request_timeout` | `request_timeout` | as is (`None` → native default 60 s). |
| `api_key`, `domain`, `debug`, `proxy` | — | each non-default value emits one `RayitoCompatWarning` naming the kwarg ("api_key ignorado: Rayito usa las credenciales de AWS de la sesión"); never an error. |
| `ingress` | `ingress` | shim default `["ALL_INGRESS"]` (the only measured configuration; an E2B sandbox is reachable from the internet through the proxy). Explicit `ingress=` wins. |
| everything else native | pass-through | `region`, `session`, `template_version`, `execution_role_arn`, `allowed_ports`, `logging`, `access_token`, `ready_timeout`, `reconnect_timeout`, `keep_on_failure`, `control_plane`, `transport`. |
| `idle`, `egress` | not accepted | `TypeError` from the signature; use `allow_internet_access` or the native `rayito.Sandbox`. |

`map_create_kwargs(...) -> CreateMapping(native_kwargs: dict[str, object],
warnings: tuple[str, ...])` is pure and unit-tested; the wrapper emits the
warnings with `warnings.warn(msg, RayitoCompatWarning, stacklevel=3)`.
`RayitoCompatWarning(UserWarning)` lives in `_compat.py` and is exported.

### D5. Surface mapping on the instance

| E2B | Rayito behaviour in the shim |
|---|---|
| `sbx.sandbox_id`, `sbx.is_running()`, `sbx.kill()`, `Sandbox.kill(id)` | native. `Sandbox.kill(id)` reuses `class_method_variant`. |
| `sbx.sandbox_domain` | the native endpoint hostname (the only domain the sandbox has). |
| `sbx.connection_config` | `UnimplementedError` (E2B internals: API key, domain). |
| `sbx.get_host(port) -> str` | native `HostAccess` (a `str` subclass; `f"https://{host}"` works; `.headers` gives the JWE headers that E2B does not need). |
| `sbx.get_info() -> SandboxInfo` / `Sandbox.get_info(id)` | E2B-shaped `SandboxInfo(sandbox_id, template_id=<image ARN>, name=<image name>, metadata: dict|None, started_at, end_at: datetime|None, state: SandboxState, raw_state: str)`. `state` maps `PENDING|RUNNING` → `RUNNING`, `SUSPENDING|SUSPENDED` → `PAUSED`; `TERMINATING|TERMINATED` → `NotFoundException` (E2B answers 404 for a killed sandbox). `end_at` = native `expires_at`, `None` when only a list item was available. |
| `sbx.set_timeout(t)` / `Sandbox.set_timeout(id, t)` | `UnimplementedError("set_timeout", "la vida de un MicroVM es inmutable: no existe UpdateMicrovm (ADR-007)")`. |
| `sbx.get_metrics(start=None, end=None) -> list[SandboxMetrics]` | `start`/`end` not `None` → `UnimplementedError` (no history; `Metrics` is a live procfs snapshot). Else one element with E2B names: `SandboxMetrics(timestamp, cpu_used_pct, cpu_count, mem_used, mem_total, disk_used, disk_total)` in bytes. `Sandbox.get_metrics(id)` class variant → `UnimplementedError` ("necesita el access token: usa connect()"). |
| `sbx.upload_url(path=None, user=None, use_signature=False)`, `sbx.download_url(...)` | `UnimplementedError` ("el proxy de Lambda MicroVMs exige cabeceras firmadas por petición; usa files.write/read o get_host(port).headers"). |
| `sbx.beta_pause()`, `sbx.pause()` | native `pause(wait=True)`; returns the sandbox id like E2B (`str`). |
| `Sandbox.connect(id)` on a paused sandbox | native `connect` (resumes; auto-resume or explicit `resume-microvm`). |
| `sbx.run_code(code, language=None, context=None, on_stdout, on_stderr, on_result, on_error, envs, timeout, request_timeout)` | `language` in `{None, "python"}` (case-insensitive) → native; anything else → `UnimplementedError("run_code(language=...)", "sólo kernels Python (SPEC.md §4)")`. E2B `timeout=None` → native default 300 s; E2B `request_timeout` passed. |
| `sbx.create_code_context(cwd=None, language=None, request_timeout=None) -> Context` | same language rule; `Context = rayito.CodeContext` (fields `id`, `language`, `cwd`). |
| `sbx.commands` | native `Commands`; `CommandHandle`, `CommandResult`, `ProcessInfo`, `CommandExitException` are the native classes (same attribute names as E2B: `pid`, `wait()`, `kill()`, `disconnect()`, `stdout`, `stderr`, `exit_code`, `error`, iteration `(stdout, stderr, pty)`). |
| `sbx.files.write(path, data, user=None, request_timeout=None) -> WriteInfo` and `sbx.files.write(files: list[WriteEntry], ...) -> list[WriteInfo]` | wrapper dispatches on the first positional (`str` → native `write`; sequence → native `write_files`); `WriteInfo = EntryInfo` (superset of E2B's `name`, `type`, `path`); `WriteEntry` native. |
| `sbx.files.read(path, format="text", user=None, request_timeout=None)` | native (same literals `text`/`bytes`/`stream`). |
| `sbx.files.list(path, depth=1, ...)`, `exists`, `get_info`, `remove`, `rename`, `make_dir` | native. |
| `sbx.files.watch_dir(path, on_event=None, on_exit=None, user=None, request_timeout=None, timeout=60, recursive=False) -> WatchHandle` | wrapper: E2B positional `on_event`, E2B default `timeout=60` (the watch ends with `TimeoutException` after 60 s, as in E2B), native `include_entry` not exposed; `WatchHandle` native (`stop()`, `get_new_events()`). |
| `sbx.pty.create(size: PtySize, on_data=None, user=None, cwd=None, envs=None, timeout=60, request_timeout=None) -> CommandHandle` | wrapper converts `rayito.e2b.PtySize(rows, cols)` (E2B field order) to the native `PtySize(cols, rows)`; returns the native `PtyHandle` (a `CommandHandle`). `sbx.pty.send_stdin(pid, data: bytes)`, `resize(pid, size)`, `kill(pid)` mapped the same way. |
| `with Sandbox(...) as sbx:` / `async with` | native semantics: `kill()` on exit. |
| `sbx.native` | the underlying `rayito.Sandbox` (escape hatch, documented). |

`Execution`, `Result`, `Logs`, `OutputMessage`, `ExecutionError`,
`FilesystemEvent`, `FilesystemEventType`, `EntryInfo`, `FileType`,
`WatchHandle`, `CommandResult`, `CommandHandle`, `ProcessInfo`, charts: the
native classes re-exported under E2B's names (`Execution.text`,
`Execution.to_json()`, `Result.formats()`, `Logs.stdout: list[str]` already
match E2B).

### D6. Listing and pagination

`Sandbox.list(query=SandboxQuery(metadata=...), state=[SandboxState.RUNNING],
limit=None)` → `SandboxPaginator`:

- `SandboxState` (`StrEnum`): `RUNNING = "running"`, `PAUSED = "paused"`.
  Filter mapping: `RUNNING` → native states `("PENDING", "RUNNING")`,
  `PAUSED` → `("SUSPENDING", "SUSPENDED")`, `None` → native default (all
  but terminal). With `query.metadata`, `state` must be `None` or
  `[RUNNING]` and the native call uses `states=("RUNNING",)` (D2); any
  other combination → `UnimplementedError("list(state=PAUSED,
  query.metadata)", "los metadatos viven en el agente; leerlos despertaría
  el sandbox")`.
- `SandboxQuery(metadata: dict[str, str] | None = None)` — the E2B v1
  dataclass shape.
- `SandboxPaginator`: `has_next: bool`, `next_token: str | None` (always
  `None`: Rayito paginates `list-microvms` internally), `next_items() ->
  list[SandboxInfo]`; `limit` is the page size (`itertools.islice` over the
  native iterator; `None` = everything in one page). Iteration is lazy: the
  O(n) metadata probes happen inside `next_items()`. `AsyncSandboxPaginator`
  has `await next_items()`. `ListedSandbox = SandboxInfo` (E2B ≥ 1.5 name).
- `next_token=` **passed in** → `UnimplementedError` (no resumable cursor
  across calls).
- Items are E2B-shaped `SandboxInfo` built from `SandboxListItem`: `end_at`
  `None`, `metadata` as returned by the native list (`None` without a
  query, the read map with one).

### D7. Exceptions

`rayito.e2b.exceptions` re-exports the native hierarchy under E2B's names
(`SandboxException`, `TimeoutException`, `NotFoundException`,
`AuthenticationException`, `InvalidArgumentException`,
`RateLimitException`, `CommandExitException`) and adds:

- `NotEnoughSpaceException(SandboxException)` and
  `TemplateException(SandboxException)`: importable so that E2B `except`
  clauses keep compiling; Rayito never raises them (docstring says so; a
  full disk surfaces as `SandboxException` with `grpc_code
  RESOURCE_EXHAUSTED`).
- `UnimplementedError(NotImplementedError)` with attributes `feature: str`
  and `reason: str`; message (Spanish, user-facing): `"E2B {feature} no
  tiene equivalente en Rayito: {reason}. Ver
  docs/site/docs/e2b-compat.md"`. Deliberately **not** a
  `SandboxException`: an E2B `except SandboxException` must not swallow a
  missing feature. Built by `_compat.unimplemented(feature, reason)`.
- `RayitoCompatWarning(UserWarning)` (D4).

The native extras (`SandboxNotReadyException`, `SandboxStateException`,
`SandboxLifetimeException`, `FileNotFoundException`,
`SandboxNotFoundException`, `QuotaExceededException`, `CapacityException`)
stay importable from `rayito.exceptions` and are subclasses of the E2B
names where the hierarchy already says so (`FileNotFoundException` and
`SandboxNotFoundException` ⊂ `NotFoundException`).

### D8. The compat test corpus (fake `rayd`, unit)

`tests/unit/test_e2b_compat_sync.py` and `test_e2b_compat_async.py` run
cookbook-style snippets, written exactly as an E2B user would after
replacing the import line, against the existing `fake_rayd` fixture (Health
+ process + filesystem + code + PTY fakes) and the `control_plane` Stubber:

1. **Hello world** (`e2b_code_interpreter` README): `with Sandbox() as
   sbx: execution = sbx.run_code("x = 1; x + 1"); assert execution.text ==
   "2"`; `print("hi")` → `execution.logs.stdout == ["hi\n"]`;
   `execution.error` is data for `1/0`.
2. **Charts**: a `plt.show()` cell → `results[0].png` and `results[0].chart`
   (`fake_code` already serves the M4 fixtures).
3. **Commands**: `sbx.commands.run("echo hi").stdout == "hi\n"`; `exit 3`
   → `CommandExitException` with `exit_code == 3`; background `run` +
   `handle.wait()`; `on_stdout` callback; `sbx.commands.list()`.
4. **Files**: `info = sbx.files.write("/home/user/a.txt", "x")` → `info.path`;
   `sbx.files.write([WriteEntry(...), WriteEntry(...)])` → two `WriteInfo`;
   `sbx.files.read(path)`, `read(path, format="bytes")`, `files.list`,
   `files.exists`, `files.watch_dir(path, on_event)` sees a `CREATE`.
5. **PTY**: `sbx.pty.create(PtySize(rows=24, cols=80), on_data=...)`,
   `pty.send_stdin(pid, b"echo hola\n")`, `pty.resize(pid, PtySize(rows=40,
   cols=120))` (the fake records `cols=120, rows=40`), `pty.kill(pid)`.
6. **Lifecycle**: `Sandbox.connect(id)`; `sbx.get_info()` shape and
   `state == SandboxState.RUNNING`; `Sandbox.list()` paginator
   (`has_next` false after one page, `limit=1` gives one item per page);
   `Sandbox.list(query=SandboxQuery(metadata={"env": "ci"}))` returns only
   the matching sandbox with the fake `Health` echoing per-VM metadata;
   `Sandbox.kill(id)`; `sbx.is_running()`; `sbx.beta_pause()` then
   `Sandbox.connect(id)`.
7. **Metrics**: `sbx.get_metrics()` → `[SandboxMetrics]` with `mem_used`
   etc. in bytes and `cpu_count == 1`.
8. **Unimplemented**: `set_timeout`, `Sandbox.set_timeout(id, t)`,
   `upload_url`, `download_url`, `get_metrics(start=...)`,
   `Sandbox.get_metrics(id)`, `run_code(language="js")`,
   `create_code_context(language="r")`, `Sandbox.list(next_token="x")`,
   `Sandbox.list(query=SandboxQuery(metadata={...}),
   state=[SandboxState.PAUSED])`, `Sandbox.beta_create(auto_pause=True)`,
   `sbx.connection_config` — each raises `UnimplementedError`, is a
   `NotImplementedError`, is **not** a `SandboxException`, and its message
   names the feature.
9. **Warnings**: `Sandbox(api_key="e2b_x", domain="e2b.dev", debug=True,
   proxy="http://p", secure=False)` emits exactly five
   `RayitoCompatWarning`s (one per kwarg) and still creates the sandbox
   with `timeout=300`, `idle=None`, `egress=[INTERNET_EGRESS ARN]`,
   `ingress=[ALL_INGRESS ARN]` (asserted on the `run-microvm` request the
   Stubber received); `allow_internet_access=False` → no
   `egressNetworkConnectors` key in the request.
10. **Names**: every name in the proposal's re-export list is importable
    from `rayito.e2b` and `rayito.e2b.exceptions` (parametrised test), and
    `rayito.e2b.__all__` equals that list.

`tests/unit/test_e2b_compat_base.py` covers the pure helpers
(`map_create_kwargs` table, `sandbox_state_from_aws`, `info_from_native`,
`metrics_from_native`, `pty_size_to_native`, `states_for`,
`unimplemented`). `FakeRayd` gains `metadata: dict[str, str]` echoed in
`Health`, and `conftest.py` a helper to stand up a second fake endpoint
with different metadata for the `list` cases.

### D9. `mypy` over tests: the 34 errors and the policy

Policy: `strict = true`, `warn_unreachable = true` and `files = ["src",
"tests"]`; **no** new `# type: ignore` except where a test deliberately
passes an invalid type to check runtime validation (the existing
`validate_timeout(True)  # type: ignore[arg-type]` pattern), and then with
the error code. Fixes by category:

| Category (count) | Fix |
|---|---|
| `misc` "Class cannot subclass X (has type Any)" (5: the four fakes + `conftest.FakeRayd`) | The generated `*_pb2_grpc.py` servicers are untyped (`follow_imports = skip` on `rayito.v1.*`). One override, scoped to exactly those modules: `[[tool.mypy.overrides]] module = ["tests.unit.conftest", "tests.unit.fake_process", "tests.unit.fake_filesystem", "tests.unit.fake_code", "tests.unit.fake_pty"]` with `disallow_subclassing_any = false`. `src` keeps the default. |
| `unused-ignore` (8) | Remove the stale `# type: ignore` comments. |
| `unreachable` (9) | `raise error; yield` generator stubs become `yield from (); raise error` (same runtime behaviour, reachable); the remaining ones are dead asserts after `pytest.raises` or narrowing mistakes — restructure the test so the assert is reachable (e.g. read the raised exception from `excinfo.value`). Never silence with `# type: ignore[unreachable]`. |
| `arg-type` (6) | `**fields: object` helpers in `test_code_base.py`/`fake_code.py` typed as `**fields: Any`; `fake_filesystem.deliver_event(event_type: filesystem_pb2.FilesystemEventType.ValueType)`; `encode_stdin` in `_process_base.py` widened to `str | bytes | bytearray` (the test documents that input as supported: a one-line `src` change, behaviour unchanged). |
| `attr-defined` (2) | `translate_rpc_error` tests narrow with `assert isinstance(mapped, SandboxException \| AuthenticationException)` before reading `grpc_code`. |
| `call-overload` (2) | `files.read(path, format="nope")` keeps the runtime check with `# type: ignore[call-overload]` (deliberate invalid literal). |
| `comparison-overlap` (2) | Compare `.value` (`ChartType.BOX_AND_WHISKER.value == "box_and_whisker"`). |

Gate: `Makefile` `lint` adds `cd clients/python && uv run mypy src tests &&
uv run ruff format --check .`; `ci.yml` Python step runs `uv run pytest
tests/unit`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run
mypy src tests`. `tests/e2e` stays type-checked too (it is under `tests`).

### D10. Packaging and release

- `pyproject.toml`: `version = "0.1.0"` (and `_version.py`; a unit test
  `test_packaging.py::test_version_matches_pyproject` reads it with
  `tomllib`); classifiers `Development Status :: 3 - Alpha`, `Intended
  Audience :: Developers`, `License :: OSI Approved :: MIT License`,
  `Operating System :: OS Independent`, `Programming Language :: Python ::
  3 :: Only`, `3.11`, `3.12`, `3.13`, `Framework :: AsyncIO`, `Topic ::
  Software Development :: Libraries :: Python Modules`, `Typing :: Typed`;
  `keywords = ["sandbox", "e2b", "aws", "microvm", "firecracker", "agents",
  "code-interpreter"]`; `[project.urls]` Homepage, Repository,
  Documentation, Changelog (repository URL from the root README; placeholder
  `https://github.com/alejandro-cedeno-10/rayito` until the repo is public — a task
  confirms it); `[dependency-groups] docs = ["mkdocs-material>=9.6",
  "mkdocstrings[python]>=0.27"]`; `[tool.uv.build-backend]` untouched (src
  layout already excludes `tests`).
- `CHANGELOG.md` (Keep a Changelog, Spanish): `0.1.0` lists the surface by
  milestone (M1 lifecycle … M5 pause/resume, M6 metadata + `rayito.e2b`),
  the image requirement (metadata needs the M6 image), the known
  limitations (`set_timeout`, 8 h, bandwidth); `0.0.1–0.0.5` collapsed as
  "internal milestone builds, never published".
- `README.md` (`clients/python`, Spanish): install, credentials
  (`AWS_PROFILE`/`AWS_REGION`, `RAYITO_TEMPLATE`), one runnable example per
  surface: create/kill, `commands` (fg, bg, `on_stdout`), `files` (write,
  read, `write_files`, `watch_dir`), `run_code` (text, png, error as data,
  contexts), `pty`, `pause`/`resume` (kernel state survives), `get_host`
  (headers), metadata (`create(metadata=)`, `list(metadata=)` with the O(n)
  note), the E2B shim (before/after import lines, what raises
  `UnimplementedError`), `AsyncSandbox`, limits and costs in one table,
  links to the docs site. Root `README.md` gets the install line and the
  shim snippet.
- Wheel: `uv build` (sdist + wheel into `clients/python/dist`), `uvx twine
  check dist/*`, and `scripts/check_wheel.py <wheel>` which asserts the
  wheel contains `rayito/py.typed`, `rayito/e2b/__init__.py`,
  `rayito/v1/health_pb2.py`, `rayito/v1/health_pb2.pyi` and **no** path
  under `tests/`, and that `METADATA` declares `Requires-Python: >=3.11`
  and the three runtime deps. Run in CI (`check` job) and in `release.yml`.
- `.github/workflows/release.yml`: `on: push: tags: ["python-v*"]` and
  `workflow_dispatch`. Job `build` (`ubuntu-24.04`): checkout, `astral-sh/
  setup-uv@v6`, `cd clients/python && uv build`, `python scripts/check_wheel.py
  clients/python/dist/*.whl`, `uvx twine check clients/python/dist/*`, on a
  tag assert `${GITHUB_REF_NAME#python-v}` equals the version in
  `pyproject.toml`, `actions/upload-artifact@v4` (`python-dist`). Job
  `publish`: `needs: build`, `if: github.event_name == 'push'`,
  `environment: pypi`, `permissions: {id-token: write, contents: read}`,
  `actions/download-artifact@v4`, `pypa/gh-action-pypi-publish@release/v1`
  with `packages-dir: clients/python/dist` (no token: Trusted Publishing).
  `workflow_dispatch` runs `build` only (dry run). Registering the publisher
  on PyPI (project `rayito`, owner/repo, workflow `release.yml`,
  environment `pypi`) and pushing the first `python-v0.1.0` tag are manual
  steps outside this change; the name reservation is what MILESTONES.md M6
  calls "reserva de nombres".

### D11. `allow_internet_access=False` is guarded by a measurement (Q42)

Whether a MicroVM launched **without** any `egressNetworkConnectors` can
reach the internet is not in `AWS_API_NOTES.md`. The e2e (D13) launches one
sandbox through the shim with `allow_internet_access=False` and runs
`python3 -c "import urllib.request; urllib.request.urlopen('https://example.com', timeout=5)"`
with `timeout=15`. Outcomes:

- The command **fails** (non-zero exit within the 15 s) → the mapping
  stands; record Q42 as "sin conector de egress no hay salida a internet".
- The command **succeeds** → `allow_internet_access=False` must not
  pretend: `map_create_kwargs` raises `UnimplementedError("allow_internet_access=False",
  "un MicroVM sin conector de egress sigue saliendo a internet (Q42); el
  allowlist de egress llega con el track de endurecimiento")`, the unit
  test for that branch flips, and Q42 records the fact.

Either way the decision is closed by data before the change is archived,
and no AWS parameter is invented.

### D12. Docs site skeleton

`docs/site/mkdocs.yml` (`site_name: Rayito`, `docs_dir: docs`, theme
`material` with `navigation.sections`, `content.code.copy`, light/dark
palette; plugins `search`, `mkdocstrings` with the `python` handler and
`paths: ["../../clients/python/src"]`, `options: {docstring_style: google,
show_source: false, members_order: source}`; `markdown_extensions`:
`admonition`, `tables`, `pymdownx.superfences`, `pymdownx.tabbed`).
Nav and pages (Spanish, like the rest of the repo's user-facing docs):

1. `index.md` — what Rayito is, the pitch (SPEC.md §2), status table M1–M6.
2. `quickstart.md` — install, credentials, image publish pointer, the
   SPEC.md §1 snippet, kill/`with`.
3. `concepts.md` — sandbox life vs idle policy, tokens (JWE + access
   token), the two channels, streams and reconnection, contexts, PTY.
4. `api.md` — `::: rayito.Sandbox`, `::: rayito.AsyncSandbox`, the
   sub-clients and models, `::: rayito.e2b` (mkdocstrings).
5. `security.md` — the SECURITY.md threat table condensed + what to never
   put in `envs`/`metadata`, execution role default `None`.
6. `limits.md` — the `AWS_API_NOTES.md` §11 table as seen from the SDK
   (8 h, 4096-char payload, 8 connections/2 channels, 4 MB/s, 50 items per
   page, 256 processes, 8 kernels, 60 min JWE).
7. `e2b-compat.md` — the D4/D5/D6/D7 tables in user form: what works
   unchanged, what maps with a note, what raises `UnimplementedError` and
   why; the import swap; the `timeout`/`set_timeout` difference; the
   metadata cost note.
8. `cost.md` — the measured numbers: $0.126/h at 2 GB/1 vCPU, $0.011 per
   suspend/resume cycle (so `max_idle_seconds < 300` never saves),
   $0.037/week per image version, e2e ≈ $0.03, cold start 2.36 s to
   `kernel_ready`, `pause()` 1.4 s, auto-resume ≈ 0.7–1.6 s, 0.65 MB/s
   write / 6.71 MB/s read, `list(metadata=)` ≈ 0.5–1 s per running sandbox,
   with the source (`MILESTONES.md`, `AWS_API_NOTES.md`) named per row.

Build: `cd clients/python && uv run --group docs mkdocs build -f
../../docs/site/mkdocs.yml --strict --site-dir ../../docs/site/_build`
(`_build` git-ignored); `Makefile` target `docs`; CI job `docs` on
`ubuntu-24.04` running the same command. No deploy (GitHub Pages is a
later decision).

### D13. Acceptance e2e (`tests/e2e/test_m6_e2b_compat.py`)

Marker `e2e`, the M1 guardrails (sweeper, pre-flight, `timeout <= 1800`),
requires the M6 image (any `rayito-base` version whose `rayd` echoes
metadata). Two sandboxes, one suspend/resume cycle, ≈ $0.03.

`test_e2b_shim_cookbook` (one sandbox, `timeout=900`, `metadata={"track":
"m6", "run": <uuid4>}`, `envs={"E2B_TEST": "1"}`, e2e `execution_role_arn`,
`logging` and `control_plane` passed through the shim):

1. `from rayito.e2b import Sandbox, SandboxQuery, SandboxState, PtySize,
   UnimplementedError`; `with Sandbox(...) as sbx:` and the `run-microvm →
   kernel_ready` timing printed (`kernel_ready_s`).
2. `sbx.run_code("x = 40; x + 2").text == "42"`; `print("hi")` in
   `logs.stdout`; `1/0` → `error.name == "ZeroDivisionError"`;
   `plt.show()` → `results[0].png`.
3. `sbx.commands.run("echo $E2B_TEST").stdout == "1\n"`; `exit 3` →
   `CommandExitException`.
4. `sbx.files.write("/home/user/m6.txt", "hola")` → `read` back;
   `write([WriteEntry, WriteEntry])`.
5. `sbx.pty.create(PtySize(rows=24, cols=80), on_data=collector,
   timeout=None)`, `send_stdin(pid, b"echo hola\n")`, `hola\r\n` seen
   within 10 s, `kill(pid)`.
6. `sbx.get_info()`: `metadata == {...}`, `state is SandboxState.RUNNING`,
   `name == <image name>`, `end_at - started_at == 900 s`.
7. `Sandbox.get_info(sbx.sandbox_id).metadata == {...}` (class variant,
   JWE mint + `Health`), timing printed (`get_info_metadata_s`).
8. `Sandbox.list(query=SandboxQuery(metadata={"run": <uuid>}))` →
   exactly `[sbx.sandbox_id]`; the number of `RUNNING` sandboxes probed and
   the wall time printed (`list_metadata_s`, `list_metadata_n`);
   `Sandbox.list(query=SandboxQuery(metadata={"run": "other"}))` → `[]`.
9. Native check: `rayito.Sandbox.list(metadata={"run": <uuid>})` yields the
   item with `metadata` filled; `rayito.Sandbox.list(metadata=...,
   states=["SUSPENDED"])` raises `InvalidArgumentException`.
10. `sbx.get_metrics()` → one `SandboxMetrics` with `mem_total > 0`;
    `sbx.set_timeout(60)`, `sbx.upload_url("/x")`, `sbx.run_code("1",
    language="js")` → `UnimplementedError`.
11. `sbx.beta_pause()` → `SUSPENDED`; `again = Sandbox.connect(sbx.sandbox_id)`
    → `again.run_code("x").text == "40"` and `again.get_info().metadata ==
    {...}` (metadata survives the snapshot); `again.native.get_health().
    resume_generation == 1`.
12. `kill()`; `Sandbox.get_info(id)` → `NotFoundException` once
    `TERMINATING|TERMINATED`.

`test_no_egress_connector` (one sandbox, `timeout=300`,
`allow_internet_access=False`): the D11 probe; prints the outcome; the
assertion direction is fixed by the first measured run and the result is
recorded as Q42.

### D14. Logging and secrecy

Unchanged rules plus: metadata keys and values are never logged by `rayd`
(count only) nor by the SDK (the `list` skip log names the sandbox id
only); the shim never logs `api_key` (the warning message does not include
the value); `UnimplementedError` messages contain no user data.

### D15. Docs alignment

`ARCHITECTURE.md` "Auth interna" payload example gains `"metadata"`; "Capa
3 › Python" layout gains `rayito/e2b/` and the metadata/list rules; the
`HealthService` row mentions `metadata`. `SPEC.md` §3 lifecycle row adds
`metadata` and `list(metadata=)`; §4 replaces the metadata non-goal with
"metadata shipped in M6 via `runHookPayload` + `Health`; `list(metadata=)`
is O(n) and RUNNING-only"; §5 adds a row for the shim. `SECURITY.md` T4 and
T9 mention `metadata` next to `envs`; a note under "Execution role" that
the shim launches with `ALL_INGRESS` + `INTERNET_EGRESS` by default (E2B
parity) and how to tighten it (`allow_internet_access=False`, `ingress=`).
`AWS_API_NOTES.md` §16: Q42 (egress without connector) and the measured
`list(metadata=)`/`get_info` probe timings. `MILESTONES.md` M6: Track C
state paragraph in the accepted style (numbers, costs, image version).

## Risks / Trade-offs

- **`Health` exposes metadata without the access token.** Accepted: the
  proxy JWE already gates `Health`; minting one needs
  `lambda:CreateMicrovmAuthToken` on the image, the same permission that
  allows terminating the VM. Metadata is documented as non-secret, like
  `envs` in the same payload (CloudTrail may log the payload: SECURITY.md
  T4). Anyone needing secret labels keeps them out of metadata.
- **`list(metadata=)` is O(n) and touches every running sandbox.** Stated in
  the docstring, the docs and the cost page; it postpones idle suspension by
  one window on each probed sandbox. Accounts with hundreds of running
  sandboxes should filter by `template`/`template_version` first (both are
  server-side). Sequential probes bound the SDK's footprint; concurrency is
  a later optimisation.
- **Shim default `timeout=300` and no idle policy** reproduce E2B's
  defaults at the cost of Rayito's own (3600 s + auto-suspend). Users who
  want Rayito's behaviour use `rayito.Sandbox`; the compatibility page says
  so in its first paragraph.
- **`ALL_INGRESS` + `INTERNET_EGRESS` by default in the shim** is the
  measured configuration and E2B's semantics (public endpoint, internet
  egress); the native `create()` keeps its explicit connectors. The
  hardening track may later change the native defaults; the shim follows
  E2B, not the native defaults, by design.
- **Cross-track coupling**: the proto field and the `rayd` echo must be in
  the M6 image used for acceptance. The change is additive and
  self-contained in `run_payload.rs`, `session.rs`, `health.rs`,
  `grpc/health.rs`; whichever M6 track publishes the image first carries
  it. An SDK 0.1.0 against the 10.0 image works except that metadata reads
  as `{}` (documented). Known touch points with the sibling changes, all
  additive and merge-safe: `m6-hardening` takes `HealthResponse` fields 9
  and 10 (`imds_blocked`, `hook_anomalies`) and adds its own optional keys
  to the run payload, so this change uses field 11 and only adds the
  `metadata` key; `m6-hardening` bumps the SDK to 0.0.6 while this change
  sets the release version 0.1.0 — the last track to land keeps 0.1.0 and
  its `CHANGELOG.md` entry names every M6 SDK addition (`SandboxInfo.egress/
  ingress`, `imds_blocked`, `hook_anomalies` included when present);
  `m6-benchmark-pool` and `m6-typescript-sdk` add their own `Makefile`
  targets and `ci.yml` jobs/steps, and this change only appends
  (`lint` lines, `docs` target, `docs` job, `release.yml`), never rewrites
  those files.
- **E2B moves.** The shim targets the E2B Python SDK 1.x surface as
  inventoried in `docs/research/2026-09-compass-report-deltas.md` and
  `SPEC.md` §3 (v1 constructor + ≥ 1.5 classmethods, `SandboxPaginator`).
  Newer E2B names are added on demand; unknown kwargs fail with `TypeError`
  at the call site rather than being swallowed.
- **`mkdocstrings` import cost**: the API page imports `rayito` at build
  time (needs `grpcio`/`boto3` installed — the `docs` group depends on the
  package itself via `uv run` in `clients/python`). Acceptable; the build
  runs in the client's environment.

## Migration Plan

1. Proto field + `rayd` echo (additive) → host tests green → included in
   the next `rayito-base` version published by M6.
2. SDK metadata + shim + gates land together as `rayito` 0.1.0; against an
   image without the echo, `metadata` reads `{}`/`None` and the shim works
   otherwise; `CHANGELOG.md` states the image requirement.
3. Release workflow exercised with `workflow_dispatch` (build only). The
   `python-v0.1.0` tag and the PyPI publisher registration happen after
   the milestone is archived, by a human.

Rollback: revert the SDK to 0.0.5 behaviour by not passing `metadata`; the
`rayd` field is inert when absent.

## Open Questions

None blocking. Two facts are resolved by measurement inside this change
rather than by design: Q42 (egress without a connector, D11) and the real
per-sandbox cost of `list(metadata=)` (D13 step 8), both recorded in
`AWS_API_NOTES.md` §16 before archiving.

## Acceptance test list

Unit (fake `rayd`, `uv run pytest tests/unit`):

- `test_payload.py`: `metadata` serialised sorted and omitted when empty;
  budget error names `envs` and `metadata`; invalid key/value types raise
  `InvalidArgumentException`.
- `test_sandbox_base.py`: `metadata_matches` (subset, exact, empty wanted
  matches everything), `list_states_for_metadata` (`None` → `("RUNNING",)`;
  `["RUNNING"]` ok; anything else raises), `metadata_from_health`.
- `test_metadata_sync.py` / `test_metadata_async.py`: `create(metadata=)`
  puts the key in `runHookPayload` (Stubber request assertion);
  `sbx.metadata` after readiness; `get_info()` carries it; class
  `get_info(id)` on `RUNNING` mints a JWE and probes `Health`, on
  `SUSPENDED` returns `metadata=None` without minting; `list(metadata=)`
  yields only matching `RUNNING` items with `metadata` filled, skips a VM
  that turned `SUSPENDED` between calls, skips `agent_ready=False`, raises
  `SandboxException` on a `Health` failure, raises
  `InvalidArgumentException` for `states=["SUSPENDED"]`; the probe channel
  is closed after each VM (fake counts open channels).
- `test_e2b_compat_base.py`, `test_e2b_compat_sync.py`,
  `test_e2b_compat_async.py`: D8 items 1–10.
- `test_packaging.py`: version parity, `__all__` of `rayito.e2b`, shim
  names importable.
- Existing 429 tests unchanged in behaviour after the `mypy` fixes.

Rust (`cargo test --workspace`, `cargo clippy --workspace --all-targets --
-D warnings`): `run_payload` parses `metadata` (present, absent, empty,
non-string value → `MalformedJson`), `session.metadata()` after `/run`,
`HealthSnapshot.metadata` in `health()`, `grpc/health.rs` maps the map;
`m1_hello.rs` `Health` before `/run` has an empty map and after a `/run`
with metadata echoes it.

Gates: `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy
src tests` (0 errors), `uvx ruff check scripts`, `uv build` +
`scripts/check_wheel.py` + `uvx twine check`, `mkdocs build --strict`,
`buf lint`, `buf breaking` additive only.

Real AWS (`RAYITO_E2E=1 RAYITO_TEMPLATE=<m6 image> uv run pytest
tests/e2e/test_m6_e2b_compat.py -m e2e -v -s`): D13 both tests green, Q42
and the probe timings recorded in `AWS_API_NOTES.md`, every MicroVM
`TERMINATED` at the end (sweeper).

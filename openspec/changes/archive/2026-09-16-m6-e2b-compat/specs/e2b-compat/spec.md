## ADDED Requirements

### Requirement: rayito.e2b is an import-level drop-in for the E2B SDKs
The package SHALL ship `rayito.e2b` so that replacing `from e2b_code_interpreter import ...` or `from e2b import ...` with `from rayito.e2b import ...` (and `e2b.exceptions` with `rayito.e2b.exceptions`) is the only source change an E2B Python 1.x program needs to run on Rayito. `rayito.e2b.__all__` SHALL export `Sandbox`, `AsyncSandbox`, `Execution`, `Result`, `Logs`, `OutputMessage`, `ExecutionError`, `Context`, `CommandResult`, `CommandHandle`, `CommandExitException`, `ProcessInfo`, `SandboxException`, `TimeoutException`, `NotFoundException`, `AuthenticationException`, `InvalidArgumentException`, `RateLimitException`, `NotEnoughSpaceException`, `TemplateException`, `UnimplementedError`, `RayitoCompatWarning`, `FilesystemEvent`, `FilesystemEventType`, `EntryInfo`, `WriteInfo`, `WriteEntry`, `FileType`, `WatchHandle`, `PtySize`, `SandboxInfo`, `ListedSandbox`, `SandboxState`, `SandboxQuery`, `SandboxMetrics`, `SandboxPaginator`, `AsyncSandboxPaginator` and the chart classes (`Chart`, `ChartType`, `BarChart`, `LineChart`, `PieChart`, `ScatterChart`, `BoxAndWhiskerChart`, `SuperChart`, `ScaleType`, `BarData`, `PieData`, `PointData`, `BoxAndWhiskerData`). Names with a native equivalent SHALL be the native classes re-exported, never copies. The module SHALL NOT install or shadow a distribution named `e2b` or `e2b_code_interpreter`.

#### Scenario: every name imports
- **WHEN** the parametrised unit test imports each listed name from `rayito.e2b` and each exception from `rayito.e2b.exceptions`
- **THEN** every import succeeds and `set(rayito.e2b.__all__)` equals the listed set

#### Scenario: hello world unchanged
- **WHEN** the E2B README snippet `with Sandbox() as sandbox: execution = sandbox.run_code("x = 1; x + 1")` runs with only the import line changed, `RAYITO_TEMPLATE` set and the fake `rayd` behind
- **THEN** `execution.text == "2"`, `sandbox.run_code("print('hi')").logs.stdout == ["hi\n"]` and the sandbox is killed on exit

### Requirement: E2B create kwargs map to Rayito or warn
`rayito.e2b.Sandbox(...)`, `Sandbox.create(...)` and `AsyncSandbox.create(...)` SHALL accept E2B's kwargs `template`, `timeout`, `metadata`, `envs`, `api_key`, `domain`, `debug`, `sandbox_id`, `request_timeout`, `proxy`, `secure`, `allow_internet_access` and map them as follows: `template` passed as given (`None` → `RAYITO_TEMPLATE`); `timeout` → the sandbox life with **300 s** when `None` and always `idle=None`; `metadata` and `envs` as is; `allow_internet_access=True` → `egress=["INTERNET_EGRESS"]`, `False` → no egress connector; `request_timeout` as is; `sandbox_id` → `connect()` semantics; `secure=False`, `api_key`, `domain`, `debug` and `proxy` (any non-default value) → one `RayitoCompatWarning` each, naming the kwarg and never its value, and otherwise ignored. The shim SHALL default `ingress` to `["ALL_INGRESS"]` and SHALL accept the native `create()` kwargs (`region`, `session`, `template_version`, `execution_role_arn`, `allowed_ports`, `ingress`, `logging`, `access_token`, `ready_timeout`, `reconnect_timeout`, `keep_on_failure`, `control_plane`, `transport`) as pass-through, and SHALL NOT accept `idle` or `egress`. The mapping SHALL be a pure, unit-tested function.

#### Scenario: E2B defaults on the wire
- **WHEN** the unit test creates `Sandbox(api_key="e2b_x", domain="e2b.dev", debug=True, proxy="http://p", secure=False)` against the stubbed control plane
- **THEN** exactly five `RayitoCompatWarning`s are emitted, none containing `e2b_x`, and the `run-microvm` request has `maximumDurationInSeconds == 300`, no `idlePolicy`, `ingressNetworkConnectors == [<ALL_INGRESS ARN>]` and `egressNetworkConnectors == [<INTERNET_EGRESS ARN>]`

#### Scenario: internet access off
- **WHEN** the unit test creates `Sandbox(allow_internet_access=False)`
- **THEN** the `run-microvm` request has no `egressNetworkConnectors` key (or, if Q42 measured that a MicroVM without a connector still reaches the internet, the call raises `UnimplementedError` naming `allow_internet_access=False`)

#### Scenario: constructor connects when given a sandbox id
- **WHEN** the unit test calls `Sandbox(sandbox_id=<id>, access_token=<token>)`
- **THEN** no `run-microvm` is issued, `get-microvm` and a token mint are, and the instance is bound to that sandbox

### Requirement: E2B features without an AWS primitive raise UnimplementedError
`rayito.e2b.UnimplementedError` SHALL subclass `NotImplementedError` (and NOT `SandboxException`), carry `feature` and `reason`, and have a message naming both. The shim SHALL raise it, before any AWS or agent call, for: `set_timeout` (instance and class variant), `upload_url`, `download_url`, `get_metrics(start=..., end=...)`, the class variant `Sandbox.get_metrics(sandbox_id)`, `connection_config`, `run_code(language=...)` and `create_code_context(language=...)` with a language other than `python` (case-insensitive) or `None`, `Sandbox.list(next_token=...)`, `Sandbox.list(query=SandboxQuery(metadata=...))` combined with a `state` filter other than `[SandboxState.RUNNING]`, and `Sandbox.beta_create(...)` with a non-`None` `auto_pause`, `network` or `mcp`. No E2B feature SHALL be approximated silently: anything not mapped and not raising SHALL fail with `TypeError` at the call site.

#### Scenario: set_timeout
- **WHEN** the unit test calls `sbx.set_timeout(60)` and `Sandbox.set_timeout(sbx.sandbox_id, 60)`
- **THEN** both raise `UnimplementedError`, `isinstance(err, NotImplementedError)` is `True`, `isinstance(err, SandboxException)` is `False`, `err.feature == "set_timeout"` and the message mentions `UpdateMicrovm`

#### Scenario: every listed feature
- **WHEN** the parametrised unit test exercises each feature in the list above
- **THEN** each raises `UnimplementedError` and no request reaches the stubbed control plane or the fake `rayd`

#### Scenario: python is the only kernel
- **WHEN** the unit test calls `sbx.run_code("1", language="js")` and `sbx.run_code("1", language="Python")`
- **THEN** the first raises `UnimplementedError` and the second executes on the default context

### Requirement: E2B-shaped models on the instance
`sbx.get_info()` and `Sandbox.get_info(sandbox_id)` SHALL return `rayito.e2b.SandboxInfo(sandbox_id, template_id=<image ARN>, name=<image name>, metadata: dict[str, str] | None, started_at, end_at: datetime | None, state: SandboxState, raw_state: str)` with `PENDING|RUNNING` → `SandboxState.RUNNING`, `SUSPENDING|SUSPENDED` → `SandboxState.PAUSED`, and `TERMINATING|TERMINATED` → `NotFoundException`. `sbx.get_metrics()` SHALL return a one-element `list[rayito.e2b.SandboxMetrics(timestamp, cpu_used_pct, cpu_count, mem_used, mem_total, disk_used, disk_total)]` in bytes. `rayito.e2b.PtySize(rows=24, cols=80)` SHALL follow E2B's field order and the shim's `pty.create(size, ...)`, `pty.resize(pid, size)` SHALL convert it to the native `PtySize(cols, rows)`; `pty.send_stdin(pid, data: bytes)` and `pty.kill(pid)` SHALL map to the native calls and `pty.create` SHALL return the native `PtyHandle` (a `CommandHandle`). `sbx.files.write` SHALL accept both `(path, data)` and `(files: Sequence[WriteEntry])` and return `WriteInfo`/`list[WriteInfo]` where `WriteInfo` is the native `EntryInfo`; `sbx.files.watch_dir(path, on_event=None, on_exit=None, user=None, request_timeout=None, timeout=60, recursive=False)` SHALL accept `on_event` positionally and default `timeout` to 60 s. `sbx.commands` SHALL be the native `Commands`. `sbx.beta_pause()` and `sbx.pause()` SHALL suspend and return the sandbox id; `Sandbox.connect(sandbox_id)` on a paused sandbox SHALL resume it. `sbx.sandbox_domain` SHALL be the endpoint hostname, `sbx.get_host(port)` the native `HostAccess`, and `sbx.native` the underlying `rayito.Sandbox`.

#### Scenario: info and metrics shapes
- **WHEN** the unit test calls `sbx.get_info()` and `sbx.get_metrics()` against the fake `rayd` with metadata `{"a": "1"}`
- **THEN** `info.state is SandboxState.RUNNING`, `info.raw_state == "RUNNING"`, `info.metadata == {"a": "1"}`, `info.end_at == info.started_at + timedelta(seconds=<timeout>)`, `info.name` is the image name, and `sbx.get_metrics()` is a one-element list whose item has `mem_total == 2 * 1024 ** 3` and `cpu_count == 1`

#### Scenario: PTY size order
- **WHEN** the unit test calls `sbx.pty.create(PtySize(rows=24, cols=80))` then `sbx.pty.resize(pid, PtySize(rows=40, cols=120))`
- **THEN** the fake `PtyService` recorded a start size of `cols=80, rows=24` and a resize to `cols=120, rows=40`

#### Scenario: write overloads
- **WHEN** the unit test calls `sbx.files.write("/home/user/a.txt", "x")` and `sbx.files.write([WriteEntry("/home/user/b.txt", "y"), WriteEntry("/home/user/c.txt", "z")])`
- **THEN** the first returns a `WriteInfo` with `path == "/home/user/a.txt"` and the second a list of two, all readable back with `sbx.files.read`

#### Scenario: terminated sandbox is not found
- **WHEN** the stubbed control plane answers `TERMINATED` for `Sandbox.get_info(sandbox_id)`
- **THEN** the shim raises `NotFoundException`

### Requirement: E2B-shaped listing
`Sandbox.list(query: SandboxQuery | None = None, state: Sequence[SandboxState] | None = None, limit: int | None = None)` SHALL return a `SandboxPaginator` (`AsyncSandboxPaginator` on `AsyncSandbox`) with `has_next`, `next_token` (always `None`) and `next_items() -> list[SandboxInfo]`, lazily consuming the native `list()`; `limit` SHALL be the page size; `SandboxState.RUNNING` SHALL map to native states `PENDING` and `RUNNING`, `SandboxState.PAUSED` to `SUSPENDING` and `SUSPENDED`; with `query.metadata` the native call SHALL use `metadata=` and `states=("RUNNING",)` and every returned item SHALL carry the read metadata; without a query `metadata` SHALL be `None` on each item and `end_at` SHALL be `None` (list items carry no duration).

#### Scenario: filter by metadata through the shim
- **WHEN** the unit test lists with `query=SandboxQuery(metadata={"env": "ci"})` while two fake agents echo `{"env": "ci"}` and `{"env": "dev"}`
- **THEN** `paginator.next_items()` contains only the first sandbox with `metadata == {"env": "ci"}` and `paginator.has_next` is `False` afterwards

#### Scenario: page size
- **WHEN** the unit test lists three running sandboxes with `limit=2`
- **THEN** the first `next_items()` returns two items with `has_next` `True`, the second returns one with `has_next` `False`

### Requirement: E2B exception names
`rayito.e2b.exceptions` SHALL re-export the native `SandboxException`, `TimeoutException`, `NotFoundException`, `AuthenticationException`, `InvalidArgumentException`, `RateLimitException` and `CommandExitException` under those names, and define `NotEnoughSpaceException(SandboxException)` and `TemplateException(SandboxException)` for import compatibility with a docstring stating that Rayito never raises them. `RayitoCompatWarning` SHALL subclass `UserWarning`.

#### Scenario: except clauses keep compiling
- **WHEN** an E2B program wraps `sbx.commands.run("exit 3")` in `except CommandExitException as e` imported from `rayito.e2b.exceptions`
- **THEN** the clause catches it with `e.exit_code == 3`, and `NotEnoughSpaceException` and `TemplateException` import and subclass `SandboxException`

### Requirement: Async parity of the shim
`rayito.e2b.AsyncSandbox` SHALL offer the same surface as `rayito.e2b.Sandbox` as coroutines (`await AsyncSandbox.create(...)`, `await sbx.run_code(...)`, `await sbx.commands.run(...)`, `await sbx.files.write(...)`, `await sbx.pty.create(...)`, `await sbx.get_info()`, `await sbx.beta_pause()`, `await AsyncSandbox.list(...)` → `AsyncSandboxPaginator` with `await next_items()`, `async with`), with the same kwarg mapping, warnings and `UnimplementedError` sites.

#### Scenario: async cookbook
- **WHEN** `test_e2b_compat_async.py` runs the hello-world, commands, files, PTY, lifecycle and unimplemented snippets with `await`
- **THEN** every assertion of the sync corpus holds

### Requirement: The shim is accepted against real AWS
The e2e `tests/e2e/test_m6_e2b_compat.py::test_e2b_shim_cookbook` SHALL run the cookbook corpus (`run_code` text/png/error, commands with `envs`, files, PTY, `get_info` with metadata, class `get_info`, `Sandbox.list(query=SandboxQuery(metadata=...))`, `get_metrics`, `UnimplementedError` sites, `beta_pause` + `Sandbox.connect` with the kernel variable alive and metadata intact, `kill` then `NotFoundException`) through `rayito.e2b` against a real MicroVM, printing `kernel_ready_s`, `get_info_metadata_s`, `list_metadata_s` and `list_metadata_n`.

#### Scenario: cookbook on AWS
- **WHEN** the e2e runs with `RAYITO_E2E=1` and the M6 image
- **THEN** every step passes, `Sandbox.list(query=SandboxQuery(metadata={"run": <uuid>}))` yields exactly the created sandbox, and `Sandbox.connect(id)` after `beta_pause()` answers `run_code("x").text == "40"` with `get_info().metadata` unchanged

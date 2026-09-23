## MODIFIED Requirements

### Requirement: E2B features without an AWS primitive raise UnimplementedError
`rayito.e2b.UnimplementedError` SHALL subclass `NotImplementedError` (and NOT `SandboxException`), carry `feature` and `reason`, and have a message naming both; it SHALL be the native `rayito.exceptions.UnimplementedError` re-exported (the shim passes its compatibility-doc reference), so an error raised by the native SDK is caught by the E2B name. The shim SHALL raise it, before any AWS or agent call, for: the class variant `Sandbox.get_metrics(sandbox_id)` when neither `access_token=` nor `RAYITO_ACCESS_TOKEN` provides the sandbox access token (the reason SHALL name both), `connection_config`, `run_code(language=...)` and `create_code_context(language=...)` with a language other than `python`, `bash`, `javascript`, `js`, `typescript` or `ts` (case-insensitive) or `None` (the reason SHALL name the available kernels and the `rayito-base-poly` variant), `Sandbox.list(query=SandboxQuery(metadata=...))` combined with a `state` filter other than `[SandboxState.RUNNING]`, `Sandbox.beta_create(...)` with a non-`None` `network` or `mcp`, and a `lifecycle` whose object-form `on_timeout` carries `keep_memory: False` (`suspend-microvm` always snapshots memory). `set_timeout` and `beta_create(auto_pause=...)` are no longer on this list: they map to the server-enforced deadline (capability `sandbox-timeout`). `upload_url` and `download_url` are mapped (requirement "The shim maps upload_url, download_url and the E2B 2.x file kwargs") and SHALL NOT be in this list. `run_code(language=)` and `create_code_context(language=)` with an accepted name SHALL forward the normalised canonical name to the core SDK (`js` → `javascript`, `ts` → `typescript`), which decides at the agent whether the image ships it; when the agent answers `UNIMPLEMENTED` because the image does not ship that kernel, the shim SHALL raise `UnimplementedError` (feature `run_code(language=<given>)` or `create_code_context(language=<given>)`, reason naming `rayito-base-poly`) chained from the core's `InvalidArgumentException`, and SHALL re-raise every other core error unchanged. It SHALL also raise it for `get_metrics(start=..., end=...)` (instance or class variant) when the agent answers `MetricsHistory` with `UNIMPLEMENTED` (an image that predates M9), with a reason telling to publish an M9 image. No E2B feature SHALL be approximated silently: anything not mapped and not raising SHALL fail with `TypeError` at the call site.

#### Scenario: set_timeout
- **WHEN** the unit test calls `sbx.set_timeout(60)` and `Sandbox.set_timeout(sbx.sandbox_id, 60, access_token=<token>)` against the fake `rayd`
- **THEN** neither raises `UnimplementedError` (both are mapped by requirement "The E2B timeout surface maps to the server-enforced deadline"), and the fake `LifecycleService` recorded two `SetTimeout{60000, EXACT}`

#### Scenario: keep_memory False
- **WHEN** the unit test calls `Sandbox.create(lifecycle={"on_timeout": {"action": "pause", "keep_memory": False}})`
- **THEN** it raises `UnimplementedError`, `isinstance(err, NotImplementedError)` is `True`, `isinstance(err, SandboxException)` is `False`, `err.feature == "lifecycle.on_timeout.keep_memory=False"`, the message mentions `suspend-microvm`, and no `run-microvm` is issued

#### Scenario: every listed feature
- **WHEN** the parametrised unit test exercises each feature in the list above
- **THEN** each raises `UnimplementedError` and no request reaches the stubbed control plane or the fake `rayd`

#### Scenario: bash and javascript are forwarded, other kernels are not
- **WHEN** the unit test calls `sbx.run_code("echo 1", language="Bash")`, `sbx.run_code("1", language="js")`, `sbx.run_code("1", language="Python")` and `sbx.run_code("1", language="r")`
- **THEN** the first two reach the fake `rayd` with `language` `"bash"` and `"javascript"`, the third executes on the default context with no `language` on the wire, and the fourth raises `UnimplementedError` with `feature == "run_code(language='r')"` and a reason naming `rayito-base-poly`

#### Scenario: async shim parity for languages
- **WHEN** `AsyncSandbox.run_code("echo 1", language="bash")` and `AsyncSandbox.create_code_context(language="javascript")` run against the fake
- **THEN** both requests carry the canonical language and `create_code_context` returns a `CodeContext` whose `language` is `javascript`

#### Scenario: typescript is forwarded and a missing kernel is unimplemented
- **WHEN** the unit test calls `sbx.run_code("1", language="ts")` against a fake `rayd` that ships the Deno kernels, and then `sbx.run_code("1", language="javascript")` and `sbx.create_code_context(language="typescript")` against a fake that answers `UNIMPLEMENTED` naming `rayito-base-poly`
- **THEN** the first reaches the fake with `language == "typescript"`, and the other two raise `UnimplementedError` (not `InvalidArgumentException`) whose reason names `rayito-base-poly` and whose `__cause__` is the core exception, in the sync and the async shim

#### Scenario: the shim runs JavaScript and TypeScript on the poly image
- **WHEN** the e2e connects `rayito.e2b.Sandbox.connect(id, access_token=...)` to a `rayito-base-poly` sandbox and runs `run_code("1 + 1", language="js")` and `run_code("const n: number = 3; n", language="ts")`, and the async shim runs one `ts` cell
- **THEN** the texts are `2` and `3`, and on a `rayito-base` sandbox `run_code("1", language="ts")` raises `UnimplementedError` naming `rayito-base-poly`

#### Scenario: native error caught by the E2B name
- **WHEN** the native SDK raises `rayito.exceptions.UnimplementedError("upload_url", "configura transfer=S3Staging(...) o RAYITO_TRANSFER_BUCKET")` inside a shim call
- **THEN** `except rayito.e2b.UnimplementedError` catches it

#### Scenario: ranged metrics and next_token are mapped, not refused
- **WHEN** the unit test calls `sbx.get_metrics(start=a, end=b)` and `Sandbox.list(limit=1, next_token=<token of a previous page>)` against an M9 fake `rayd`, `Sandbox.get_metrics(sbx.sandbox_id)` with `RAYITO_ACCESS_TOKEN` unset, and `sbx.get_metrics(start=a)` against a fake that answers `MetricsHistory` with `UNIMPLEMENTED`
- **THEN** the first two return a list and a paginator without raising, the third raises `UnimplementedError` naming `access_token` and `RAYITO_ACCESS_TOKEN` with no request to the control plane, and the fourth raises `UnimplementedError` with `feature == "get_metrics(start=, end=)"` and a reason naming M9

### Requirement: E2B-shaped models on the instance
`sbx.get_info()` and `Sandbox.get_info(sandbox_id)` SHALL return `rayito.e2b.SandboxInfo(sandbox_id, template_id=<image ARN>, name=<image name>, metadata: dict[str, str] | None, started_at, end_at: datetime | None, state: SandboxState, raw_state: str)` with `PENDING|RUNNING` → `SandboxState.RUNNING`, `SUSPENDING|SUSPENDED` → `SandboxState.PAUSED`, and `TERMINATING|TERMINATED` → `NotFoundException`; `end_at` SHALL be the native `SandboxInfo.expires_at`, i.e. the logical deadline of `Health.lifecycle` when the sandbox is managed (every shim launch on an M9 image) and `started_at + maximumDurationInSeconds` otherwise. `sbx.get_metrics(start: datetime | None = None, end: datetime | None = None, request_timeout: float | None = None)` SHALL return `list[rayito.e2b.SandboxMetrics(timestamp, cpu_used_pct, cpu_count, mem_used, mem_total, disk_used, disk_total, mem_cache)]` in bytes, ascending, from the native `get_metrics_history(start=, end=)`; when neither bound is given and the history is empty or the image predates M9 it SHALL return the one-element list of the native `get_metrics()` snapshot. The class variant `Sandbox.get_metrics(sandbox_id, start=None, end=None, request_timeout=None, *, access_token=None, **kwargs)` SHALL return the same list through the native class `get_metrics_history` with the token from `access_token` or `RAYITO_ACCESS_TOKEN`. `rayito.e2b.SandboxMetrics.mem_cache` SHALL default to 0 so positional construction keeps working. `rayito.e2b.PtySize(rows=24, cols=80)` SHALL follow E2B's field order and the shim's `pty.create(size, ...)`, `pty.resize(pid, size)` SHALL convert it to the native `PtySize(cols, rows)`; `pty.send_stdin(pid, data: bytes)` and `pty.kill(pid)` SHALL map to the native calls and `pty.create` SHALL return the native `PtyHandle` (a `CommandHandle`). `sbx.files.write` SHALL accept both `(path, data)` and `(files: Sequence[WriteEntry])` and return `WriteInfo`/`list[WriteInfo]` where `WriteInfo` is the native `EntryInfo`; `sbx.files.watch_dir(path, on_event=None, on_exit=None, user=None, request_timeout=None, timeout=60, recursive=False)` SHALL accept `on_event` positionally and default `timeout` to 60 s. `sbx.commands` SHALL be the native `Commands`. `sbx.beta_pause()` and `sbx.pause()` SHALL suspend and return the sandbox id; `Sandbox.connect(sandbox_id)` on a paused sandbox SHALL resume it. `sbx.sandbox_domain` SHALL be the endpoint hostname, `sbx.get_host(port)` the native `HostAccess`, and `sbx.native` the underlying `rayito.Sandbox`.

#### Scenario: info and metrics shapes
- **WHEN** the unit test calls `sbx.get_info()` and `sbx.get_metrics()` against the fake `rayd` with metadata `{"a": "1"}`
- **THEN** `info.state is SandboxState.RUNNING`, `info.raw_state == "RUNNING"`, `info.metadata == {"a": "1"}`, `info.end_at` equals the deadline of the fake's `Health.lifecycle` (and `info.started_at + timedelta(seconds=<timeout>)` when the fake reports `UNMANAGED`), `info.name` is the image name, and, with the fake history empty, `sbx.get_metrics()` is a one-element list whose item has `mem_total == 2 * 1024 ** 3` and `cpu_count == 1`

#### Scenario: PTY size order
- **WHEN** the unit test calls `sbx.pty.create(PtySize(rows=24, cols=80))` then `sbx.pty.resize(pid, PtySize(rows=40, cols=120))`
- **THEN** the fake `PtyService` recorded a start size of `cols=80, rows=24` and a resize to `cols=120, rows=40`

#### Scenario: write overloads
- **WHEN** the unit test calls `sbx.files.write("/home/user/a.txt", "x")` and `sbx.files.write([WriteEntry("/home/user/b.txt", "y"), WriteEntry("/home/user/c.txt", "z")])`
- **THEN** the first returns a `WriteInfo` with `path == "/home/user/a.txt"` and the second a list of two, all readable back with `sbx.files.read`

#### Scenario: terminated sandbox is not found
- **WHEN** the stubbed control plane answers `TERMINATED` for `Sandbox.get_info(sandbox_id)`
- **THEN** the shim raises `NotFoundException`

#### Scenario: metrics series through the shim
- **WHEN** the fake `rayd` holds three history samples with `mem_cache_bytes` 7, 8 and 9 and the unit test calls `sbx.get_metrics()`, `sbx.get_metrics(start=a, end=b)` and `Sandbox.get_metrics(sbx.sandbox_id, access_token=<token>)`
- **THEN** each returns three items in timestamp order with `mem_cache` 7, 8 and 9, the second request carried the `start_unix_ms`/`end_unix_ms` of `a` and `b`, and the third was one `MetricsHistory` over a dedicated channel carrying `x-access-token`

### Requirement: E2B-shaped listing
`Sandbox.list(query: SandboxQuery | None = None, state: Sequence[SandboxState] | None = None, limit: int | None = None, next_token: str | None = None, order: str | None = None)` SHALL return a `SandboxPaginator` (`AsyncSandboxPaginator` on `AsyncSandbox`) over the native `Sandbox.paginate` (`AsyncSandbox.paginate`) with `has_next`, `next_token` (the native opaque token: `None` only once the listing is exhausted) and `next_items() -> list[SandboxInfo]`; `limit` SHALL be the page size; `next_token` SHALL resume a previous listing with the same filters; `order` SHALL be `"asc"` or `"desc"` by `startedAt` (computed client-side over every page) and anything else SHALL raise `InvalidArgumentException`. `SandboxQuery` SHALL be `SandboxQuery(metadata=None, state=None, started_after=None, template=None)` in E2B's field order: `state` (or the `state=` kwarg; both given with different sets → `InvalidArgumentException`) SHALL map `SandboxState.RUNNING` to native states `PENDING` and `RUNNING` and `SandboxState.PAUSED` to `SUSPENDING` and `SUSPENDED`; `started_after` SHALL keep sandboxes started at or after it (a naive `datetime` is local time); `template` (or the `template=` kwarg; both given and different → `InvalidArgumentException`) SHALL be an image name or ARN passed to the native `template`; with `query.metadata` the native call SHALL use `metadata=` and `states=("RUNNING",)` and every returned item SHALL carry the read metadata; without a query `metadata` SHALL be `None` on each item and `end_at` SHALL be `None` (list items carry no duration).

#### Scenario: filter by metadata through the shim
- **WHEN** the unit test lists with `query=SandboxQuery(metadata={"env": "ci"})` while two fake agents echo `{"env": "ci"}` and `{"env": "dev"}`
- **THEN** `paginator.next_items()` contains only the first sandbox with `metadata == {"env": "ci"}` and `paginator.has_next` is `False` afterwards

#### Scenario: page size
- **WHEN** the unit test lists three running sandboxes with `limit=2`
- **THEN** the first `next_items()` returns two items with `has_next` `True`, the second returns one with `has_next` `False`

#### Scenario: resuming with next_token through the shim
- **WHEN** the unit test lists three running sandboxes with `limit=1`, keeps `paginator.next_token` after the first page, and calls `Sandbox.list(limit=1, next_token=<that token>)`
- **THEN** the first paginator's `next_token` is a non-empty string while `has_next` is `True`, and walking the second paginator yields the remaining two sandboxes once each

#### Scenario: order and the new query fields
- **WHEN** the unit test lists sandboxes started at 10, 20 and 30 s (the last one `SUSPENDED`) with `order="desc"`, with `query=SandboxQuery(state=[SandboxState.PAUSED])`, with `query=SandboxQuery(started_after=<15 s>)`, with `order="sideways"`, and with `state=[SandboxState.RUNNING]` together with `query=SandboxQuery(state=[SandboxState.PAUSED])`
- **THEN** the first yields 30, 20, 10, the second only the suspended one with `state is SandboxState.PAUSED`, the third the sandboxes at 20 and 30 s, and the last two raise `InvalidArgumentException` before any AWS call

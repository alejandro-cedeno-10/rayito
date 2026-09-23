## ADDED Requirements

### Requirement: The E2B timeout surface maps to the server-enforced deadline
`rayito.e2b.Sandbox` and `rayito.e2b.AsyncSandbox` SHALL map E2B's timeout surface onto the native deadline of the `sandbox-timeout` capability:

- `sbx.set_timeout(timeout, request_timeout=None)` SHALL call the native `set_timeout` (`EXACT`).
- `Sandbox.set_timeout(sandbox_id, timeout, request_timeout=None, **kwargs)` SHALL call the native class variant with `access_token=` or `RAYITO_ACCESS_TOKEN`.
- `Sandbox.connect(sandbox_id, timeout=None, ...)`, with `timeout` positional after `sandbox_id` as in E2B, SHALL pass `timeout` to the native class `connect` (`AT_LEAST`).
- `Sandbox.beta_create(auto_pause=True)` SHALL launch in pause mode with `auto_resume=False`.
- `beta_create(auto_pause=True)` together with `lifecycle` SHALL raise `InvalidArgumentException`.
- The native `LifecycleUnsupportedException` SHALL be re-raised as `UnimplementedError` with feature `lifecycle` and a reason naming the M9 image, chained to the original.
- A beyond-cap `set_timeout` SHALL raise the native `InvalidArgumentException` naming `max_lifetime` and 28800.

#### Scenario: set_timeout and connect through the shim
- **WHEN** the unit test calls `sbx.set_timeout(90)`, `Sandbox.set_timeout(sbx.sandbox_id, 120, access_token=t)` and `Sandbox.connect(sbx.sandbox_id, 300, access_token=t)` against the fake `rayd`
- **THEN** the fake `LifecycleService` recorded `SetTimeout{90000, EXACT}`, `SetTimeout{120000, EXACT}` and `SetTimeout{300000, AT_LEAST}` in that order

#### Scenario: older image through the shim
- **WHEN** the unit test calls `Sandbox.create()` against a fake `Health` without `lifecycle`
- **THEN** it raises `UnimplementedError` with `feature == "lifecycle"`, `err.__cause__` is a `LifecycleUnsupportedException`, and the stubbed control plane recorded one `terminate_microvm`

#### Scenario: beta_create auto_pause
- **WHEN** the unit test calls `Sandbox.beta_create(auto_pause=True)`
- **THEN** the payload `lifecycle.on_timeout == "pause"`, `lifecycle.auto_resume` is false and the request carries an `idlePolicy` with `autoResumeEnabled: true`

## MODIFIED Requirements

### Requirement: E2B create kwargs map to Rayito or warn
`rayito.e2b.Sandbox(...)`, `Sandbox.create(...)` and `AsyncSandbox.create(...)` SHALL accept E2B's kwargs `template`, `timeout`, `metadata`, `envs`, `api_key`, `domain`, `debug`, `sandbox_id`, `request_timeout`, `proxy`, `secure`, `allow_internet_access`, `lifecycle` and map them as follows: `template` passed as given (`None` → `RAYITO_TEMPLATE`); `timeout` → the logical deadline enforced by `rayd` (capability `sandbox-timeout`) with **300 s** when `None`, under a platform cap given by the Rayito-only keyword-only `max_lifetime` (default `max(3600, min(timeout + 60, 28800))`, at most 28800, sent as `maximumDurationInSeconds`); `lifecycle` (`{"on_timeout": "kill"|"pause"|{"action": "kill"|"pause", "keep_memory": bool}, "auto_resume": bool}`, absent or `on_timeout=None` = `"kill"`) → the native `on_timeout`, with `idle=None` for kill and `IdlePolicy(max_idle_seconds=300, auto_resume=<auto_resume>)` for pause, validated like E2B (an action other than `kill`/`pause`, an unknown key, `keep_memory` with `kill` and `auto_resume=True` without pause → `InvalidArgumentException`; `keep_memory: False` → `UnimplementedError`); every shim launch sends a lifecycle block, so on an image older than M9 the shim terminates the just-launched VM and raises `UnimplementedError` naming the M9 image instead of running an unenforced timeout; `metadata` and `envs` as is; `allow_internet_access=True` → `egress=["INTERNET_EGRESS"]`, `False` → no egress connector; `request_timeout` as is; `sandbox_id` → `connect()` semantics; `secure=False`, `api_key`, `domain`, `debug` and `proxy` (any non-default value) → one `RayitoCompatWarning` each, naming the kwarg and never its value, and otherwise ignored. The shim SHALL default `ingress` to `["ALL_INGRESS"]` and SHALL accept the native `create()` kwargs (`region`, `session`, `template_version`, `execution_role_arn`, `allowed_ports`, `ingress`, `logging`, `access_token`, `ready_timeout`, `reconnect_timeout`, `keep_on_failure`, `control_plane`, `transport`) as pass-through, and SHALL NOT accept `idle` or `egress`. The mapping SHALL be a pure, unit-tested function.

#### Scenario: E2B defaults on the wire
- **WHEN** the unit test creates `Sandbox(api_key="e2b_x", domain="e2b.dev", debug=True, proxy="http://p", secure=False)` against the stubbed control plane
- **THEN** exactly five `RayitoCompatWarning`s are emitted, none containing `e2b_x`, and the `run-microvm` request has `maximumDurationInSeconds == 3600`, no `idlePolicy`, a payload `lifecycle == {auto_resume: false, cap_s: 3600, on_timeout: "kill", timeout_s: 300}`, `ingressNetworkConnectors == [<ALL_INGRESS ARN>]` and `egressNetworkConnectors == [<INTERNET_EGRESS ARN>]`

#### Scenario: internet access off
- **WHEN** the unit test creates `Sandbox(allow_internet_access=False)`
- **THEN** the `run-microvm` request has no `egressNetworkConnectors` key (or, if Q42 measured that a MicroVM without a connector still reaches the internet, the call raises `UnimplementedError` naming `allow_internet_access=False`)

#### Scenario: constructor connects when given a sandbox id
- **WHEN** the unit test calls `Sandbox(sandbox_id=<id>, access_token=<token>)`
- **THEN** no `run-microvm` is issued, `get-microvm` and a token mint are, and the instance is bound to that sandbox

#### Scenario: lifecycle pause maps to an idle policy
- **WHEN** the unit test creates `Sandbox(timeout=60, lifecycle={"on_timeout": "pause", "auto_resume": True})` against the stubbed control plane
- **THEN** the `run-microvm` request has `maximumDurationInSeconds == 3600`, `idlePolicy == {maxIdleDurationSeconds: 300, suspendedDurationSeconds: 3300, autoResumeEnabled: true}` and a payload `lifecycle == {auto_resume: true, cap_s: 3600, on_timeout: "pause", timeout_s: 60}`

#### Scenario: E2B lifecycle validation
- **WHEN** the unit test creates with `lifecycle={"on_timeout": "freeze"}`, `lifecycle={"on_timeout": "kill", "auto_resume": True}` and `lifecycle={"on_timeout": {"action": "kill", "keep_memory": True}}`
- **THEN** each raises `InvalidArgumentException` and no `run-microvm` is issued

### Requirement: E2B features without an AWS primitive raise UnimplementedError
`rayito.e2b.UnimplementedError` SHALL subclass `NotImplementedError` (and NOT `SandboxException`), carry `feature` and `reason`, and have a message naming both; it SHALL be the native `rayito.exceptions.UnimplementedError` re-exported (the shim passes its compatibility-doc reference), so an error raised by the native SDK is caught by the E2B name. The shim SHALL raise it, before any AWS or agent call, for: `get_metrics(start=..., end=...)`, the class variant `Sandbox.get_metrics(sandbox_id)`, `connection_config`, `run_code(language=...)` and `create_code_context(language=...)` with a language other than `python`, `bash`, `javascript`, `js`, `typescript` or `ts` (case-insensitive) or `None` (the reason SHALL name the available kernels and the `rayito-base-poly` variant), `Sandbox.list(next_token=...)`, `Sandbox.list(query=SandboxQuery(metadata=...))` combined with a `state` filter other than `[SandboxState.RUNNING]`, `Sandbox.beta_create(...)` with a non-`None` `network` or `mcp`, and a `lifecycle` whose object-form `on_timeout` carries `keep_memory: False` (`suspend-microvm` always snapshots memory). `set_timeout` and `beta_create(auto_pause=...)` are no longer on this list: they map to the server-enforced deadline (capability `sandbox-timeout`). `upload_url` and `download_url` are mapped (requirement "The shim maps upload_url, download_url and the E2B 2.x file kwargs") and SHALL NOT be in this list. `run_code(language=)` and `create_code_context(language=)` with an accepted name SHALL forward the normalised canonical name to the core SDK (`js` → `javascript`, `ts` → `typescript`), which decides at the agent whether the image ships it; when the agent answers `UNIMPLEMENTED` because the image does not ship that kernel, the shim SHALL raise `UnimplementedError` (feature `run_code(language=<given>)` or `create_code_context(language=<given>)`, reason naming `rayito-base-poly`) chained from the core's `InvalidArgumentException`, and SHALL re-raise every other core error unchanged. No E2B feature SHALL be approximated silently: anything not mapped and not raising SHALL fail with `TypeError` at the call site.

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

### Requirement: E2B-shaped models on the instance
`sbx.get_info()` and `Sandbox.get_info(sandbox_id)` SHALL return `rayito.e2b.SandboxInfo(sandbox_id, template_id=<image ARN>, name=<image name>, metadata: dict[str, str] | None, started_at, end_at: datetime | None, state: SandboxState, raw_state: str)` with `PENDING|RUNNING` → `SandboxState.RUNNING`, `SUSPENDING|SUSPENDED` → `SandboxState.PAUSED`, and `TERMINATING|TERMINATED` → `NotFoundException`; `end_at` SHALL be the native `SandboxInfo.expires_at`, i.e. the logical deadline of `Health.lifecycle` when the sandbox is managed (every shim launch on an M9 image) and `started_at + maximumDurationInSeconds` otherwise. `sbx.get_metrics()` SHALL return a one-element `list[rayito.e2b.SandboxMetrics(timestamp, cpu_used_pct, cpu_count, mem_used, mem_total, disk_used, disk_total)]` in bytes. `rayito.e2b.PtySize(rows=24, cols=80)` SHALL follow E2B's field order and the shim's `pty.create(size, ...)`, `pty.resize(pid, size)` SHALL convert it to the native `PtySize(cols, rows)`; `pty.send_stdin(pid, data: bytes)` and `pty.kill(pid)` SHALL map to the native calls and `pty.create` SHALL return the native `PtyHandle` (a `CommandHandle`). `sbx.files.write` SHALL accept both `(path, data)` and `(files: Sequence[WriteEntry])` and return `WriteInfo`/`list[WriteInfo]` where `WriteInfo` is the native `EntryInfo`; `sbx.files.watch_dir(path, on_event=None, on_exit=None, user=None, request_timeout=None, timeout=60, recursive=False)` SHALL accept `on_event` positionally and default `timeout` to 60 s. `sbx.commands` SHALL be the native `Commands`. `sbx.beta_pause()` and `sbx.pause()` SHALL suspend and return the sandbox id; `Sandbox.connect(sandbox_id)` on a paused sandbox SHALL resume it. `sbx.sandbox_domain` SHALL be the endpoint hostname, `sbx.get_host(port)` the native `HostAccess`, and `sbx.native` the underlying `rayito.Sandbox`.

#### Scenario: info and metrics shapes
- **WHEN** the unit test calls `sbx.get_info()` and `sbx.get_metrics()` against the fake `rayd` with metadata `{"a": "1"}`
- **THEN** `info.state is SandboxState.RUNNING`, `info.raw_state == "RUNNING"`, `info.metadata == {"a": "1"}`, `info.end_at` equals the deadline of the fake's `Health.lifecycle` (and `info.started_at + timedelta(seconds=<timeout>)` when the fake reports `UNMANAGED`), `info.name` is the image name, and `sbx.get_metrics()` is a one-element list whose item has `mem_total == 2 * 1024 ** 3` and `cpu_count == 1`

#### Scenario: PTY size order
- **WHEN** the unit test calls `sbx.pty.create(PtySize(rows=24, cols=80))` then `sbx.pty.resize(pid, PtySize(rows=40, cols=120))`
- **THEN** the fake `PtyService` recorded a start size of `cols=80, rows=24` and a resize to `cols=120, rows=40`

#### Scenario: write overloads
- **WHEN** the unit test calls `sbx.files.write("/home/user/a.txt", "x")` and `sbx.files.write([WriteEntry("/home/user/b.txt", "y"), WriteEntry("/home/user/c.txt", "z")])`
- **THEN** the first returns a `WriteInfo` with `path == "/home/user/a.txt"` and the second a list of two, all readable back with `sbx.files.read`

#### Scenario: terminated sandbox is not found
- **WHEN** the stubbed control plane answers `TERMINATED` for `Sandbox.get_info(sandbox_id)`
- **THEN** the shim raises `NotFoundException`

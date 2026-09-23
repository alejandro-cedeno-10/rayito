# e2b-compat Specification

## Purpose
TBD - created by archiving change m6-e2b-compat. Update Purpose after archive.

## Requirements

### Requirement: rayito.e2b is an import-level drop-in for the E2B SDKs
The package SHALL ship `rayito.e2b` so that replacing `from e2b_code_interpreter import ...` or `from e2b import ...` with `from rayito.e2b import ...` (and `e2b.exceptions` with `rayito.e2b.exceptions`) is the only source change an E2B Python **2.x** program needs to run on Rayito. The contract is pinned to `e2b` 2.51.0 and `e2b-code-interpreter` 2.10.0, and the module docstring SHALL name it.

`rayito.e2b.__all__` SHALL contain at least the following names. Names that the other M9 changes add are kept as well.

- Sandboxes and helpers: `Sandbox`, `AsyncSandbox`, `E2B`, `ConnectionConfig`, `get_signature`, `ALL_TRAFFIC`
- Code execution: `Execution`, `Result`, `Logs`, `OutputMessage`, `ExecutionError`, `Context`
- Commands: `CommandResult`, `CommandHandle`, `AsyncCommandHandle`, `CommandExitException`, `ProcessInfo`
- Exceptions: `SandboxException`, `TimeoutException`, `NotFoundException`, `FileNotFoundException`, `SandboxNotFoundException`, `AuthenticationException`, `InvalidArgumentException`, `RateLimitException`, `NotEnoughSpaceException`, `ServiceBusyException`, `FileUploadException`, `GitAuthException`, `GitUpstreamException`, `TemplateException`, `BuildException`, `UnimplementedError`, `RayitoCompatWarning`
- Filesystem: `FilesystemEvent`, `FilesystemEventType`, `EntryInfo`, `WriteInfo`, `WriteEntry`, `FileType`, `WatchHandle`, `AsyncWatchHandle`
- Sandbox models: `PtySize`, `SandboxInfo`, `ListedSandbox`, `SandboxState`, `SandboxQuery`, `SandboxMetrics`, `SandboxPaginator`, `AsyncSandboxPaginator`
- Git: `Git`, `GitStatus`, `GitBranches`, `GitFileStatus`, `GitResetMode`
- Type aliases: `Stdout`, `Stderr`, `PtyOutput`, `OutputHandler`, `Username`, `MIMEType`, `RunCodeLanguage`
- Unimplemented resources: `Template`, `AsyncTemplate`, `Volume`, `AsyncVolume`, `Secret`, `AsyncSecret`
- Charts: `Chart`, `Chart2D`, `ChartType`, `BarChart`, `LineChart`, `PieChart`, `ScatterChart`, `BoxAndWhiskerChart`, `SuperChart`, `ScaleType`, `BarData`, `PieData`, `PointData`, `BoxAndWhiskerData`

Names with a native equivalent SHALL be the native classes re-exported, never copies. In particular:

- `Context is rayito.CodeContext` and `WriteInfo is rayito.EntryInfo`
- `NotEnoughSpaceException is rayito.DiskFullException` and `ServiceBusyException is rayito.CapacityException`
- `UnimplementedError is rayito.UnimplementedError` and `Git is rayito.Git`

The type aliases SHALL be `Username = Stdout = Stderr = MIMEType = str`, `PtyOutput = bytes`, `OutputHandler = Callable[[T], Any]` (subscriptable), and `RunCodeLanguage = Literal["python", "javascript", "typescript", "r", "java", "bash"] | str`. The module SHALL NOT install or shadow a distribution named `e2b` or `e2b_code_interpreter`.

#### Scenario: every name imports
- **WHEN** the parametrised unit test imports each listed name from `rayito.e2b`, and each exception from `rayito.e2b.exceptions`
- **THEN** every import succeeds, `set(rayito.e2b.__all__)` is a superset of the listed set, every entry of `__all__` imports, and `OutputHandler[str]` is a valid subscription

#### Scenario: aliases are the native classes
- **WHEN** the unit test compares the aliases
- **THEN** `NotEnoughSpaceException is DiskFullException`, `ServiceBusyException is CapacityException` and `rayito.e2b.UnimplementedError is rayito.UnimplementedError` all hold, and `Context is CodeContext`

#### Scenario: hello world unchanged
- **WHEN** the E2B README snippet `with Sandbox.create() as sandbox: execution = sandbox.run_code("x = 1; x + 1")` runs with only the import line changed, with `RAYITO_TEMPLATE` set and the fake `rayd` behind it
- **THEN** `execution.text == "2"`, `sandbox.run_code("print('hi')").logs.stdout == ["hi\n"]`, and the sandbox is killed on exit

### Requirement: E2B create kwargs map to Rayito or warn
`rayito.e2b.Sandbox.create(...)` and `rayito.e2b.AsyncSandbox.create(...)` SHALL accept E2B 2.x's positional order `template, timeout, metadata, envs, secure, allow_internet_access, mcp, network, iam, lifecycle, volume_mounts, logger`, plus E2B's `ApiParams` as keywords: `request_timeout`, `retries`, `headers`, `api_headers`, `api_key`, `validate_api_key`, `domain`, `api_url`, `debug`, `proxy`, `sandbox_url`. The deprecated constructor `rayito.e2b.Sandbox(...)` SHALL keep its 1.x keyword surface: `sandbox_id` means `connect()` semantics, and it gains `logger`, `retries` and `headers`.

The mapping SHALL be:

- **`template`:** passed as given; `None` means `RAYITO_TEMPLATE`.
- **`timeout`:** the logical deadline enforced by `rayd` (capability `sandbox-timeout`), 300 s when `None`. It runs under the platform cap given by the Rayito-only keyword-only `max_lifetime`. That cap defaults to `max(3600, min(timeout + 60, 28800))`, is at most 28800, and is sent as `maximumDurationInSeconds`.
- **`lifecycle`:** it takes the form `{"on_timeout": "kill"|"pause"|{"action": "kill"|"pause", "keep_memory": bool}, "auto_resume": bool}`, and an absent value or `on_timeout=None` means `"kill"`.
  - It maps to the native `on_timeout`, with `idle=None` for kill and `IdlePolicy(max_idle_seconds=300, auto_resume=<auto_resume>)` for pause.
  - It is validated like E2B: an action other than `kill`/`pause`, an unknown key, `keep_memory` with `kill`, or `auto_resume=True` without pause raises `InvalidArgumentException`.
  - `keep_memory: False` raises `UnimplementedError` with feature `lifecycle.on_timeout.keep_memory=False`.
  - Every shim launch sends a lifecycle block. So on an image older than M9, the shim terminates the just-launched VM and raises `UnimplementedError` naming the M9 image, instead of running an unenforced timeout.
- **`metadata` and `envs`:** passed as given.
- **`allow_internet_access` and `network`:** `egress` is always `["INTERNET_EGRESS"]`, and `allow_internet_access` is forwarded to the native `create()`.
  - `False` means the native in-guest egress policy with `ALL_TRAFFIC` appended to `deny_out`. It is enforced on `rayito-base-caps`; on any other image the MicroVM is terminated and `UnimplementedError` is raised.
  - `network` is translated by the pure `map_network` into the native `network=`.
  - `map_network` raises `UnimplementedError` for `rules`, `mask_request_host` and `allow_public_traffic=True`, with the reasons of the unimplemented table.
  - It accepts `allow_public_traffic=False` as a no-op, applies the measured `https_ports` rule, and raises `TypeError` naming any other unknown key.
- **`mcp`, `iam`, `volume_mounts`:** any value other than `None` SHALL raise `UnimplementedError`.
- **`logger`:** it SHALL receive the sandbox's SDK log records.
- **`request_timeout`:** passed as given.
- **`retries`:** it SHALL set botocore `retries={"mode": "standard", "total_max_attempts": retries + 1}` on a control plane shared per `(session, region, settings)`.
- **`headers`:** they SHALL become extra gRPC metadata on every RPC, the anonymous `Health` included. The keys `x-aws-proxy-auth`, `x-aws-proxy-port`, `x-aws-proxy-force-h2`, `x-access-token`, `rayito-compress`, `user-agent`, `content-type`, `te` and `host` SHALL be refused with `InvalidArgumentException` naming the key and never the value, and so SHALL any key starting with `x-aws-proxy-`, `grpc-` or `:`, any key ending in `-bin`, and any non-printable-ASCII value.
- **`proxy`:** it SHALL be an `http://host:port` URL and SHALL set the gRPC channel argument `grpc.http_proxy` plus botocore `proxies` for `http` and `https`. Any other form SHALL raise `InvalidArgumentException`, and the URL SHALL never be logged or echoed.
- **`api_key`, `domain`, `debug`, `api_url`, `sandbox_url`, `validate_api_key`, `api_headers` and `secure=False`:** each SHALL emit one `RayitoCompatWarning` naming the kwarg and never its value, and SHALL otherwise be ignored.
- **`retries`, `proxy` or an integration combined with an explicit `control_plane`:** this SHALL raise `InvalidArgumentException`.

The shim SHALL default `ingress` to `["ALL_INGRESS"]`. It SHALL accept the native keyword-only pass-through kwargs `region`, `session`, `template_version`, `execution_role_arn`, `allowed_ports`, `ingress`, `logging`, `access_token`, `ready_timeout`, `reconnect_timeout`, `keep_on_failure`, `control_plane` and `transport`. It SHALL reject `idle`, `egress` and any other unknown kwarg with `TypeError`.

Every argument-level `UnimplementedError`, `TypeError` and `InvalidArgumentException` of this mapping SHALL be raised before any AWS or agent call. The two image gates (lifecycle, egress enforcement) are the exception: they raise after launch, having terminated the VM. The mapping SHALL be a pure, unit-tested function.

#### Scenario: E2B defaults on the wire
- **WHEN** the unit test calls `Sandbox.create(api_key="e2b_x", domain="e2b.dev", debug=True, api_url="https://a", sandbox_url="https://s", validate_api_key=True, api_headers={"k": "v"}, secure=False)` against the stubbed control plane
- **THEN** exactly eight `RayitoCompatWarning`s are emitted, none containing `e2b_x`, `https://a` or `v`
- **AND** the `run-microvm` request has `maximumDurationInSeconds == 3600`, no `idlePolicy`, a payload `lifecycle == {auto_resume: false, cap_s: 3600, on_timeout: "kill", timeout_s: 300}`, `ingressNetworkConnectors == [<ALL_INGRESS ARN>]` and `egressNetworkConnectors == [<INTERNET_EGRESS ARN>]`

#### Scenario: positional 2.x order
- **WHEN** the pure mapping receives `("tpl", 120, {"a": "1"}, {"K": "v"})` positionally
- **THEN** the native kwargs carry `template == "tpl"`, a 120 s deadline, `metadata == {"a": "1"}` and `envs == {"K": "v"}`

#### Scenario: connection kwargs reach the transport and the control plane
- **WHEN** the unit test creates a sandbox with `headers={"X-Trace": "1"}`, `proxy="http://127.0.0.1:3128"` and `retries=2`
- **THEN** the fake `rayd` sees `x-trace: 1` on `Health`, on a unary and on a stream
- **AND** the native transport options contain `("grpc.http_proxy", "http://127.0.0.1:3128")`
- **AND** the control plane's botocore config has `retries["total_max_attempts"] == 3` and `proxies == {"http": "http://127.0.0.1:3128", "https": "http://127.0.0.1:3128"}`

#### Scenario: reserved headers and bad proxies are refused
- **WHEN** the unit test calls `Sandbox.create(headers={"x-access-token": "x"})`, `Sandbox.create(headers={"X-AWS-Proxy-Port": "1"})` and `Sandbox.create(proxy="https://p:1")`
- **THEN** each raises `InvalidArgumentException`, the header messages name the key and never the value, and no `run-microvm` is issued

#### Scenario: internet access off
- **WHEN** the unit test creates `Sandbox.create(allow_internet_access=False)` against a fake `rayd` reporting `egress_enforcement` `GUEST_ROUTES`, and again against one reporting `NONE`
- **THEN** both `run-microvm` requests keep `egressNetworkConnectors == [<INTERNET_EGRESS ARN>]` and carry `"network":{"enforce":true}` in the payload
- **AND** the first sends `UpdateNetwork` with `deny_out=["0.0.0.0/0"]` and returns the sandbox
- **AND** the second raises `UnimplementedError` naming `rayito-base-caps` after `terminate-microvm` was recorded

#### Scenario: constructor connects when given a sandbox id
- **WHEN** the unit test calls `Sandbox(sandbox_id=<id>, access_token=<token>)`
- **THEN** no `run-microvm` is issued, a `get-microvm` and a token mint are, and the instance is bound to that sandbox

#### Scenario: lifecycle pause maps to an idle policy
- **WHEN** the unit test creates `Sandbox.create(timeout=60, lifecycle={"on_timeout": "pause", "auto_resume": True})` against the stubbed control plane
- **THEN** the `run-microvm` request has `maximumDurationInSeconds == 3600`, `idlePolicy == {maxIdleDurationSeconds: 300, suspendedDurationSeconds: 3300, autoResumeEnabled: true}` and a payload `lifecycle == {auto_resume: true, cap_s: 3600, on_timeout: "pause", timeout_s: 60}`

#### Scenario: E2B lifecycle validation
- **WHEN** the unit test creates with `lifecycle={"on_timeout": "freeze"}`, `lifecycle={"on_timeout": "kill", "auto_resume": True}` and `lifecycle={"on_timeout": {"action": "kill", "keep_memory": True}}`
- **THEN** each raises `InvalidArgumentException` and no `run-microvm` is issued

### Requirement: E2B features without an AWS primitive raise UnimplementedError
`UnimplementedError` SHALL be defined once, as `rayito.exceptions.UnimplementedError`, and re-exported by `rayito.e2b` and `rayito.e2b.exceptions` as the same class. The shim builds its instances with a reference to the compatibility doc. The class SHALL subclass `NotImplementedError` and NOT `SandboxException`, carry `feature` and `reason`, and have a message naming both, so an error raised by the native SDK is caught by the E2B name. Each reason SHALL cite the `AWS_API_NOTES.md` section or `SPEC.md` §4 clause it rests on, or name the image or configuration the feature needs.

The shim SHALL raise it before any AWS or agent call, in both `Sandbox` and `AsyncSandbox`, for these features:

- **fork and snapshots:** `fork` (instance and `Sandbox.fork(sandbox_id)`), `create_snapshot`, `list_snapshots`, `delete_snapshot` (AWS_API_NOTES §1, §15)
- **resume and pause variants:** `connect(on_resume='reboot')` (§5); `pause(keep_memory=False)` and `Sandbox.pause(sandbox_id, keep_memory=False)` (§5); a `lifecycle` whose object-form `on_timeout` carries `keep_memory: False`, with feature `lifecycle.on_timeout.keep_memory=False` (§5: `suspend-microvm` always snapshots memory)
- **network keys:** `network.rules` (§7), `network.mask_request_host` (§7), `network.allow_public_traffic=True` (§3, §7)
- **identity and MCP:** `iam=` (§9); `mcp=`, `beta_create(mcp=...)`, `get_mcp_url()` and `get_mcp_token()` (§3, §7)
- **out of scope by SPEC §4:** `volume_mounts=`, and every public method of `Volume`/`AsyncVolume`
- **signatures:** `get_signature(...)` (§7)
- **secrets:** every public method of `Secret`/`AsyncSecret` (SPEC §4, AWS_API_NOTES §7)
- **templates:** every public method of `Template`/`AsyncTemplate` (SPEC §4)
- **bound-client attributes:** the attributes `E2B(...).Template`, `.AsyncTemplate`, `.Volume`, `.AsyncVolume`, `.Secret` and `.AsyncSecret`
- **kernels:** `run_code(language=...)` and `create_code_context(language=...)` with a language other than `python`, `bash`, `javascript`, `js`, `typescript` or `ts` (case-insensitive) or `None`. The reason SHALL name the available kernels and the `rayito-base-poly` variant.
- **metadata queries:** `Sandbox.list(query=SandboxQuery(metadata=...))` combined with a `state` filter other than `[SandboxState.RUNNING]`
- **metrics without a token:** the class variant `Sandbox.get_metrics(sandbox_id)` when neither `access_token=` nor `RAYITO_ACCESS_TOKEN` provides the sandbox access token. The reason SHALL name both.

The shim SHALL also raise it in these cases:

- **old agent, metrics:** `get_metrics(start=..., end=...)`, instance or class variant, when the agent answers `MetricsHistory` with `UNIMPLEMENTED` (an image that predates M9), with a reason telling to publish an M9 image.
- **old agent, kernels:** `run_code(language=)` / `create_code_context(language=)` when the agent answers `UNIMPLEMENTED` because the image does not ship that kernel. The feature is `run_code(language=<given>)` or `create_code_context(language=<given>)`, the reason names `rayito-base-poly`, and the error is chained from the core's `InvalidArgumentException`. Every other core error is re-raised unchanged.

The following are mapped and SHALL NOT raise it:

- `set_timeout` and `beta_create(auto_pause=...)` (server-enforced deadline)
- `upload_url` and `download_url` (S3 presigned transfers)
- a ranged `get_metrics` and `Sandbox.list(next_token=...)` (metrics history and pagination)
- `connection_config`
- `beta_create(network=...)`, which maps like `create(network=...)`

`run_code(language=)` and `create_code_context(language=)` with an accepted name SHALL forward the normalised canonical name to the core SDK (`js` → `javascript`, `ts` → `typescript`), which decides at the agent whether the image ships it. Each listed E2B name SHALL fail with `UnimplementedError` and never with `TypeError` or `AttributeError`, whatever arguments it is called with.

No E2B feature SHALL be approximated silently. An unknown kwarg outside E2B's 2.51 surface SHALL fail with `TypeError` at the call site.

#### Scenario: set_timeout
- **WHEN** the unit test calls `sbx.set_timeout(60)` and `Sandbox.set_timeout(sbx.sandbox_id, 60, access_token=<token>)` against the fake `rayd`
- **THEN** neither raises `UnimplementedError` (both are mapped by requirement "The E2B timeout surface maps to the server-enforced deadline"), and the fake `LifecycleService` recorded two `SetTimeout{60000, EXACT}`

#### Scenario: every listed feature
- **WHEN** the parametrised unit test exercises each feature raised before any call (instance, class and bound-client forms, sync and async)
- **THEN** each raises `UnimplementedError`, not `TypeError` or `AttributeError`
- **AND** `isinstance(err, NotImplementedError)` is `True` and `isinstance(err, SandboxException)` is `False`
- **AND** `err.reason` contains `AWS_API_NOTES.md §`, `SPEC.md §4`, `RAYITO_ACCESS_TOKEN` or `rayito-base-poly`
- **AND** no request reaches the stubbed control plane or the fake `rayd`

#### Scenario: keep_memory False
- **WHEN** the unit test calls `Sandbox.create(lifecycle={"on_timeout": {"action": "pause", "keep_memory": False}})`
- **THEN** it raises `UnimplementedError` with `err.feature == "lifecycle.on_timeout.keep_memory=False"`, the message mentions `suspend-microvm`, and no `run-microvm` is issued

#### Scenario: neutral values are accepted
- **WHEN** the unit test calls `sbx.pause(keep_memory=True)`, `sbx.connect(on_resume="restore")` and `Sandbox.create(network={"allow_public_traffic": False})`
- **THEN** none raises `UnimplementedError`, and each reaches the stubbed control plane

#### Scenario: native error caught by the E2B name
- **WHEN** the native SDK raises `rayito.exceptions.UnimplementedError("upload_url", "configura transfer=S3Staging(...) o RAYITO_TRANSFER_BUCKET")` inside a shim call
- **THEN** `except rayito.e2b.UnimplementedError` catches it

#### Scenario: bash and javascript are forwarded, other kernels are not
- **WHEN** the unit test calls `sbx.run_code("echo 1", language="Bash")`, `sbx.run_code("1", language="js")`, `sbx.run_code("1", language="Python")` and `sbx.run_code("1", language="r")`
- **THEN** the first two reach the fake `rayd` with `language` `"bash"` and `"javascript"`
- **AND** the third runs on the default context with no `language` on the wire
- **AND** the fourth raises `UnimplementedError` with `feature == "run_code(language='r')"` and a reason naming `rayito-base-poly`

#### Scenario: async shim parity for languages
- **WHEN** `AsyncSandbox.run_code("echo 1", language="bash")` and `AsyncSandbox.create_code_context(language="javascript")` run against the fake
- **THEN** both requests carry the canonical language, and `create_code_context` returns a `CodeContext` whose `language` is `javascript`

#### Scenario: typescript is forwarded and a missing kernel is unimplemented
- **WHEN** the unit test calls `sbx.run_code("1", language="ts")` against a fake `rayd` that ships the Deno kernels, and then `sbx.run_code("1", language="javascript")` and `sbx.create_code_context(language="typescript")` against a fake that answers `UNIMPLEMENTED` naming `rayito-base-poly`
- **THEN** the first reaches the fake with `language == "typescript"`
- **AND** the other two raise `UnimplementedError`, not `InvalidArgumentException`, whose reason names `rayito-base-poly` and whose `__cause__` is the core exception, in the sync and the async shim

#### Scenario: ranged metrics and next_token are mapped, not refused
- **WHEN** the unit test calls, in order:
  - `sbx.get_metrics(start=a, end=b)` against an M9 fake `rayd`
  - `Sandbox.list(limit=1, next_token=<token of a previous page>)` against an M9 fake `rayd`
  - `Sandbox.get_metrics(sbx.sandbox_id)` with `RAYITO_ACCESS_TOKEN` unset
  - `sbx.get_metrics(start=a)` against a fake that answers `MetricsHistory` with `UNIMPLEMENTED`
- **THEN** the first two return a list and a paginator without raising
- **AND** the third raises `UnimplementedError` naming `access_token` and `RAYITO_ACCESS_TOKEN`, with no request to the control plane
- **AND** the fourth raises `UnimplementedError` with `feature == "get_metrics(start=, end=)"` and a reason naming M9

#### Scenario: beta_create network maps to the egress policy
- **WHEN** the unit test calls `Sandbox.beta_create(network={"deny_out": ["0.0.0.0/0"]})` against a fake reporting `GUEST_ROUTES`, and `Sandbox.beta_create(mcp={"x": {}})`
- **THEN** the first raises no `UnimplementedError`, and `UpdateNetwork` carries `deny_out=["0.0.0.0/0"]`
- **AND** the second raises `UnimplementedError` with `feature == "mcp"` and makes no request

#### Scenario: the shim runs JavaScript and TypeScript on the poly image
- **WHEN** the e2e connects `rayito.e2b.Sandbox.connect(id, access_token=...)` to a `rayito-base-poly` sandbox and runs `run_code("1 + 1", language="js")` and `run_code("const n: number = 3; n", language="ts")`, and the async shim runs one `ts` cell
- **THEN** the texts are `2` and `3`
- **AND** on a `rayito-base` sandbox, `run_code("1", language="ts")` raises `UnimplementedError` naming `rayito-base-poly`

### Requirement: E2B-shaped models on the instance
`sbx.get_info()` and `Sandbox.get_info(sandbox_id)` SHALL return `rayito.e2b.SandboxInfo` with E2B 2.x's fields, followed by `raw_state`:

`sandbox_id`, `sandbox_domain`, `template_id` (the image ARN), `name` (the image name), `metadata`, `started_at`, `end_at`, `state`, `cpu_count`, `memory_mb`, `envd_version` (the agent version), `allow_internet_access`, `network`, `lifecycle`, `volume_mounts`, `raw_state`.

The field values SHALL be:

- **`state`:** `PENDING|RUNNING` → `SandboxState.RUNNING`, `SUSPENDING|SUSPENDED` → `SandboxState.PAUSED`, and `TERMINATING|TERMINATED` → `NotFoundException`.
- **`end_at`:** the native `SandboxInfo.expires_at`. That is the logical deadline of `Health.lifecycle` when the sandbox is managed, which covers every shim launch on an M9 image, and `started_at + maximumDurationInSeconds` otherwise. It is `None` on list items.
- **`cpu_count`, `memory_mb` and `envd_version`:** they SHALL be `None` when not read from the agent.
- **`lifecycle`:** `{"on_timeout", "auto_resume"}` or `None` when unmanaged.
- **`network`:** `{"allow_out", "deny_out"}` or `None` when no guest policy was read.
- **`allow_internet_access`:** `False` only when a read policy denies all traffic.
- **`volume_mounts`:** always `[]`.

**Metrics.**

- `sbx.get_metrics(start: datetime | None = None, end: datetime | None = None, request_timeout: float | None = None)` SHALL return `list[rayito.e2b.SandboxMetrics(timestamp, cpu_used_pct, cpu_count, mem_used, mem_total, disk_used, disk_total, mem_cache)]`, in bytes and ascending, from the native `get_metrics_history(start=, end=)`.
- When neither bound is given and the history is empty or the image predates M9, it SHALL return the one-element list of the native `get_metrics()` snapshot.
- The class variant `Sandbox.get_metrics(sandbox_id, start=None, end=None, request_timeout=None, *, access_token=None, **kwargs)` SHALL return the same list through the native class `get_metrics_history`, with the token from `access_token` or `RAYITO_ACCESS_TOKEN`.
- `rayito.e2b.SandboxMetrics.mem_cache` SHALL default to 0, so positional construction keeps working.

`rayito.e2b.PtySize(rows=24, cols=80)` SHALL follow E2B's field order, and the shim SHALL convert it to the native `PtySize(cols, rows)`.

The PTY and filesystem wrappers SHALL follow E2B 2.x's signatures:

- **Sync PTY:** `pty.create(size, user=None, cwd=None, envs=None, timeout=60, request_timeout=None, *, on_data=None)` and `pty.connect(pid, timeout=60, request_timeout=None, *, on_data=None)`. A callable passed in the `user` slot SHALL raise `InvalidArgumentException`.
- **Async PTY:** `pty.create(size, on_data, user=None, cwd=None, envs=None, timeout=60, request_timeout=None)` and `pty.connect(pid, on_data, timeout=60, request_timeout=None)`.
- **PTY input and control:** `pty.send_stdin(pid, data: bytes)`, `pty.resize(pid, size)` and `pty.kill(pid)` SHALL map to the native calls, and `pty.create` SHALL return the native `PtyHandle`.
- **Writes:** `files.write` SHALL accept both `(path, data)` and `(files: Sequence[WriteEntry])`, and `files.write_files(files, user=None, request_timeout=None, ...)` SHALL return `list[WriteInfo]`. `WriteInfo` is the native `EntryInfo`.
- **Sync watch:** `files.watch_dir(path, user=None, request_timeout=None, recursive=False, include_entry=False, allow_network_mounts=False, *, on_event=None, on_exit=None, timeout=None)` SHALL pass `include_entry` to the agent, accept `allow_network_mounts` as a no-op because the sandbox has no network mounts, and live until `stop()` by default. A callable passed in the `user` slot SHALL raise `InvalidArgumentException`.
- **Async watch:** `files.watch_dir(path, on_event, on_exit=None, user=None, request_timeout=None, timeout=60, recursive=False, include_entry=False, allow_network_mounts=False)`.

The remaining instance members SHALL behave as follows:

- **`sbx.commands`:** a shim `Commands` with E2B's positional signatures, returning the native handles.
- **`sbx.pause(keep_memory=None)` and `Sandbox.pause(sandbox_id)`:** they SHALL suspend and return `True`, or `False` when the sandbox was already `SUSPENDING`/`SUSPENDED`. `beta_pause` SHALL be the same function.
- **`Sandbox.connect(sandbox_id, timeout=None)`:** on a paused sandbox it SHALL resume it.
- **`sbx.connect(timeout=None)`:** it SHALL resume a paused sandbox on the already-bound native object, extend the deadline to at least `timeout` when given, and return `self`.
- **`sbx.is_running(request_timeout=None)`:** it SHALL bound its `Health` probe by `request_timeout`.
- **`sbx.sandbox_domain`:** the endpoint hostname.
- **`sbx.envd_api_url` and `sbx.envd_direct_url`:** both `https://<endpoint>`.
- **`sbx.traffic_access_token`:** the proxy JWE currently held for port 8080.
- **`sbx.get_host(port)`:** the native `HostAccess`.
- **`sbx.git`:** the native `Git`.
- **`sbx.native`:** the underlying `rayito.Sandbox`.

#### Scenario: info and metrics shapes
- **WHEN** the unit test calls `sbx.get_info()` and `sbx.get_metrics()` against the fake `rayd` with metadata `{"a": "1"}`
- **THEN** `info.state is SandboxState.RUNNING`, `info.raw_state == "RUNNING"` and `info.metadata == {"a": "1"}`
- **AND** `info.end_at` equals the deadline of the fake's `Health.lifecycle`, or `info.started_at + timedelta(seconds=<timeout>)` when the fake reports `UNMANAGED`
- **AND** `info.sandbox_domain` equals the endpoint, `info.envd_version` equals the fake agent version, and `info.cpu_count` and `info.memory_mb` equal the fake Health values
- **AND** `info.volume_mounts == []` and `info.name` is the image name
- **AND** with the fake history empty, `sbx.get_metrics()` is a one-element list whose item has `mem_total == 2 * 1024 ** 3` and `cpu_count == 1`

#### Scenario: metrics series through the shim
- **WHEN** the fake `rayd` holds three history samples with `mem_cache_bytes` 7, 8 and 9, and the unit test calls `sbx.get_metrics()`, `sbx.get_metrics(start=a, end=b)` and `Sandbox.get_metrics(sbx.sandbox_id, access_token=<token>)`
- **THEN** each returns three items in timestamp order with `mem_cache` 7, 8 and 9
- **AND** the second request carried the `start_unix_ms`/`end_unix_ms` of `a` and `b`
- **AND** the third was one `MetricsHistory` over a dedicated channel carrying `x-access-token`

#### Scenario: pause returns a bool
- **WHEN** the unit test calls `sbx.pause()`, then `sbx.pause()` again while the stub answers `SUSPENDED`
- **THEN** the first returns `True`, the second returns `False`, and only one `suspend-microvm` is issued

#### Scenario: PTY size order
- **WHEN** the unit test calls `sbx.pty.create(PtySize(rows=24, cols=80))`, then `sbx.pty.resize(pid, PtySize(rows=40, cols=120))`, then `sbx.pty.connect(pid)`
- **THEN** the fake `PtyService` records a start size of `cols=80, rows=24`, a resize to `cols=120, rows=40`, and a `Connect` for that `pid`

#### Scenario: write overloads
- **WHEN** the unit test calls `sbx.files.write("/home/user/a.txt", "x")`, `sbx.files.write([WriteEntry("/home/user/b.txt", "y")])` and `sbx.files.write_files([WriteEntry("/home/user/c.txt", "z"), WriteEntry("/home/user/d.txt", "w")])`
- **THEN** the first returns a `WriteInfo` with `path == "/home/user/a.txt"`, the second a list of one and the third a list of two, all readable back with `sbx.files.read`

#### Scenario: watch_dir 2.x signature
- **WHEN** the unit test calls the sync `sbx.files.watch_dir("/w", include_entry=True)` and then `sbx.files.watch_dir("/w", lambda e: None)`
- **THEN** the first `WatchDir` request carries `include_entry = true`, and the second raises `InvalidArgumentException` naming `on_event`

#### Scenario: terminated sandbox is not found
- **WHEN** the stubbed control plane answers `TERMINATED` for `Sandbox.get_info(sandbox_id)`
- **THEN** the shim raises `NotFoundException`

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

### Requirement: E2B exception names
`rayito.e2b.exceptions` SHALL re-export these native classes under their names:

- `SandboxException`, `TimeoutException`, `NotFoundException`, `FileNotFoundException`, `SandboxNotFoundException`
- `AuthenticationException`, `InvalidArgumentException`, `RateLimitException`, `CommandExitException`
- `FileUploadException` (a subclass of `TransferException`, itself a `SandboxException` carrying `code` and `reason`, raised when a transfer import fails)
- `GitAuthException` (subclass of `AuthenticationException`) and `GitUpstreamException` (subclass of `SandboxException`)
- `UnimplementedError`

It SHALL bind the following aliases to the native classes Rayito actually raises:

- `NotEnoughSpaceException` to `DiskFullException`, which is raised for `disk_reserve`/`disk_full`
- `ServiceBusyException` to `CapacityException`, which is raised for `InsufficientCapacityException`

It SHALL define `TemplateException(SandboxException)` and `BuildException(Exception)` for import compatibility, each with a docstring stating that Rayito never raises it. `RayitoCompatWarning` SHALL subclass `UserWarning`.

#### Scenario: except clauses keep compiling
- **WHEN** an E2B program wraps `sbx.commands.run("exit 3")` in `except CommandExitException as e`, imported from `rayito.e2b.exceptions`
- **THEN** the clause catches it with `e.exit_code == 3`, and `TemplateException` and `BuildException` import, with `TemplateException` subclassing `SandboxException`

#### Scenario: capacity and disk errors use the E2B names
- **WHEN** the stubbed control plane raises `InsufficientCapacityException` on `run-microvm`, and the fake `rayd` rejects a write with `RESOURCE_EXHAUSTED` and `disk_reserve`
- **THEN** the first surfaces as an instance of `ServiceBusyException` and the second as an instance of `NotEnoughSpaceException`

### Requirement: Async parity of the shim
`rayito.e2b.AsyncSandbox` SHALL offer the same surface as `rayito.e2b.Sandbox` as coroutines, including:

- `await AsyncSandbox.create(...)`, `await sbx.run_code(...)`, `await sbx.commands.run(...)` with E2B's positional order
- `await handle.wait(on_stdout=...)` with sync or async callbacks
- `await sbx.files.write(...)`, `await sbx.files.write_files(...)`, `await sbx.files.watch_dir(path, on_event, ...)`
- `await sbx.pty.create(size, on_data)` and `await sbx.pty.connect(pid, on_data)`
- the code-context methods, `await sbx.get_info()`, `await sbx.pause()` returning a `bool`, and `await sbx.connect()`
- `await AsyncSandbox.list(...)` returning an `AsyncSandboxPaginator`
- `sbx.git` as the native `AsyncGit`, and `async with`
- `E2B(...).AsyncSandbox`

The kwarg mapping, warnings, connection options and `UnimplementedError` sites SHALL be the same as the sync shim's. The properties `envd_api_url`, `envd_direct_url`, `traffic_access_token` and `connection_config` SHALL be plain properties.

#### Scenario: async cookbook
- **WHEN** `test_e2b_compat_async.py` and `test_e2b_v2_async.py` run the hello-world, commands, files, PTY, lifecycle, connection-option and unimplemented snippets with `await`
- **THEN** every assertion of the sync corpus holds

### Requirement: The shim is accepted against real AWS
The e2e `tests/e2e/test_m6_e2b_compat.py::test_e2b_shim_cookbook` SHALL run the cookbook corpus (`run_code` text/png/error, commands with `envs`, files, PTY, `get_info` with metadata, class `get_info`, `Sandbox.list(query=SandboxQuery(metadata=...))`, `get_metrics`, `UnimplementedError` sites, `beta_pause` + `Sandbox.connect` with the kernel variable alive and metadata intact, `kill` then `NotFoundException`) through `rayito.e2b` against a real MicroVM, printing `kernel_ready_s`, `get_info_metadata_s`, `list_metadata_s` and `list_metadata_n`.

#### Scenario: cookbook on AWS
- **WHEN** the e2e runs with `RAYITO_E2E=1` and the M6 image
- **THEN** every step passes, `Sandbox.list(query=SandboxQuery(metadata={"run": <uuid>}))` yields exactly the created sandbox, and `Sandbox.connect(id)` after `beta_pause()` answers `run_code("x").text == "40"` with `get_info().metadata` unchanged

### Requirement: The shim is unaware of pools
`rayito.e2b.Sandbox(...)`, `rayito.e2b.Sandbox.create(...)` and `rayito.e2b.AsyncSandbox.create(...)` SHALL NOT accept a `pool` kwarg (it SHALL fail as any unknown kwarg does, with `TypeError`, never silently ignored and never mapped), `rayito.e2b.__all__` SHALL contain no name containing `Pool`, and the E2B kwarg mapping of the shim SHALL be byte-for-byte unchanged by `m7-suspended-pool`. E2B's SDK has no pool surface, so there is nothing to emulate; a program that wants pooled sandboxes uses the native `rayito.SandboxPool`.

#### Scenario: pool kwarg rejected by the shim
- **WHEN** a unit test calls `rayito.e2b.Sandbox.create(pool=object())` and `rayito.e2b.AsyncSandbox.create(pool=object())`
- **THEN** both raise `TypeError` mentioning `pool` and no `run-microvm` is issued

#### Scenario: no pool name exported
- **WHEN** a unit test inspects `rayito.e2b.__all__` and `rayito.e2b.exceptions.__all__`
- **THEN** no entry contains the substring `Pool`

### Requirement: The shim maps upload_url, download_url and the E2B 2.x file kwargs
`rayito.e2b.Sandbox.upload_url(path=None, user=None, use_signature=False, use_signature_expiration=None)` and `download_url(path, user=None, use_signature=False, use_signature_expiration=None)` SHALL delegate to the native `files.upload_url` / `files.download_url` with `expires_in = use_signature_expiration or 3600` and return the native `UploadTicket` / `DownloadLink` (both `str`, so `requests.put(url, data=f)` and `urlopen(url)` work unchanged). `path=None` SHALL raise `InvalidArgumentException` (an S3 URL carries no file name), `use_signature_expiration <= 0` SHALL raise `InvalidArgumentException`, and `use_signature` SHALL be accepted and ignored because Rayito URLs are always signed. `rayito.e2b.AsyncSandbox` SHALL offer both as coroutines. The shim's `files.write(...)` (both overloads) SHALL accept `gzip`, `metadata` and `use_octet_stream`, and `files.read(...)` SHALL accept `gzip` and `stream_idle_timeout`, passing them to the native calls. `docs/site/docs/e2b-compat.md` SHALL list both methods under "Se mapea, con una nota" with the divergences of design D20 (raw-body PUT, asynchronous landing covered by the barrier and `wait()`, single-use, snapshot download, expiry always set, transfer bucket required, missing file raises at call time, async coroutines).

#### Scenario: E2B upload pattern with only the import changed
- **WHEN** the unit test runs `url = sbx.upload_url("/home/user/in.bin")`, PUTs 1 KiB to `url` on the fake S3 and then calls `sbx.files.read("/home/user/in.bin", format="bytes")`
- **THEN** the read returns the 1 KiB (the fake `rayd` applied the barrier) and `url` is the native `UploadTicket`

#### Scenario: invalid expiration
- **WHEN** the unit test calls `sbx.upload_url("/home/user/x", use_signature_expiration=-1)` and `sbx.download_url("/home/user/x", use_signature_expiration=0)`
- **THEN** both raise `InvalidArgumentException` and no RPC reached the fake

#### Scenario: E2B 2.x write kwargs
- **WHEN** the unit test calls `sbx.files.write("/home/user/a.txt", "x", gzip=True, metadata={"k": "v"}, use_octet_stream=True)`
- **THEN** the fake `rayd` received a gzip-compressed `Write` whose first message carries `metadata == {"k": "v"}`

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

### Requirement: E2B network options and update_network map to the guest egress policy
The pure `map_network` in `rayito/e2b/_compat.py` SHALL translate E2B's `network` dict into the native `NetworkOptions`.

Accepted keys:
- `allow_out` and `deny_out`: lists or callables. Callables are invoked with a context exposing `all_traffic == "0.0.0.0/0"` and an empty `rules` mapping.
- `egress_proxy`: a dict with `address` and optional `username` and `password`.
- `allow_public_traffic=False`: accepted as a no-op.
- `https_ports`: follows the QE2 measurement through the constant `HTTPS_PORTS_SUPPORTED`. When `True`, a list of ports 1–65535 is accepted with no effect. When `False`, a non-empty list raises `UnimplementedError("network.https_ports", ...)` citing the QE2 row. An empty list is always accepted.

Rejected keys:
- `rules`, `mask_request_host` and `allow_public_traffic=True` SHALL raise `UnimplementedError`.
- Any other key SHALL raise `TypeError` naming it (unmapped E2B surface fails with `TypeError`).

`update_network`:
- `Sandbox.update_network(network, **opts) -> None` and `AsyncSandbox.update_network` SHALL replace the whole policy through the native method and return `None`. They accept `allow_out`, `deny_out`, `egress_proxy` and `allow_internet_access`, and raise `UnimplementedError` for `rules`.
- The class forms `Sandbox.update_network(sandbox_id, network, *, access_token=None, region=None, session=None, request_timeout=None)` SHALL use the native class variant and never kill the sandbox.

`ALL_TRAFFIC` SHALL be importable from `rayito.e2b` without being added to `rayito.e2b.__all__` by this change.

The native gate's `UnimplementedError` SHALL surface as the shim's `UnimplementedError` with the same `feature` and `reason`.

#### Scenario: E2B's block-all idiom
- **WHEN** the unit test calls `Sandbox.create(network={"allow_out": ["api.example.com"], "deny_out": lambda ctx: [ctx.all_traffic]})` against a fake reporting `GUEST_ROUTES_AND_PROXY`
- **THEN** `UpdateNetwork` carries `allow_out=["api.example.com"]` and `deny_out=["0.0.0.0/0"]`

#### Scenario: unsupported network keys never pass silently
- **WHEN** the unit test passes `network={"rules": {...}}`, `{"mask_request_host": "x"}`, `{"allow_public_traffic": True}` and `{"bogus": 1}`
- **THEN** the first three raise `UnimplementedError` and the fourth `TypeError`, all before any request reaches the stubbed control plane

#### Scenario: https_ports follows the measurement
- **WHEN** the unit test patches `HTTPS_PORTS_SUPPORTED` to `True` and then to `False` and creates `Sandbox(network={"https_ports": [3000]})`
- **THEN** the first creates the sandbox with no egress enforcement requested and the second raises `UnimplementedError` with `feature == "network.https_ports"`

#### Scenario: update_network returns None in both forms
- **WHEN** the unit test calls `sbx.update_network({})` and `Sandbox.update_network(sbx.sandbox_id, {"deny_out": ["0.0.0.0/0"]}, access_token=<token>)`
- **THEN** both return `None`, the fake `rayd` received two `UpdateNetwork` calls with the empty and the deny-all policy, and no `terminate-microvm` was recorded

### Requirement: Connection configuration surface
`rayito.e2b.ConnectionConfig` SHALL be constructible with the keywords `request_timeout`, `retries`, `headers`, `proxy`, `logger` and `region`. It SHALL accept E2B's ignored keys with one `RayitoCompatWarning` each. It SHALL validate its values with the same rules as `create`, and expose read-only properties `request_timeout` (60.0 when `None`), `retries`, `headers`, `proxy`, `logger`, `region` and `integration`, plus `get_request_timeout(request_timeout=None)`.

`ConnectionConfig.set_integration(integration: str | None)` SHALL store a process-wide integration name. Every control plane built afterwards SHALL append it to botocore `user_agent_extra` (`"rayito/<version> <integration>"`), and `None` SHALL clear it. `sbx.connection_config`, sync and async, SHALL return a `ConnectionConfig` snapshot of the sandbox's effective settings: the region, the request timeout, the retries, headers, proxy and logger given at create/connect, and the integration in force at create.

`logger=` given to `create`/`connect` SHALL receive every log record emitted by that sandbox and its services: readiness, reconnection, token refresh, commands, files, PTY, code and git. The module loggers SHALL receive none of them. The content of the records SHALL be unchanged: never tokens, header values, proxy URLs, commands, PTY bytes or file contents.

#### Scenario: set_integration reaches the user agent
- **WHEN** the unit test calls `ConnectionConfig.set_integration("acme/1.0")` and then `Sandbox.create()`
- **THEN** the control plane's botocore config has a `user_agent_extra` ending in `acme/1.0`, and `sbx.connection_config.integration == "acme/1.0"`

#### Scenario: logger receives the SDK records
- **WHEN** the unit test passes `logger=logging.getLogger("app")` with a list handler to `Sandbox.create()` against the fakes
- **THEN** the handler receives the `run-microvm` acceptance and readiness records, and a handler on `rayito.sandbox` receives none of them for that sandbox

#### Scenario: secrets never reach the logs
- **WHEN** the unit test creates a sandbox with `headers={"x-trace": "valor-secreto"}` and `proxy="http://u:clave@127.0.0.1:3128"`, capturing every `rayito.*` record and the custom logger
- **THEN** no captured record contains `valor-secreto`, `clave` or `u:clave`

### Requirement: Bound E2B client
`rayito.e2b.E2B(*, region=None, session=None, control_plane=None, **api_params)` SHALL expose instance attributes `Sandbox` and `AsyncSandbox`. These SHALL be subclasses of the shim classes carrying a read-only copy of the given params as `_bound_params`. Every classmethod and every instance created from them SHALL merge those params under the per-call kwargs with E2B's rule: per-call values win, a per-call `None` falls back to the bound value, and per-call `headers` replace the bound ones. The ignored `ApiParams` given to `E2B(...)` SHALL warn once, at construction. Accessing `Template`, `AsyncTemplate`, `Volume`, `AsyncVolume`, `Secret` or `AsyncSecret` on the client SHALL raise `UnimplementedError`.

#### Scenario: bound params are used
- **WHEN** the unit test calls `E2B(region="us-east-1", control_plane=fake).Sandbox.create()` and then `E2B(region="us-east-1", control_plane=fake).Sandbox.get_info(sandbox_id, region="us-east-1")`
- **THEN** both calls use the fake control plane, and a per-call `region` value overrides the bound one

#### Scenario: two clients are isolated
- **WHEN** two clients are built with different `headers` and each creates a sandbox
- **THEN** each sandbox's RPCs carry only its own client's headers, and `rayito.e2b.Sandbox._bound_params` stays empty

### Requirement: E2B positional commands
`sbx.commands` SHALL be a shim `Commands` (`AsyncCommands` on `AsyncSandbox`) with these E2B 2.x signatures:

- `run(cmd, background=None, envs=None, user=None, cwd=None, on_stdout=None, on_stderr=None, stdin=None, timeout=60, request_timeout=None)`
- `connect(pid, timeout=60, request_timeout=None)`; async adds `on_stdout=None, on_stderr=None`
- `list(request_timeout=None)`, `kill(pid, request_timeout=None)`
- `send_stdin(pid, data, request_timeout=None)`, `close_stdin(pid, request_timeout=None)`

Each method delegates to the native keyword-only method. `background=None` and `stdin=None` SHALL mean `False`, and the methods SHALL return the native `CommandResult`, `CommandHandle` or `AsyncCommandHandle`.

#### Scenario: positional background run with wait callbacks
- **WHEN** the unit test calls `handle = sbx.commands.run("echo hi", True)` and then `handle.wait(on_stdout=chunks.append)`
- **THEN** `handle` is a native `CommandHandle`, `chunks == ["hi\n"]`, and the result has `exit_code == 0`

#### Scenario: positional envs
- **WHEN** the unit test calls `sbx.commands.run("env", False, {"K": "1"})`
- **THEN** the fake `ProcessService` records a `Start` whose `envs` contain `K=1`

### Requirement: Code-interpreter helpers
The shim SHALL expose, sync and async:

- `list_code_contexts(request_timeout=None) -> list[Context]`
- `remove_code_context(context, request_timeout=None)`
- `restart_code_context(context, request_timeout=None)`

`context` is a `Context` or its id, and the methods delegate to the native methods. The native models SHALL gain:

- `Logs.to_json()`, returning `{"stdout": [...], "stderr": [...]}` as a JSON string
- `ExecutionError.to_json()`, returning `{"name", "value", "traceback"}` as a JSON string
- `CodeContext.from_json(data)`, which builds a context from `{"id", "language", "cwd"}` and raises `InvalidArgumentException` naming any missing key

#### Scenario: context lifecycle through the shim
- **WHEN** the unit test creates a context, lists contexts, restarts it and removes it through the shim
- **THEN** the fake `CodeService` records `CreateContext`, `ListContexts`, `RestartContext` and `DestroyContext` for that id, and the listing contains a `Context` with that id

#### Scenario: JSON round-trips
- **WHEN** the unit test serialises `Logs(stdout=["a"], stderr=["b"])` and `ExecutionError("E", "v", "tb")`, and parses `{"id": "c1", "language": "python", "cwd": "/home/user"}`
- **THEN** `json.loads(logs.to_json()) == {"stdout": ["a"], "stderr": ["b"]}`, `json.loads(err.to_json()) == {"name": "E", "value": "v", "traceback": "tb"}`, and `CodeContext.from_json(...)` equals `CodeContext("c1", "python", "/home/user")`

### Requirement: The 2.x shim is accepted against real AWS
The e2e `tests/e2e/test_m9_e2b_v2.py` SHALL run, as subprocesses against a real MicroVM of the M9 `rayito-base` image, the corpus of E2B 2.x example programs under `tests/e2e/e2b_corpus/sync/` and `tests/e2e/e2b_corpus/async/`. Each program SHALL cite the docs.e2b.dev page it follows and SHALL differ from an E2B program only in its import line.

The corpus SHALL cover:

- `create` and `run_code`
- `commands`, including a positional background `run` and `wait` callbacks
- `files` including `write_files`, and `watch_dir(include_entry=True)`
- `pty.create` + `pty.connect`
- `list/remove/restart_code_context`
- `get_info`, with every 2.x field populated or documented-empty
- the `list` paginator
- `pause()` returning `True` and then `False`
- `Sandbox.connect(id)` and the instance `connect()`
- `kill`
- `E2B().Sandbox.create()`

The e2e SHALL print the per-program wall time.

#### Scenario: corpus on AWS
- **WHEN** the e2e runs with `RAYITO_E2E=1`, `RAYITO_TEMPLATE` pointing at the M9 image and `RAYITO_ACCESS_TOKEN` set for the subprocesses
- **THEN** every sync and async program exits 0, `sbx.fork()` raises `UnimplementedError` against the live sandbox before any agent call, and the output prints `corpus_ok` equal to the number of programs

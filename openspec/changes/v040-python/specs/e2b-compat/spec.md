## MODIFIED Requirements

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

- **old agent, metrics:** `get_metrics(start=..., end=...)`, instance or class variant, when the agent answers `MetricsHistory` with `UNIMPLEMENTED` (an image that predates M9), with a reason telling to publish an M9 image. The native core already raises `UnimplementedError` for this (the generic unary table), so the shim's translation is a plain re-wrap: it never inspects a status code.
- **old agent, kernels:** `run_code(language=)` / `create_code_context(language=)` when the agent answers `UNIMPLEMENTED` because the image does not ship that kernel. The feature is `run_code(language=<given>)` or `create_code_context(language=<given>)`, the reason names `rayito-base-poly`, and the error is chained from the core's `UnimplementedError` (the generic unary table's `UNIMPLEMENTED` branch, not `InvalidArgumentException`). The shim only calls its kernel-remapping helper after catching `UnimplementedError` from the core, so there is no status-code check and no `None`-return path left; every other core error (e.g. the `INVALID_ARGUMENT` of an agent older than M9 that does not know `typescript`) is re-raised unchanged.

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
- **AND** the other two raise `UnimplementedError`, not `InvalidArgumentException`, whose reason names `rayito-base-poly` and whose `__cause__` is the core's own `UnimplementedError` (native, from the generic unary table), in the sync and the async shim

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

### Requirement: The E2B timeout surface maps to the server-enforced deadline
`rayito.e2b.Sandbox` and `rayito.e2b.AsyncSandbox` SHALL map E2B's timeout surface onto the native deadline of the `sandbox-timeout` capability:

- `sbx.set_timeout(timeout, request_timeout=None)` SHALL call the native `set_timeout` (`EXACT`).
- `Sandbox.set_timeout(sandbox_id, timeout, request_timeout=None, **kwargs)` SHALL call the native class variant with `access_token=` or `RAYITO_ACCESS_TOKEN`.
- `Sandbox.connect(sandbox_id, timeout=None, ...)`, with `timeout` positional after `sandbox_id` as in E2B, SHALL pass `timeout` to the native class `connect` (`AT_LEAST`).
- `Sandbox.beta_create(auto_pause=True)` SHALL launch in pause mode with `auto_resume=False`.
- `beta_create(auto_pause=True)` together with `lifecycle` SHALL raise `InvalidArgumentException`.
- The native `LifecycleUnsupportedException` SHALL be re-raised as `UnimplementedError` with feature `lifecycle` and a reason naming the M9 image, chained to the original. Since `LifecycleUnsupportedException` is itself a subclass of the core `UnimplementedError` (`sandbox-timeout`'s "The SDK fails closed on agents older than M9"), the shim's `except LifecycleUnsupportedException` keeps working unchanged: it discriminates by type, never by `isinstance(_, InvalidArgumentException)` or by inspecting a status code.
- A beyond-cap `set_timeout` SHALL raise the native `InvalidArgumentException` naming `max_lifetime` and 28800.

#### Scenario: set_timeout and connect through the shim
- **WHEN** the unit test calls `sbx.set_timeout(90)`, `Sandbox.set_timeout(sbx.sandbox_id, 120, access_token=t)` and `Sandbox.connect(sbx.sandbox_id, 300, access_token=t)` against the fake `rayd`
- **THEN** the fake `LifecycleService` recorded `SetTimeout{90000, EXACT}`, `SetTimeout{120000, EXACT}` and `SetTimeout{300000, AT_LEAST}` in that order

#### Scenario: older image through the shim
- **WHEN** the unit test calls `Sandbox.create()` against a fake `Health` without `lifecycle`
- **THEN** it raises `UnimplementedError` with `feature == "lifecycle"`, `err.__cause__` is a `LifecycleUnsupportedException`
- **AND** `isinstance(err.__cause__, UnimplementedError)` is `True` and `isinstance(err.__cause__, InvalidArgumentException)` is `False`
- **AND** the stubbed control plane recorded one `terminate_microvm`

#### Scenario: beta_create auto_pause
- **WHEN** the unit test calls `Sandbox.beta_create(auto_pause=True)`
- **THEN** the payload `lifecycle.on_timeout == "pause"`, `lifecycle.auto_resume` is false and the request carries an `idlePolicy` with `autoResumeEnabled: true`

## ADDED Requirements

### Requirement: Instance calls on rayito.e2b warn about ApiParams they cannot apply
`sbx.kill(**api_params)`, `sbx.pause(keep_memory=None, **api_params)` and `sbx.connect(timeout=None, **api_params)` (sync and async) SHALL validate every `ApiParam` they receive (the same rules as the class variants), then emit one `RayitoCompatWarning` for each of `headers`, `proxy` and `retries` that was given, plus `request_timeout` on `kill`/`pause` (which the native `kill()`/`pause(wait=)` do not accept), because an instance call operates on the channel and control plane this sandbox already built and cannot reconstruct them the way `Sandbox.<call>(sandbox_id, ...)` does. Each warning SHALL name only the parameter, never its value. `headers={}` (or any other empty/falsy value) SHALL NOT warn, since nothing was actually given to ignore. `connect()`'s `request_timeout` SHALL be applied, not warned about, and it SHALL reach the underlying resume/extend call. The class variants (`Sandbox.kill(id, ...)`, `Sandbox.pause(id, ...)`, `Sandbox.connect(id, ...)`) SHALL NOT emit these warnings: they apply every given `ApiParam` through the native class call.

#### Scenario: kill and pause warn about the channel/plane params
- **WHEN** the unit test calls `sbx.kill(retries=3, proxy="http://h:1")` and, separately, `sbx.pause(request_timeout=5)`
- **THEN** the first emits exactly two `RayitoCompatWarning`s (`retries`, `proxy`) and the sandbox still terminates
- **AND** the second emits one `RayitoCompatWarning` naming `request_timeout`, and the sandbox still suspends

#### Scenario: connect applies request_timeout and warns about the rest
- **WHEN** the unit test calls `sbx.connect(request_timeout=5)` and, separately, `sbx.connect(headers={"x-a": "1"})`
- **THEN** the first emits no `RayitoCompatWarning` and the resume call carries the 5 s timeout
- **AND** the second emits exactly one `RayitoCompatWarning` naming `headers`, and the warning text never contains `"1"`

#### Scenario: an empty mapping is not "given"
- **WHEN** the unit test calls `sbx.kill(headers={})` and `sbx.connect(headers={})`
- **THEN** neither emits a `RayitoCompatWarning` naming `headers`

#### Scenario: class variants apply everything without warning
- **WHEN** the unit test calls `Sandbox.kill(sbx.sandbox_id, retries=3, proxy="http://h:1", access_token=t)`
- **THEN** it emits no `RayitoCompatWarning` and the fake control plane received the retried, proxied call

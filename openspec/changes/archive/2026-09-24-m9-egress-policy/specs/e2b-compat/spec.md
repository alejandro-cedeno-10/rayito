## MODIFIED Requirements

### Requirement: E2B create kwargs map to Rayito or warn
`rayito.e2b.Sandbox(...)`, `Sandbox.create(...)` and `AsyncSandbox.create(...)` SHALL accept E2B's kwargs `template`, `timeout`, `metadata`, `envs`, `api_key`, `domain`, `debug`, `sandbox_id`, `request_timeout`, `proxy`, `secure`, `allow_internet_access`, `lifecycle` and, keyword-only, `network`, and SHALL map them as follows:
- `template`: passed as given (`None` → `RAYITO_TEMPLATE`).
- `timeout`: the logical deadline enforced by `rayd` (capability `sandbox-timeout`), **300 s** when `None`, under a platform cap given by the Rayito-only keyword-only `max_lifetime` (default `max(3600, min(timeout + 60, 28800))`, at most 28800, sent as `maximumDurationInSeconds`).
- `lifecycle` (`{"on_timeout": "kill"|"pause"|{"action": "kill"|"pause", "keep_memory": bool}, "auto_resume": bool}`, absent or `on_timeout=None` = `"kill"`): the native `on_timeout`, with `idle=None` for kill and `IdlePolicy(max_idle_seconds=300, auto_resume=<auto_resume>)` for pause, validated like E2B (an action other than `kill`/`pause`, an unknown key, `keep_memory` with `kill` and `auto_resume=True` without pause → `InvalidArgumentException`; `keep_memory: False` → `UnimplementedError`). Every shim launch sends a lifecycle block, so on an image older than M9 the shim terminates the just-launched VM and raises `UnimplementedError` naming the M9 image instead of running an unenforced timeout.
- `metadata` and `envs`: as is.
- `allow_internet_access` and `network`: `egress` is always `["INTERNET_EGRESS"]`. `allow_internet_access` is forwarded to the native `create()`. `False` means the native in-guest egress policy with `ALL_TRAFFIC` appended to `deny_out`: enforced on `rayito-base-caps`, and on any other image the MicroVM is terminated and `UnimplementedError` is raised. `network` is translated by the pure `map_network` into the native `network=`.
- `request_timeout`: as is.
- `sandbox_id`: `connect()` semantics.
- `secure=False`, `api_key`, `domain`, `debug` and `proxy` (any non-default value): one `RayitoCompatWarning` each, naming the kwarg and never its value, and otherwise ignored.

The shim SHALL default `ingress` to `["ALL_INGRESS"]`. It SHALL accept the native `create()` kwargs (`region`, `session`, `template_version`, `execution_role_arn`, `allowed_ports`, `ingress`, `logging`, `access_token`, `ready_timeout`, `reconnect_timeout`, `keep_on_failure`, `control_plane`, `transport`) as pass-through, and SHALL NOT accept `idle` or `egress`. The mapping SHALL be a pure, unit-tested function.

#### Scenario: E2B defaults on the wire
- **WHEN** the unit test creates `Sandbox(api_key="e2b_x", domain="e2b.dev", debug=True, proxy="http://p", secure=False)` against the stubbed control plane
- **THEN** exactly five `RayitoCompatWarning`s are emitted, none containing `e2b_x`, and the `run-microvm` request has `maximumDurationInSeconds == 3600`, no `idlePolicy`, a payload `lifecycle == {auto_resume: false, cap_s: 3600, on_timeout: "kill", timeout_s: 300}`, `ingressNetworkConnectors == [<ALL_INGRESS ARN>]` and `egressNetworkConnectors == [<INTERNET_EGRESS ARN>]`

#### Scenario: internet access off
- **WHEN** the unit test creates `Sandbox(allow_internet_access=False)` against a fake `rayd` reporting `egress_enforcement` `GUEST_ROUTES`, and again against one reporting `NONE`
- **THEN** both `run-microvm` requests keep `egressNetworkConnectors == [<INTERNET_EGRESS ARN>]` and carry `"network":{"enforce":true}` in the payload; the first sends `UpdateNetwork` with `deny_out=["0.0.0.0/0"]` and returns the sandbox, and the second raises the shim's `UnimplementedError` naming `rayito-base-caps` after `terminate-microvm` was recorded

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
`rayito.e2b.UnimplementedError` SHALL subclass `NotImplementedError` (and NOT `SandboxException`), carry `feature` and `reason`, and have a message naming both. It SHALL be the native `rayito.exceptions.UnimplementedError` re-exported (the shim passes its compatibility-doc reference), so `except rayito.UnimplementedError` catches what the shim raises and an error raised by the native SDK is caught by the E2B name.

The shim SHALL raise it, before any AWS or agent call, for:
- the class variant `Sandbox.get_metrics(sandbox_id)` when neither `access_token=` nor `RAYITO_ACCESS_TOKEN` provides the sandbox access token (the reason SHALL name both);
- `connection_config`;
- `run_code(language=...)` and `create_code_context(language=...)` with a language other than `python`, `bash`, `javascript`, `js`, `typescript` or `ts` (case-insensitive) or `None` (the reason SHALL name the available kernels and the `rayito-base-poly` variant);
- `Sandbox.list(query=SandboxQuery(metadata=...))` combined with a `state` filter other than `[SandboxState.RUNNING]`;
- `Sandbox.beta_create(...)` with a non-`None` `mcp`;
- a `lifecycle` whose object-form `on_timeout` carries `keep_memory: False` (`suspend-microvm` always snapshots memory).

`set_timeout` and `beta_create(auto_pause=...)` are no longer on this list: they map to the server-enforced deadline (capability `sandbox-timeout`). `upload_url` and `download_url` are mapped (requirement "The shim maps upload_url, download_url and the E2B 2.x file kwargs") and SHALL NOT be in this list. `Sandbox.beta_create(network=...)` SHALL map like `create(network=...)`.

`run_code(language=)` and `create_code_context(language=)` with an accepted name SHALL forward the normalised canonical name to the core SDK (`js` → `javascript`, `ts` → `typescript`), which decides at the agent whether the image ships it; when the agent answers `UNIMPLEMENTED` because the image does not ship that kernel, the shim SHALL raise `UnimplementedError` (feature `run_code(language=<given>)` or `create_code_context(language=<given>)`, reason naming `rayito-base-poly`) chained from the core's `InvalidArgumentException`, and SHALL re-raise every other core error unchanged.

It SHALL also raise it for `get_metrics(start=..., end=...)` (instance or class variant) when the agent answers `MetricsHistory` with `UNIMPLEMENTED` (an image that predates M9), with a reason telling to publish an M9 image.

No E2B feature SHALL be approximated silently: anything not mapped and not raising SHALL fail with `TypeError` at the call site.

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

#### Scenario: beta_create network maps to the egress policy
- **WHEN** the unit test calls `Sandbox.beta_create(network={"deny_out": ["0.0.0.0/0"]})` against a fake reporting `GUEST_ROUTES`, and `Sandbox.beta_create(mcp={"x": {}})`
- **THEN** the first raises no `UnimplementedError` and `UpdateNetwork` carries `deny_out=["0.0.0.0/0"]`, while the second raises `UnimplementedError` and makes no request

## ADDED Requirements

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

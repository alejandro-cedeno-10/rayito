## ADDED Requirements

### Requirement: Metadata travels in the run payload and rayd keeps it
`Sandbox.create(metadata=...)` SHALL serialise the mapping as the optional `"metadata"` key of the version-1 `runHookPayload` (omitted when empty, `sort_keys`, keys non-empty `str`, values `str`) and SHALL fail client-side with `InvalidArgumentException` naming `envs` and `metadata` when the serialised payload exceeds 4096 characters. `rayd` SHALL parse `metadata` as an optional `map<string, string>` of the payload (absent → empty; a non-string value → `MalformedJson`; unknown extra keys still ignored), SHALL keep it in the session from the single accepted `/run` for the life of the MicroVM, and SHALL log at most the number of keys — never a key or a value.

#### Scenario: metadata in the payload
- **WHEN** the SDK calls `Sandbox.create(metadata={"b": "2", "a": "1"}, envs={"E": "x"})`
- **THEN** the `run-microvm` request's `runHookPayload` decodes to a JSON object with `"metadata": {"a": "1", "b": "2"}` and `"v": 1`, and `create(metadata={})` produces a payload without the `metadata` key

#### Scenario: payload budget names both channels
- **WHEN** `metadata` and `envs` together push the serialised payload over 4096 characters
- **THEN** `create()` raises `InvalidArgumentException` before any AWS call and the message mentions both `envs` and `metadata`

#### Scenario: rayd parses and ignores the unknown
- **WHEN** a `/run` body carries `{"v":1,"token_sha256":"<hex>","metadata":{"a":"1"},"future":true}`
- **THEN** `parse_run_payload` succeeds with `metadata == {"a": "1"}`, and a payload whose `metadata` holds a non-string value fails with `MalformedJson`

### Requirement: Health echoes the sandbox metadata
`HealthService.Health` SHALL carry `metadata` (`map<string, string>`, field 11): empty before `/run` or when the payload had none, and exactly the parsed map afterwards, unchanged by `/suspend`, `/resume` or a repeated `/run`. The field is additive: `buf breaking` SHALL report no breaking change and an SDK reading an image that predates it SHALL see an empty map.

#### Scenario: echo after run
- **WHEN** an integration test calls `Health` before `/run`, then posts `/run` with `"metadata":{"env":"ci"}` and calls `Health` again
- **THEN** the first response has an empty `metadata` and the second has `{"env": "ci"}`

#### Scenario: echo survives a pause
- **WHEN** the e2e reads `get_health().metadata` after `pause()` and `resume()`
- **THEN** it equals the mapping passed to `create()` and `resume_generation` increased by one

### Requirement: SDK exposes metadata on the sandbox, its health and its info
The SDK SHALL expose `sbx.metadata -> dict[str, str]` (the map of the last `Health` seen; authoritative once `agent_ready` was `True`, because metadata is immutable for the life of the sandbox), `SandboxHealth.metadata: dict[str, str]`, and `SandboxInfo.metadata: dict[str, str] | None` where `None` means "not read from the agent" and `{}` means "read and empty". The instance `sbx.get_info()` SHALL return the refreshed `get-microvm` info with `metadata=sbx.metadata` and no extra RPC. The class variant `Sandbox.get_info(sandbox_id, *, read_metadata=True)` SHALL, when the MicroVM is `RUNNING`, mint a JWE for port 8080, send one `Health` with a 5 s deadline over a dedicated channel closed afterwards, and fill `metadata` when `agent_ready` is `True`; in any other state, or with `read_metadata=False`, it SHALL return `metadata=None` without touching the endpoint. `connect()` SHALL populate `sbx.metadata` from its readiness `Health`. `AsyncSandbox` SHALL offer the same surface.

#### Scenario: metadata after connect
- **WHEN** a second process calls `Sandbox.connect(sandbox_id, access_token=token)` on a sandbox created with `metadata={"run": "42"}`
- **THEN** `sbx.metadata == {"run": "42"}` and `sbx.get_info().metadata == {"run": "42"}`

#### Scenario: class get_info reads only running sandboxes
- **WHEN** the unit test calls `Sandbox.get_info(sandbox_id)` with the stubbed control plane answering `RUNNING` and the fake `rayd` echoing `{"a": "1"}`
- **THEN** exactly one `create-microvm-auth-token` call is made, the result has `metadata == {"a": "1"}`, and with the control plane answering `SUSPENDED` no token is minted and `metadata is None`

### Requirement: list filters by metadata client-side, on running sandboxes only
`Sandbox.list(metadata=...)` SHALL page `list-microvms` with `states=("RUNNING",)`, then for each item **sequentially** call `get-microvm`, `create-microvm-auth-token` (port 8080) and `Health` (5 s deadline, dedicated channel closed after the call), and SHALL yield the item with `metadata` filled iff `agent_ready` is `True` and every requested key equals the sandbox's value (subset match, exact strings). It SHALL skip items whose state is no longer `RUNNING` at `get-microvm` time, items whose `get-microvm` answers `ResourceNotFoundException`, and items whose `Health` answers `agent_ready=False` (logged at `info` with the sandbox id only); it SHALL raise `SandboxException` naming the sandbox id when a `Health` fails, so the caller never receives a silently incomplete list; it SHALL raise `InvalidArgumentException` when `states` contains anything other than `RUNNING`, because probing a suspended sandbox would wake it. Without `metadata`, `list()` SHALL behave as before with `metadata=None` on every item. The docstring and the docs SHALL state the cost: `n_running × (GetMicrovm + CreateMicrovmAuthToken + Health)` and one idle-window postponement per probed sandbox.

#### Scenario: only the matching running sandbox
- **WHEN** `list-microvms` returns three `RUNNING` items whose fake agents echo `{"env": "ci", "run": "1"}`, `{"env": "ci", "run": "2"}` and `{}` and the SDK calls `list(metadata={"env": "ci", "run": "2"})`
- **THEN** exactly the second item is yielded, its `metadata == {"env": "ci", "run": "2"}`, three tokens were minted and three `Health` calls were made, and every probe channel is closed afterwards

#### Scenario: state changed between calls
- **WHEN** one of the listed items answers `SUSPENDED` at `get-microvm` time
- **THEN** it is skipped without minting a token and the others are still probed

#### Scenario: a failing health is an error, not a gap
- **WHEN** a probed sandbox's `Health` answers `UNAVAILABLE` for the whole 5 s deadline
- **THEN** `list()` raises `SandboxException` whose message contains that sandbox id

#### Scenario: suspended states refused
- **WHEN** the SDK calls `list(metadata={"a": "1"}, states=["SUSPENDED"])`
- **THEN** it raises `InvalidArgumentException` before any AWS call

#### Scenario: real listing on AWS
- **WHEN** the e2e creates a sandbox with `metadata={"run": <uuid>}` and calls `Sandbox.list(metadata={"run": <uuid>})` and `Sandbox.list(metadata={"run": "other"})`
- **THEN** the first yields exactly that sandbox id with the metadata filled and the second yields nothing, and the wall time and the number of probed sandboxes are printed and recorded in `AWS_API_NOTES.md`

### Requirement: Metadata is not secret and is never logged
Metadata SHALL be documented as non-secret labels (it travels in `runHookPayload` like `envs` and is readable through `Health` by any principal able to mint a proxy JWE for the MicroVM). Neither `rayd` nor the SDK SHALL write metadata keys or values to logs, exceptions or `debug_error_string`; `SECURITY.md` T4 and T9 SHALL mention `metadata` next to `envs`.

#### Scenario: logging allowlist
- **WHEN** the `rayd` logging allowlist test runs after a `/run` with metadata
- **THEN** no log line contains a metadata key or value, and the `/run` line reports `metadata_keys` as a count

## MODIFIED Requirements

### Requirement: list filters by metadata client-side, on running sandboxes only
Without `index=` (the default), `Sandbox.list(metadata=...)` SHALL page `list-microvms` with `states=("RUNNING",)`, then for each item **sequentially** call `get-microvm`, `create-microvm-auth-token` (port 8080) and `Health` (5 s deadline, dedicated channel closed after the call), and SHALL yield the item with `metadata` filled iff `agent_ready` is `True` and every requested key equals the sandbox's value (subset match, exact strings). It SHALL skip items whose state is no longer `RUNNING` at `get-microvm` time, items whose `get-microvm` answers `ResourceNotFoundException`, and items whose `Health` answers `agent_ready=False` (logged at `info` with the sandbox id only); it SHALL raise `SandboxException` naming the sandbox id when a `Health` fails, so the caller never receives a silently incomplete list; it SHALL raise `InvalidArgumentException` when `states` contains anything other than `RUNNING`, because probing a suspended sandbox would wake it. With `index=DynamoDbIndex(...)` the metadata filter SHALL instead follow the `metadata-index` capability (no probes, any non-terminal state). Without `metadata`, `list()` SHALL behave as before with `metadata=None` on every item. The docstring and the docs SHALL state the cost: `n_running × (GetMicrovm + CreateMicrovmAuthToken + Health)` and one idle-window postponement per probed sandbox.

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
- **WHEN** the SDK calls `list(metadata={"a": "1"}, states=["SUSPENDED"])` without `index=`
- **THEN** it raises `InvalidArgumentException` before any AWS call

#### Scenario: suspended states accepted with the index
- **WHEN** the SDK calls `list(metadata={"a": "1"}, states=["SUSPENDED"], index=idx)`
- **THEN** no exception is raised at validation and no `Health` probe is made

#### Scenario: real listing on AWS
- **WHEN** the e2e creates a sandbox with `metadata={"run": <uuid>}` and calls `Sandbox.list(metadata={"run": <uuid>})` and `Sandbox.list(metadata={"run": "other"})`
- **THEN** the first yields exactly that sandbox id with the metadata filled and the second yields nothing, and the wall time and the number of probed sandboxes are printed and recorded in `AWS_API_NOTES.md`

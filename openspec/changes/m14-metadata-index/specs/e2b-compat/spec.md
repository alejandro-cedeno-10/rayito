## ADDED Requirements

### Requirement: SandboxQuery.metadata over PAUSED works with the index extension
The E2B shim's `Sandbox.list`/`AsyncSandbox.list` (TS `Sandbox.list`) SHALL accept the Rayito extension `index=` (TS `index`), also bound by `E2B(index=...)` (TS `new E2B({ index })`) and used only by `list`. With it, `SandboxQuery(metadata=..., state=[PAUSED])` SHALL map to the native indexed listing with E2B's state mapping (`PAUSED` = `SUSPENDING|SUSPENDED`, `RUNNING` = `PENDING|RUNNING`) and SHALL make no probe. Without it, the same `UnimplementedError` as before SHALL be raised, whose reason names `index=DynamoDbIndex(...)` and `optional-features.md`. `docs/site/docs/e2b-parity.md` row 40 SHALL read "divergente" saying the table is optional, only sandboxes created with the index appear, state comes from `list-microvms` and without the index it stays `UnimplementedError`; the status counts SHALL read 72 implementado, 21 divergente, 9 fuera por SPEC, 11 imposible.

#### Scenario: paused query with the index
- **WHEN** `Sandbox.list(query=SandboxQuery(metadata={"user": "42"}, state=[SandboxState.PAUSED]), index=idx).next_items()` runs over two paused and one running indexed sandboxes
- **THEN** it returns the two paused ones with state `paused` and the control plane records no `GetMicrovm` or `CreateMicrovmAuthToken`

#### Scenario: without the index the reason names the option
- **WHEN** the same query runs without `index=`
- **THEN** `UnimplementedError` is raised and its message contains `index=DynamoDbIndex` and `optional-features`

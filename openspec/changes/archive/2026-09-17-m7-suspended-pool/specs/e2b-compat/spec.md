## ADDED Requirements

### Requirement: The shim is unaware of pools
`rayito.e2b.Sandbox(...)`, `rayito.e2b.Sandbox.create(...)` and `rayito.e2b.AsyncSandbox.create(...)` SHALL NOT accept a `pool` kwarg (it SHALL fail as any unknown kwarg does, with `TypeError`, never silently ignored and never mapped), `rayito.e2b.__all__` SHALL contain no name containing `Pool`, and the E2B kwarg mapping of the shim SHALL be byte-for-byte unchanged by `m7-suspended-pool`. E2B's SDK has no pool surface, so there is nothing to emulate; a program that wants pooled sandboxes uses the native `rayito.SandboxPool`.

#### Scenario: pool kwarg rejected by the shim
- **WHEN** a unit test calls `rayito.e2b.Sandbox.create(pool=object())` and `rayito.e2b.AsyncSandbox.create(pool=object())`
- **THEN** both raise `TypeError` mentioning `pool` and no `run-microvm` is issued

#### Scenario: no pool name exported
- **WHEN** a unit test inspects `rayito.e2b.__all__` and `rayito.e2b.exceptions.__all__`
- **THEN** no entry contains the substring `Pool`

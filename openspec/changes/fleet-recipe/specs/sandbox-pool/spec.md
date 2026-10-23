## ADDED Requirements

### Requirement: PoolConfig can give every slot a logical deadline
`PoolConfig` SHALL accept `max_lifetime: int | None = None` and `on_timeout: "kill" | None = None` (TypeScript `maxLifetimeMs` and `onTimeout`), declared after every existing field so positional construction keeps working. They SHALL map onto the `Sandbox.create()` options of the same name: `timeout` becomes the logical deadline and `max_lifetime` the platform cap (by default `timeout + 60`). `__post_init__` (and `validatePoolConfig`) SHALL raise `InvalidArgumentException` when `on_timeout` is anything other than `None` or `"kill"`, and SHALL validate `max_lifetime` with the same rules as `create()` (`SandboxLifetimeException` above 28 800 s, `InvalidArgumentException` below 120 s or below `timeout`). The launch arguments SHALL carry `max_lifetime` and `on_timeout` only when they are set, so a pool that does not set them launches exactly as before.

#### Scenario: not set, launched as before
- **WHEN** a unit test derives the launch arguments of `PoolConfig(size=1, timeout=7200)`
- **THEN** they have neither `max_lifetime` nor `on_timeout`

#### Scenario: set, forwarded to create
- **WHEN** a unit test derives the launch arguments of `PoolConfig(size=1, timeout=7200, max_lifetime=7260, on_timeout="kill")`
- **THEN** they carry `timeout=7200`, `max_lifetime=7260` and `on_timeout="kill"`

#### Scenario: pause is rejected
- **WHEN** a unit test builds `PoolConfig(size=1, timeout=7200, on_timeout="pause")`
- **THEN** `InvalidArgumentException` is raised and its message names `on_timeout`

#### Scenario: cap below the timeout is rejected
- **WHEN** a unit test builds `PoolConfig(size=1, timeout=7200, max_lifetime=7000)`
- **THEN** `InvalidArgumentException` is raised and its message names `max_lifetime`

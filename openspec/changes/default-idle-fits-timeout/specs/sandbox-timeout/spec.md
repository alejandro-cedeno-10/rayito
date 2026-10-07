## ADDED Requirements

### Requirement: The default idle policy adapts to a short timeout
When the caller does not pass `idle` (Python: the `DEFAULT_IDLE_POLICY` default; TypeScript: `idle` absent or `undefined`) and the default window of 300 s is not below the bound it is checked against (`timeout` without a lifecycle block, `max_lifetime` with one), `create()` SHALL NOT raise. Without a lifecycle block and with `on_timeout="kill"` the default policy SHALL be dropped, so the `run-microvm` request carries no `idlePolicy` and the sandbox ends at its timeout. With `on_timeout="pause"` the platform idle SHALL use `max_idle_seconds = PAUSE_DEFAULT_IDLE_FALLBACK_SECONDS` (60, the API minimum). An explicit `idle` SHALL keep the existing validation and raise `InvalidArgumentException` / `InvalidArgumentError` when it does not fit.

#### Scenario: short timeout without idle
- **WHEN** a unit test calls `create(timeout=120)` without `idle` against the stubbed control plane
- **THEN** the request has `maximumDurationInSeconds == 120` and no `idlePolicy`

#### Scenario: short pause without idle
- **WHEN** `create(timeout=120, on_timeout="pause")` is called without `idle`
- **THEN** the request has `maximumDurationInSeconds == 180` and `idlePolicy == {maxIdleDurationSeconds: 60, suspendedDurationSeconds: 120, autoResumeEnabled: true}`

#### Scenario: explicit idle that does not fit
- **WHEN** `create(timeout=120, idle=IdlePolicy())` is called
- **THEN** it raises `InvalidArgumentException` and no `run-microvm` is recorded

## ADDED Requirements

### Requirement: agent_pool_warmup returns the runtime's warm-up steps
Both SDKs SHALL export `agent_pool_warmup(runtime="opencode")` / `agentPoolWarmup(runtime)` returning the runtime's `warmup_steps()`, which load the runtime into the page cache before the slot is parked (option C). Neither SHALL accept a `serve` option, and no step SHALL start a resident server. Both SDKs SHALL produce the steps of `testdata/agent/pool-warmup.json` for every runtime it lists.

#### Scenario: warm-up steps per runtime
- **WHEN** a unit test reads `agent_pool_warmup(runtime)` for each case of `testdata/agent/pool-warmup.json`
- **THEN** the steps' `cmd`, `background` and `tag` equal the case's steps, and for `"opencode"` there is a single foreground `opencode --version` step

#### Scenario: serve is gone
- **WHEN** a caller passes `serve=True` to `agent_pool_warmup`
- **THEN** Python raises `TypeError` and TypeScript does not type-check

### Requirement: agent.prepare() starts warm-up steps without waiting
`sbx.agent.prepare(runtime=...)` SHALL take no `serve` option and SHALL start the runtime's warm-up steps in the background without waiting for any of them; a step with `background=True` SHALL run with no timeout, so a long-lived process a runtime starts is not killed after `timeout_seconds`.

#### Scenario: prepare with a background step
- **WHEN** a caller runs `sbx.agent.prepare(runtime=r)` with a runtime whose warm-up has a `background=True` step
- **THEN** that step is started with `background=True` and no timeout, every handle is disconnected, and `prepare()` returns without waiting

## REMOVED Requirements

### Requirement: agent_pool_warmup prepares pool slots for the agent
**Reason**: `serve=True` (option D) is removed in 0.9.0.
**Migration**: Use `agent_pool_warmup(runtime)` (option C) or the normal start.

### Requirement: agent.prepare() leaves background steps running
**Reason**: `prepare(serve=True)` (option D) is removed in 0.9.0.
**Migration**: Call `prepare(runtime=...)` without `serve`.

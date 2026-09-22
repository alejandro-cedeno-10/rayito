## MODIFIED Requirements

### Requirement: E2B features without an AWS primitive raise UnimplementedError
`rayito.e2b.UnimplementedError` SHALL subclass `NotImplementedError` (and NOT `SandboxException`), carry `feature` and `reason`, and have a message naming both. The shim SHALL raise it, before any AWS or agent call, for: `set_timeout` (instance and class variant), `upload_url`, `download_url`, `get_metrics(start=..., end=...)`, the class variant `Sandbox.get_metrics(sandbox_id)`, `connection_config`, `run_code(language=...)` and `create_code_context(language=...)` with a language other than `python` (case-insensitive) or `None`, `Sandbox.list(next_token=...)`, `Sandbox.list(query=SandboxQuery(metadata=...))` combined with a `state` filter other than `[SandboxState.RUNNING]`, and `Sandbox.beta_create(...)` with a non-`None` `auto_pause`, `network` or `mcp`. The `set_timeout` error's `reason` SHALL name both `UpdateMicrovm` (why it cannot exist, ADR-007) and `rayito.Sandbox.reincarnate()` (the supported path: checkpoint of `/home/user` to S3 plus a new VM, `sdk-persistence`); the shim SHALL NOT gain a `persist` kwarg or any persistence method (not E2B surface). No E2B feature SHALL be approximated silently: anything not mapped and not raising SHALL fail with `TypeError` at the call site.

#### Scenario: set_timeout
- **WHEN** the unit test calls `sbx.set_timeout(60)` and `Sandbox.set_timeout(sbx.sandbox_id, 60)`
- **THEN** both raise `UnimplementedError`, `isinstance(err, NotImplementedError)` is `True`, `isinstance(err, SandboxException)` is `False`, `err.feature == "set_timeout"` and the message mentions both `UpdateMicrovm` and `reincarnate`

#### Scenario: every listed feature
- **WHEN** the parametrised unit test exercises each feature in the list above
- **THEN** each raises `UnimplementedError` and no request reaches the stubbed control plane or the fake `rayd`

#### Scenario: python is the only kernel
- **WHEN** the unit test calls `sbx.run_code("1", language="js")` and `sbx.run_code("1", language="Python")`
- **THEN** the first raises `UnimplementedError` and the second executes on the default context

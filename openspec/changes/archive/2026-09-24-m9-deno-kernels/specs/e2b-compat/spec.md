## MODIFIED Requirements

### Requirement: E2B features without an AWS primitive raise UnimplementedError
`rayito.e2b.UnimplementedError` SHALL subclass `NotImplementedError` (and NOT `SandboxException`), carry `feature` and `reason`, and have a message naming both. The shim SHALL raise it, before any AWS or agent call, for: `set_timeout` (instance and class variant), `upload_url`, `download_url`, `get_metrics(start=..., end=...)`, the class variant `Sandbox.get_metrics(sandbox_id)`, `connection_config`, `run_code(language=...)` and `create_code_context(language=...)` with a language other than `python`, `bash`, `javascript`, `js`, `typescript` or `ts` (case-insensitive) or `None` (the reason SHALL name the available kernels and the `rayito-base-poly` variant), `Sandbox.list(next_token=...)`, `Sandbox.list(query=SandboxQuery(metadata=...))` combined with a `state` filter other than `[SandboxState.RUNNING]`, and `Sandbox.beta_create(...)` with a non-`None` `auto_pause`, `network` or `mcp`. `run_code(language=)` and `create_code_context(language=)` with an accepted name SHALL forward the normalised canonical name to the core SDK (`js` → `javascript`, `ts` → `typescript`), which decides at the agent whether the image ships it; when the agent answers `UNIMPLEMENTED` because the image does not ship that kernel, the shim SHALL raise `UnimplementedError` (feature `run_code(language=<given>)` or `create_code_context(language=<given>)`, reason naming `rayito-base-poly`) chained from the core's `InvalidArgumentException`, and SHALL re-raise every other core error unchanged. No E2B feature SHALL be approximated silently: anything not mapped and not raising SHALL fail with `TypeError` at the call site.

#### Scenario: set_timeout
- **WHEN** the unit test calls `sbx.set_timeout(60)` and `Sandbox.set_timeout(sbx.sandbox_id, 60)`
- **THEN** both raise `UnimplementedError`, `isinstance(err, NotImplementedError)` is `True`, `isinstance(err, SandboxException)` is `False`, `err.feature == "set_timeout"` and the message mentions `UpdateMicrovm`

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

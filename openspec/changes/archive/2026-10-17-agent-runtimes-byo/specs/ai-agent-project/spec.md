## ADDED Requirements

### Requirement: AgentProject is one typed, validated description of an agent
Both SDKs SHALL export `AgentProject` (Python frozen dataclass; TypeScript class with an options object) with `name`, `runtime` (registry name or runtime object), `spec` (`AgentSpec`), `gateways`, `source`, `exclude`, `limits` (`AgentLimits`), `serve` (`ServeOptions`: `port=AGENT_SERVICE_PORT`, `timeout_seconds=DEFAULT_AGENT_DEPLOY_TIMEOUT_SECONDS`, `idle=DEFAULT_IDLE_POLICY`, `max_concurrency=AGENT_SERVICE_MAX_CONCURRENCY`, `metadata`), `allow_internet_access=False`, `egress`, `memory_mib=AGENT_MIN_MEMORY_MIB`, `base=DEFAULT_AGENT_TEMPLATE_BASE` and `template`. Python SHALL also export `AsyncAgentProject` with the same constructor. Construction SHALL be pure and SHALL validate before any call: `name` with the agent name rules, every gateway the spec needs present in `gateways`, `serve.timeout_seconds` at most the platform maximum, `serve.port` not 8080 or 9000, and the runtime/provider combination supported by the provider matrix.

#### Scenario: missing gateway
- **WHEN** a caller builds an `AgentProject` whose `spec.model.gateway` is `"bedrock"` and `gateways` has no `"bedrock"` entry
- **THEN** it raises `InvalidArgumentException` / `InvalidArgumentError` and no AWS client is created

#### Scenario: reserved port
- **WHEN** a caller builds `ServeOptions(port=8080)`
- **THEN** it raises `InvalidArgumentException` / `InvalidArgumentError`

### Requirement: rayito-agent.toml and rayito-agent.json load into the same AgentProject
`AgentProject.load(path)` SHALL accept a file or a directory; for a directory it SHALL use the first of `AGENT_PROJECT_FILE_NAMES` (`rayito-agent.toml`, `rayito-agent.json`) that exists. The document SHALL declare `schema = "rayito.agent-project/1"`. Unknown keys at any level SHALL raise `InvalidArgumentException` / `InvalidArgumentError` whose message names the dotted path; relative paths SHALL resolve against the file's directory and SHALL NOT escape it; no key SHALL accept a secret value, and a gateway's `secret` SHALL be a secret name. Python SHALL parse TOML with `tomllib`; TypeScript SHALL load the optional peer `smol-toml` only when reading a `.toml` file and SHALL fail with a message naming the peer when it is missing; JSON SHALL need no dependency. `from_dict`/`fromObject` SHALL be pure and `to_dict`/`toObject` SHALL return the canonical form that round-trips. Both SDKs SHALL produce the canonical form of every case in `testdata/agent-project/valid/` and the error path and code of every case in `testdata/agent-project/invalid/cases.json`, and `testdata/agent-project/schema.json` SHALL accept every valid case and reject every invalid one.

#### Scenario: shared vectors
- **WHEN** the unit tests of both SDKs load every file under `testdata/agent-project/valid/`
- **THEN** each `to_dict()` / `toObject()` equals its `.expected.json`

#### Scenario: misspelt key
- **WHEN** a file has `[runtime] entrpoint = "agent:agent"`
- **THEN** loading raises an invalid-argument error whose message contains `runtime.entrpoint`

#### Scenario: path escape
- **WHEN** a file has `[source] path = "../other"`
- **THEN** loading raises an invalid-argument error and reads nothing outside the file's directory

### Requirement: build, run, deploy and export are one call each
`AgentProject` SHALL provide `build(bucket=...)` (the image through `AgentTemplate` with the project's runtime and `source`), `run(prompt, ...)` (a sandbox from the project's image, the project's gateways and network options, one `sbx.agent.run`, then `kill()`, also on error), `deploy()` (an `AgentDeployment`, see `ai-agent-service`) and `export(target=, out_dir=)` (see `ai-agent-service`). The Python async class SHALL expose the same operations as coroutines, with identical parameter names, order and defaults (checked with `inspect.signature`), and TypeScript the same operations in camelCase with milliseconds and an options object.

#### Scenario: run kills its sandbox on failure
- **WHEN** a unit test runs `project.run("hola")` over a fake sandbox whose agent run raises `AgentException`
- **THEN** the exception propagates and the fake recorded exactly one `kill()`

#### Scenario: sync and async signatures match
- **WHEN** a unit test compares `inspect.signature` of every public method of `AgentProject` and `AsyncAgentProject`
- **THEN** names, order and defaults are equal

### Requirement: AgentProject operations that cost are off until called
Constructing or loading an `AgentProject`, calling `export()` or running `rayito agent init` SHALL make no AWS call and create no AWS client. `build()`, `run()` and `deploy()` SHALL be the only operations that create resources, and the docstring/TSDoc of `AgentProject` and of `deploy()` SHALL carry the "Coste y activación" block with its six entries; TypeScript SHALL register it in `clients/typescript/scripts/cost-declarations/agent-project.json`. A test in each SDK SHALL assert zero calls for construction, loading and export.

#### Scenario: zero calls
- **WHEN** a unit test loads a project file and exports it to a temporary directory with every AWS client factory replaced by a recorder
- **THEN** the recorder saw no client and no call

## MODIFIED Requirements

### Requirement: The AgentRuntime port resolves by name or by object
Both SDKs SHALL define a pure `AgentRuntime` port (`build_config`/`buildConfig`, `command`, `new_state`/`newState`, `parse_line`/`parseLine`, `finish`, `template_steps`/`templateSteps`, `warmup_steps`/`warmupSteps`) and a `nombre -> AgentRuntime` registry. `warmup_steps()`/`warmupSteps()` SHALL take no argument. `runtime=`/`runtime` SHALL accept either a registered name or any object that implements the port. An unregistered name SHALL raise `UnimplementedError`/`UnimplementedError` naming the runtime, never a raw `KeyError`/`undefined` access; any other value SHALL raise `InvalidArgumentException`/`InvalidArgumentError`. The registry SHALL contain `opencode`, `deepagents`, `langgraph`, `strands`, `strands-harness` and `hermes`; `custom` SHALL NOT be a registry name because `CustomRuntime` needs configuration, and `"custom"` SHALL raise `InvalidArgumentException`/`InvalidArgumentError` telling the caller to pass a `CustomRuntime` object. The signature of `AgentRuntime` SHALL NOT change in this version, so an object written for 0.13 keeps working with `sbx.agent.run`.

#### Scenario: an unregistered runtime name
- **WHEN** `sbx.agent.run(prompt, spec=spec, runtime="crewai")` is called
- **THEN** it raises `UnimplementedError` naming `runtime="crewai"`, before any RPC

#### Scenario: a caller's own runtime object
- **WHEN** `runtime=` is an object that implements every method of `AgentRuntime`
- **THEN** `sbx.agent.run()` uses it directly, with no registry lookup

#### Scenario: the new names resolve
- **WHEN** a unit test resolves `"langgraph"`, `"strands"`, `"strands-harness"` and `"hermes"` in each SDK
- **THEN** it gets a `DeepAgents` with a required entrypoint, a `StrandsRuntime`, a `StrandsRuntime` with `harness` true and a `HermesRuntime`

#### Scenario: custom needs an object
- **WHEN** a caller passes `runtime="custom"`
- **THEN** it raises `InvalidArgumentException` / `InvalidArgumentError` and no RPC is made

## ADDED Requirements

### Requirement: Optional InstallableRuntime and ServableRuntime ports
Both SDKs SHALL define two optional, pure ports next to `AgentRuntime`: `InstallableRuntime` (`install_steps(target)`/`installSteps(target)`, `context_files`/`contextFiles`, `manifest_entry`/`manifestEntry`, `smoke_checks`/`smokeChecks`, `prefetch_paths`/`prefetchPaths`) and `ServableRuntime` (`host_config(spec, wiring=, workdir=)`/`hostConfig(spec, { wiring, workdir })`). `InstallTarget` SHALL distinguish `microvm` and `container`; `ModelWiring` SHALL be `GatewayWiring` (gateway URLs by name) or `DirectWiring` (export, no gateway). `OpenCodeRuntime` and `DeepAgents` SHALL implement both ports without changing any command, configuration file or event they produce, so `testdata/agent/opencode-run-commands.json`, `deepagents-run-commands.json`, `opencode-config/` and `deepagents-config.json` stay byte-identical. Passing a runtime that lacks a port to an operation that needs it (`AgentTemplate`, `AgentProject.deploy`, `AgentProject.export`) SHALL raise `InvalidArgumentException`/`InvalidArgumentError` naming the missing port, before any RPC.

#### Scenario: existing vectors do not move
- **WHEN** the unit tests of both SDKs run the existing OpenCode and deepagents vectors after the change
- **THEN** every vector passes unchanged

#### Scenario: a task-only runtime cannot be deployed
- **WHEN** a caller deploys an `AgentProject` whose runtime object implements only `AgentRuntime`
- **THEN** it raises `InvalidArgumentException` naming `ServableRuntime` and no sandbox is created

### Requirement: Strands Agents runs through the same contract
Both SDKs SHALL export `StrandsRuntime(entrypoint=None, harness=False, language="python")` (TypeScript options object). It SHALL run through the agent host and the Rayito JSONL protocol v1, in task mode (`sbx.agent.run`) and in service mode. Without `entrypoint` the host SHALL build a Strands `Agent` (or, with `harness=True`, `create_harness(...)`) from `AgentSpec`; with `entrypoint="module:attr"` the attribute SHALL be an agent instance or a factory receiving a context whose `model` is already wired to the gateway. `language="typescript"` SHALL run the user's module through the Node bridge and require Node in the image. Installs SHALL use the pinned, hash-checked requirements whose versions and sha256 are in `limits.json`.

#### Scenario: Strands over a Bedrock gateway
- **WHEN** a unit test builds the host configuration of `StrandsRuntime()` for an `AgentSpec` with `provider="bedrock"` and a gateway URL
- **THEN** the configuration points the Bedrock client's endpoint at the gateway URL, sets the bearer token to `MODEL_CREDENTIAL_PLACEHOLDER` and contains no other credential

#### Scenario: invalid entrypoint
- **WHEN** a caller builds `StrandsRuntime(entrypoint="not a path")`
- **THEN** it raises `InvalidArgumentException` / `InvalidArgumentError` before any RPC

### Requirement: Hermes Agent runs through the same contract
Both SDKs SHALL export `HermesRuntime(toolsets=HERMES_DEFAULT_TOOLSETS, skills_dir=None)`. The host SHALL drive Hermes through its API server bound to `127.0.0.1` on `AGENT_HERMES_API_PORT`, with an `API_SERVER_KEY` generated at random on every start and passed only through the child's environment, and SHALL never put the prompt in argv. Hermes SHALL run as `user` with `HERMES_HOME` under `AGENT_STATE_DIR`, `HERMES_IGNORE_USER_CONFIG=1` and only the configured toolsets. Hermes SHALL be installed from the pinned commit verified with `git rev-parse HEAD` and a hash-checked requirements file. A provider that cannot be reached through a gateway with a placeholder key SHALL raise `InvalidArgumentException` / `InvalidArgumentError` before any RPC.

#### Scenario: Hermes with an Anthropic gateway
- **WHEN** a unit test builds the host configuration of `HermesRuntime()` for `provider="anthropic"`
- **THEN** `ANTHROPIC_BASE_URL` is the gateway URL, `ANTHROPIC_API_KEY` is `MODEL_CREDENTIAL_PLACEHOLDER`, and the API server host is `127.0.0.1`

#### Scenario: Hermes events become Rayito events
- **WHEN** the host's Hermes driver replays `testdata/agent/hermes-v0.22.2-events.jsonl`
- **THEN** it emits the v1 events of the matching expected file, ending with `done`

### Requirement: CustomRuntime runs the user's own agent
Both SDKs SHALL export `CustomRuntime(install=(), requirements=None, command=None, entrypoint=None, app=None, protocol="rayito-v1", python=None)` and `HttpApp(command, port, health_path="/ping", invoke_path="/invocations", protocol="agentcore")`. Exactly one of `command`, `entrypoint` and `app` SHALL be set. `command` SHALL receive the request on stdin and either write v1 lines (`rayito-v1`) or plain text (`text`, wrapped as `text_delta`, `text` and `done`); `entrypoint` SHALL be a Python `module:attr` callable hosted in process; `app` SHALL be a server the host starts, health-checks and proxies, translating `openai-chat` streams, while `raw` is exposed without translation. `HttpApp.port` SHALL NOT be 8080, 9000 or `AGENT_SERVICE_PORT`.

#### Scenario: more than one entry kind
- **WHEN** a caller builds `CustomRuntime(command=["./run.sh"], entrypoint="app:main")`
- **THEN** it raises `InvalidArgumentException` / `InvalidArgumentError`

#### Scenario: plain text command
- **WHEN** the host runs a `command` runtime with `protocol="text"` that prints `hola` and exits 0
- **THEN** the events are `text_delta("hola")`, `text("hola")` and `done`

### Requirement: Framework defaults are safe and content-free
The adapters and the host SHALL turn off what sends data out or runs model-written code unisolated by default: no `OTEL_*` exporter for Strands; for the Strands harness `programmatic_tool_caller` and web search off, `web_fetch` and the other built-in tools following `AgentPermissions` (with `DEFAULT_DENIED_TOOLS`), sessions and memory under `AGENT_STATE_DIR`, and no `"ask"` intervention; for Hermes the settings of its requirement; and `allow_internet_access=False` unless the project opens it. Logs of the host SHALL carry only ids, states, durations, byte counts and codes.

#### Scenario: harness defaults
- **WHEN** a unit test builds the host configuration of `StrandsRuntime(harness=True)` with default permissions
- **THEN** `programmatic_tool_caller` and `web_search` are false, `web_fetch` is false, and the session and memory directories are under `AGENT_STATE_DIR`

# ai-agent-project Specification

## Purpose
TBD - created by archiving change agent-runtimes-byo. Update Purpose after archive.

## Requirements

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

### Requirement: AgentDeployment exposes its access token and can pause and resume
`AgentDeployment` and `AsyncAgentDeployment` (and the TypeScript `AgentDeployment`) SHALL expose `access_token` / `accessToken`, the access token of the deployment's sandbox (the one passed to `deploy()`, `RAYITO_ACCESS_TOKEN` or the generated one), and SHALL provide `pause(wait=True)` and `resume(wait=True)` (`pause({ wait })`, `resume({ wait })`) that call the sandbox's `pause` and `resume` and keep the sandbox alive. `pause()` SHALL return `False` when the sandbox was already suspended. The token SHALL never be written to a log, an error or a CLI output.

#### Scenario: reconnect with the returned token
- **WHEN** a caller deploys without `access_token`, reads `deployment.access_token` and calls `AgentDeployment.connect(sandbox_id, access_token=token)` from another process
- **THEN** the second client can invoke the agent and the first can still stop it

#### Scenario: pause and resume
- **WHEN** a caller calls `pause()` then `resume()`
- **THEN** the sandbox's `pause` and `resume` ran once each with `wait` forwarded and no `kill` was called

### Requirement: deploy can keep the sandbox of a failed startup
`AgentProject.deploy` SHALL accept `keep_on_failure` / `keepOnFailure` (`False` by default). With it, a failure after the sandbox was created SHALL NOT terminate the sandbox and SHALL annotate the error with the sandbox id and how to terminate it (never the token); a `BaseException` that is not an `Exception` SHALL still terminate it. It SHALL raise `InvalidArgumentException` before creating anything when no `access_token` (argument or `RAYITO_ACCESS_TOKEN`) is known. `rayito agent deploy --keep-on-failure` SHALL pass it.

#### Scenario: kept on failure
- **WHEN** `deploy(access_token=..., keep_on_failure=True)` fails because the host left a failed marker
- **THEN** no `kill()` ran and the error names the sandbox id

#### Scenario: no token to come back with
- **WHEN** `deploy(keep_on_failure=True)` runs with no access token and no `RAYITO_ACCESS_TOKEN`
- **THEN** it raises `InvalidArgumentException` and creates no sandbox

### Requirement: An HttpApp tells when its own routes are not exposed
`HttpApp` SHALL provide `hides_own_routes` / `hidesOwnRoutes`: `true` when the protocol is neither `raw` nor `json` and `health_path` or `invoke_path` differ from `AGENT_SERVICE_HEALTH_PATH` and `AGENT_SERVICE_INVOKE_PATH`. `rayito agent deploy` SHALL print a warning naming the protocol and the routes, and pointing to `protocol = "raw"`, when it is `true`; the default protocol SHALL NOT change.

#### Scenario: custom routes with the default protocol
- **WHEN** an app with `health_path="/health"` and the default protocol is deployed from the CLI
- **THEN** the CLI prints a warning mentioning `/health` and `protocol = "raw"`

#### Scenario: raw
- **WHEN** the same app uses `protocol="raw"`
- **THEN** no warning is printed

### Requirement: The project file gains the zero-config tables
`rayito.agent-project/1` SHALL accept, additively and with unknown keys still rejected with their path, `[scaling]` (`min_vms`, `max_vms`, `warm_vms`, `sessions`, `scale_in_after_seconds`), `[env]` (`NAME = "value"`), `[network]` (`allow_out`, `deny_out`), `[deploy]` (`region`, `bucket`, `base_image_version`), `[serve]` `queue_max` and `queue_timeout_seconds`, `[runtime]` `language`, `python`, `node` and `entry`, and `[runtime.app]` `request_field` and `response_field`. `[runtime] kind` SHALL be optional when it is detectable. A file valid in the previous 0.14 build SHALL load unchanged.

#### Scenario: previous file loads
- **WHEN** a project file that has no new key is loaded
- **THEN** it loads with the same values as before

#### Scenario: unknown key
- **WHEN** `[scaling] max_vm = 2` is loaded
- **THEN** it raises `AgentProjectException` with code `unknown_key` and the path `scaling.max_vm`

### Requirement: Project environment never carries a secret
`env` SHALL reject names matching the secret-like names and values with the secret prefixes with `code="secret_value"`, and the error SHALL never contain the value. Credentials SHALL keep going through gateways.

#### Scenario: secret in env
- **WHEN** `env={"API_KEY": <a value with a known secret prefix>}`
- **THEN** it raises `AgentProjectException(code="secret_value")` without the value

### Requirement: The HTTP app can use the json protocol
`HttpApp(protocol="json", request_field="prompt", response_field=None)` SHALL be valid. Explicit `HttpApp` SHALL keep `agentcore` as default and `hides_own_routes` SHALL be `False` for `json`.

#### Scenario: default unchanged
- **WHEN** `HttpApp(command=[...])` is built without a protocol
- **THEN** its protocol is `agentcore`

### Requirement: deploy_agent is a one-call facade over detection and deploy
`deploy_agent(path=".", *, project=None, options=None, overrides=None, region=None, session=None)`, `deploy_agent_async` and `deployAgent(path, {...})` SHALL detect (or load) the project, resolve the AWS defaults and deploy it, and return the fleet. `deploy_agent` and `deploy_agent_async` SHALL have identical signatures (checked with `inspect.signature`). `AgentProject.deploy()` SHALL be unchanged.

#### Scenario: signatures match
- **WHEN** the signatures of `deploy_agent` and `deploy_agent_async` are compared
- **THEN** they have the same parameters in the same order with the same defaults

### Requirement: AWS defaults are resolved in a fixed order and created only on request
For region, base image, bucket, model secret and access token, `deploy_agent` SHALL use: explicit argument, project file `[deploy]`, environment variable, discovery, then a Spanish error before anything is created. The bucket SHALL never be created. A missing base image SHALL be published only with confirmation (`--yes` when not a TTY). A secret SHALL be created only with `create_secret_from_env` (`--create-secret`) and its value SHALL be read from the local environment variable of the provider and never written to disk or logs. A generated access token SHALL be saved by the CLI to a file with mode `0600` that the default source excludes skip.

#### Scenario: no secret and no option
- **WHEN** no secret exists under the convention name and `--create-secret` is absent
- **THEN** the deploy fails before creating any resource and the message names both options

#### Scenario: token file
- **WHEN** the CLI generates a token
- **THEN** the file mode is `0600`

### Requirement: Default source excludes keep local state out of the image
The default source excludes SHALL cover `.git`, `.env`, `.env.*`, `.venv`, `venv`, `node_modules`, `__pycache__`, `*.pyc` and `.rayito`. Dependencies SHALL be installed in the image, never copied from the laptop.

#### Scenario: env file
- **WHEN** a project holds `.env`
- **THEN** it is not part of the uploaded source

## ADDED Requirements

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

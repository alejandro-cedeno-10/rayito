## ADDED Requirements

### Requirement: The model id is optional unless Rayito uses it
`[model] id` SHALL be optional in `testdata/agent-project/schema.json`, in `_read_model` / the TypeScript reader and in `AgentModel` (Python `id: str | None`, TypeScript `id?: string`), whose positional order SHALL NOT change. `[model]` SHALL accept the optional keys `id_env`, `base_url_env` and `api_key_env`, each a variable name matching `^[A-Za-z_][A-Za-z0-9_]*$` and never a value; a name that is also a key of `[env]` SHALL raise `conflict` on that path. Both SDKs SHALL decide with one pure function (`require_model_id` / `requireModelId`) that the id is required for the gateway presets of `MODEL_ID_REQUIRED_PRESETS` (`bedrock`, `gemini`, derived from the presets that take `models`) and for the runtimes `opencode`, `hermes`, and `deepagents` or `strands` without `entrypoint`, raising `AgentProjectException` / `AgentProjectError` with `missing_key` on `model.id`. `to_dict` / `toObject` SHALL emit `id` only when set and the `*_env` keys only when set, so every existing project serializes unchanged.

#### Scenario: graph that chooses its model
- **WHEN** a project file has `[runtime] kind = "langgraph"` with an `entrypoint`, a gateway with preset `openai-compatible` and `[model]` with `provider`, `gateway` and `base_url_env = "LLM_BASE_URL"` but no `id`
- **THEN** both SDKs load it and `to_dict()` / `toObject()` contains no `model.id`

#### Scenario: a runtime that builds the model
- **WHEN** a project file has `[runtime] kind = "opencode"` and `[model]` without `id`
- **THEN** loading raises `missing_key` on `model.id`, as in `testdata/agent-model-optional/requirement.json`

#### Scenario: existing projects are byte-identical
- **WHEN** both SDKs load every file of `testdata/agent-project/valid/` that existed before this change
- **THEN** each canonical form equals its unchanged `.expected.json`

### Requirement: A LangGraph project can name its graph
`[runtime] graph` SHALL be accepted only with `kind = "langgraph"`, matching `^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$`, and SHALL be resolved against the `graphs` of `langgraph.json` in the project directory into `entrypoint` before the runtime is built, so the driver configuration does not change. `graph` together with `entrypoint` SHALL raise `conflict` on `runtime.graph`; a name absent from `graphs` SHALL raise `invalid_value` on `runtime.graph` with a message listing the available names; `graph` with another kind SHALL raise `invalid_value` on `runtime.graph`. `AgentProject.graph` (TypeScript `graph`) SHALL default to `None` / `undefined` and round-trip through `to_dict`.

#### Scenario: graph chosen in the file
- **WHEN** a project directory has `langgraph.json` with graphs `triage` and `echo` and `rayito-agent.toml` with `[runtime] kind = "langgraph"` and `graph = "triage"`
- **THEN** the loaded runtime's entrypoint is the one of `triage` and `to_dict()` keeps `graph = "triage"` without `entrypoint`

#### Scenario: graph and entrypoint
- **WHEN** a file sets both `[runtime] graph` and `[runtime] entrypoint`
- **THEN** loading raises `conflict` on `runtime.graph`

### Requirement: The project file maps Secrets Manager values to environment variables
The project file SHALL accept `[secret_env]`, whose keys are variable names and whose values are a secret name or ARN or a table `{ secret, key, version_stage }`, and `[[secret_env_from]]`, whose entries are `{ secret, prefix, keys }`; `AgentProject` SHALL expose them as `secret_env` (values `str | SecretEnvRef`) and `secret_env_from` (`SecretEnvSource` items), and both SDKs SHALL export `SecretEnvRef` and `SecretEnvSource`. Secret references SHALL pass the existing secret-name rules (no spaces, no value prefixes, `secret_value` otherwise); names SHALL NOT be reserved per `testdata/agent-secret-env/reserved.json` (`invalid_value`); a name in both `[env]` and `[secret_env]` SHALL raise `conflict`; unknown keys SHALL raise `unknown_key`. `[env]` SHALL keep its current rules and messages. `to_dict` SHALL emit the two tables only when not empty. Parsing SHALL follow `testdata/agent-secret-env/parse.json` in both SDKs.

#### Scenario: both tables
- **WHEN** both SDKs load the `tableCases` of `testdata/agent-secret-env/parse.json`
- **THEN** each result or error code and path equals `expected`

#### Scenario: no secret env
- **WHEN** a project has neither table
- **THEN** its canonical form has no `secret_env` nor `secret_env_from` key

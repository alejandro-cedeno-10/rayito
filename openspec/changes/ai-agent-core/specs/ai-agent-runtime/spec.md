## ADDED Requirements

### Requirement: Agent specs are validated before any call
Both SDKs SHALL provide `AgentModel`, `AgentPermissions`, `SubAgent`, `McpLocal`, `McpRemote`, `AgentSpec` and `AgentLimits` as immutable values validated at construction, raising `InvalidArgumentException` / `InvalidArgumentError` without any network call. `AgentModel.provider` SHALL be one of `bedrock`, `anthropic` or `openai-compatible`; `region` SHALL be required for `bedrock`; `base_path` SHALL be `""` or an absolute path without a trailing `/` and only accepted for `openai-compatible`. `gateway` (and `McpRemote.gateway`) SHALL follow the gateway route-name rule. No type SHALL carry a credential or header field. `AgentSpec.raw_config` SHALL reject, naming the key, any top-level key in `RESERVED_CONFIG_KEYS` (`provider`, `autoupdate`, `share`, `enabled_providers`, `model`, `small_model`, `mcp`, `agent`, `permission`, `instructions`). `AgentSpec.require_gateways(available)` SHALL raise naming the first missing gateway among the model's and every `McpRemote`'s. Both SDKs SHALL pass the shared vectors in `testdata/agent/domain-vectors.json`.

#### Scenario: Bedrock without a region
- **WHEN** a caller builds `AgentModel(provider="bedrock", id="m", gateway="bedrock")`
- **THEN** it raises `InvalidArgumentException` and no AWS client is created

#### Scenario: a reserved raw_config key
- **WHEN** a caller builds `AgentSpec(model=..., raw_config={"provider": {...}})`
- **THEN** it raises `InvalidArgumentException` whose message names `'provider'`

#### Scenario: a missing gateway
- **WHEN** a spec's model uses gateway `bedrock`, one `McpRemote` uses `docs-mcp`, and the sandbox only has `bedrock`
- **THEN** `require_gateways` raises `InvalidArgumentException` naming `'docs-mcp'`

### Requirement: Agent permissions are headless and deny the hanging tools
`AgentPermissions` SHALL accept only the actions `allow` and `deny`, for `default`, for a tool and for each argument pattern of a tool; `ask` SHALL raise `InvalidArgumentException` explaining that the run has nobody to answer. `effective_tools()` SHALL return `DEFAULT_DENIED_TOOLS` (`question`, `webfetch`, `websearch`) set to `deny`, overridden by the caller's entries.

#### Scenario: ask is rejected
- **WHEN** a caller builds `AgentPermissions(tools={"edit": "ask"})`
- **THEN** it raises `InvalidArgumentException`

#### Scenario: a caller re-enables a default-denied tool
- **WHEN** `AgentPermissions(tools={"webfetch": "allow"}).effective_tools()` is read
- **THEN** `webfetch` is `allow` and `question` and `websearch` are `deny`

### Requirement: Run limits have cost-bounding defaults shared through limits.json
`AgentLimits` SHALL default to `DEFAULT_AGENT_MAX_STEPS` (50), `DEFAULT_AGENT_TIMEOUT_SECONDS` (600; TypeScript `timeoutMs` 600000), `DEFAULT_AGENT_MAX_OUTPUT_BYTES` (16 MiB, never above `COMMAND_OUTPUT_MAX_BYTES`) and `DEFAULT_AGENT_MAX_TOTAL_TOKENS` (1000000; `None`/`null` disables it), all rendered from `limits.json` by `scripts/gen_limits.py`. Non-positive values SHALL raise `InvalidArgumentException`.

#### Scenario: defaults come from limits.json
- **WHEN** `AgentLimits()` is built in either SDK
- **THEN** its values equal the generated constants and the shared vectors

### Requirement: Agent events and failures form a closed, content-free vocabulary
Events SHALL be a union discriminated by `type`, one of `text_delta`, `text`, `reasoning`, `tool_call`, `step_started`, `step_finished`, `agent_failed` and `done`. `TokenUsage` SHALL carry `input` (excluding cached tokens), `output`, `reasoning`, `cache_read` and `cache_write`, with `total` summing the five. A failure `reason` SHALL be one of `model_error`, `runtime_error`, `runtime_missing`, `runtime_version_mismatch`, `protocol_error`, `timeout`, `max_steps`, `token_budget`, `output_limit`, `aborted` and `busy`; its message SHALL come from a fixed Spanish table keyed by reason, the same in both SDKs, and the only variable part SHALL be a `detail_code` that matches `[A-Za-z0-9_.-]{1,64}`; any other `detail_code` SHALL be dropped. `AgentException(SandboxException)` / `AgentError extends SandboxError` SHALL carry `reason`, the session id, `usage`, the exit code and `detail_code`. `ToolCall.output` SHALL be cut to `MAX_TOOL_OUTPUT_PREVIEW_BYTES` UTF-8 bytes on a character boundary, flagged by `output_truncated`.

#### Scenario: provider text never reaches a message
- **WHEN** a `model_error` carries `detail_code="Invalid key sk-123 for user"`
- **THEN** the message is exactly `el modelo devolvió un error` and `detail_code` is `None`

#### Scenario: a failure becomes an exception
- **WHEN** `AgentFailed("token_budget", detail_code="x", session_id="ses_1").to_exception(usage)` is built
- **THEN** it is an `AgentException` and a `SandboxException` with `reason="token_budget"`, `session_id="ses_1"`, that `usage`, and message `el agente superó su presupuesto de tokens (x)`

### Requirement: Gateway presets allow only the chosen model paths
`bedrock_gateway(secret, region=, models=)` SHALL return a `SecretGateway` with upstream `https://bedrock-runtime.<region>.amazonaws.com`, the single header `authorization`, and for each distinct model exactly `POST /model/<id>/converse-stream` and `POST /model/<id>/converse` with the id percent-encoded (`:` as `%3A`); every rule SHALL pass `is_safe_request_path`, and a model id containing `/` SHALL raise `InvalidArgumentException`. `anthropic_gateway(secret)` SHALL allow only `POST /v1/messages` on `https://api.anthropic.com` with `x-api-key`. `openai_compatible_gateway(secret, upstream=, base_path=)` SHALL allow only `POST <base_path>/chat/completions` with `authorization`. Building a preset SHALL make no AWS call.

#### Scenario: Bedrock rules match what the clients send
- **WHEN** `bedrock_gateway("k", region="us-east-1", models=["us.anthropic.claude-haiku-4-5-20251001-v1:0"])` is built
- **THEN** `allow` is `[("POST", "/model/us.anthropic.claude-haiku-4-5-20251001-v1%3A0/converse-stream"), ("POST", "/model/us.anthropic.claude-haiku-4-5-20251001-v1%3A0/converse")]`

#### Scenario: an ARN cannot be allowed
- **WHEN** a model id is an inference-profile ARN
- **THEN** `bedrock_gateway` raises `InvalidArgumentException`

## MODIFIED Requirements

### Requirement: Agent specs are validated before any call
Both SDKs SHALL provide `AgentModel`, `AgentPermissions`, `SubAgent`, `McpLocal`, `McpRemote`, `AgentSpec` and `AgentLimits` as immutable values validated at construction, raising `InvalidArgumentException` / `InvalidArgumentError` without any network call. `AgentModel.provider` SHALL be one of `bedrock`, `anthropic`, `openai-compatible`, `openai`, `google` or `azure`, and SHALL never name a consumer subscription; `region` SHALL be required for `bedrock`; `base_path` SHALL be `""` or an absolute path without a trailing `/` and only accepted for `openai-compatible`. `gateway` (and `McpRemote.gateway`) SHALL follow the gateway route-name rule. No type SHALL carry a credential or header field. `AgentSpec.raw_config` SHALL reject, naming the key, any top-level key in `RESERVED_CONFIG_KEYS` (`provider`, `autoupdate`, `share`, `enabled_providers`, `model`, `small_model`, `mcp`, `agent`, `permission`, `instructions`, `plugin`). `AgentSpec.require_gateways(available)` SHALL raise naming the first missing gateway among the model's and every `McpRemote`'s. Both SDKs SHALL pass the shared vectors in `testdata/agent/domain-vectors.json` and `testdata/agent/provider-catalogue.json`.

#### Scenario: Bedrock without a region
- **WHEN** a caller builds `AgentModel(provider="bedrock", id="m", gateway="bedrock")`
- **THEN** it raises `InvalidArgumentException` and no AWS client is created

#### Scenario: a reserved raw_config key
- **WHEN** a caller builds `AgentSpec(model=..., raw_config={"provider": {...}})`
- **THEN** it raises `InvalidArgumentException` whose message names `'provider'`

#### Scenario: a missing gateway
- **WHEN** a spec's model uses gateway `bedrock`, one `McpRemote` uses `docs-mcp`, and the sandbox only has `bedrock`
- **THEN** `require_gateways` raises `InvalidArgumentException` naming `'docs-mcp'`

#### Scenario: a refused provider
- **WHEN** a caller builds `AgentModel(provider="chatgpt-plan", id="m", gateway="modelo")`
- **THEN** it raises `InvalidArgumentException`

### Requirement: Gateway presets allow only the chosen model paths
`bedrock_gateway(secret, region=, models=)` SHALL return a `SecretGateway` with upstream `https://bedrock-runtime.<region>.amazonaws.com`, the single header `authorization`, and for each distinct model exactly `POST /model/<id>/converse-stream` and `POST /model/<id>/converse` with the id percent-encoded (`:` as `%3A`); every rule SHALL pass `is_safe_request_path`, and a model id containing `/` SHALL raise `InvalidArgumentException`. `anthropic_gateway(secret)` SHALL allow only `POST /v1/messages` on `https://api.anthropic.com` with `x-api-key`. `openai_compatible_gateway(secret, upstream=, base_path=)` SHALL allow only `POST <base_path>/chat/completions` with `authorization`. Both SDKs SHALL also provide, with exactly the upstream, headers and rules of `testdata/agent/provider-catalogue.json`: `openai_gateway(secret)` (`https://api.openai.com`, `authorization`, `POST /v1/responses` and `POST /v1/chat/completions`); `gemini_gateway(secret, models=)` (`https://generativelanguage.googleapis.com`, `x-goog-api-key`, for each distinct model `POST /v1beta/models/<id>:streamGenerateContent` and `POST /v1beta/models/<id>:generateContent`, rejecting an empty list and any id containing `/`); `azure_openai_gateway(secret, resource=)` (`https://<resource>.openai.azure.com`, `api-key`, `POST /openai/v1/responses` and `POST /openai/v1/chat/completions`, rejecting a `resource` that is not a 1-63 character DNS label of `[a-z0-9-]` without a leading or trailing hyphen); `openrouter_gateway(secret)` (`https://openrouter.ai`, `POST /api/v1/chat/completions`), `groq_gateway(secret)` (`https://api.groq.com`, `POST /openai/v1/chat/completions`), `mistral_gateway(secret)` (`https://api.mistral.ai`, `POST /v1/chat/completions`), `deepseek_gateway(secret)` (`https://api.deepseek.com`, `POST /chat/completions`) and `xai_gateway(secret)` (`https://api.x.ai`, `POST /v1/responses` and `POST /v1/chat/completions`), all with `authorization`; and `litellm_gateway(secret, upstream=, base_path="/v1")` (`authorization`, `POST <base_path>/chat/completions` and `POST <base_path>/responses`, rejecting an upstream that is not `https://host` and a `base_path` that `AgentModel.base_path` would reject). Every preset SHALL accept `rate_per_minute`. The OpenAI-style presets cannot restrict the model, which travels in the body; their docstrings SHALL say so and point to provider-side limits. Building a preset SHALL make no AWS call and load no optional peer.

#### Scenario: Bedrock rules match what the clients send
- **WHEN** `bedrock_gateway("k", region="us-east-1", models=["us.anthropic.claude-haiku-4-5-20251001-v1:0"])` is built
- **THEN** `allow` is `[("POST", "/model/us.anthropic.claude-haiku-4-5-20251001-v1%3A0/converse-stream"), ("POST", "/model/us.anthropic.claude-haiku-4-5-20251001-v1%3A0/converse")]`

#### Scenario: an ARN cannot be allowed
- **WHEN** a model id is an inference-profile ARN
- **THEN** `bedrock_gateway` raises `InvalidArgumentException`

#### Scenario: Gemini rules carry the model
- **WHEN** `gemini_gateway("k", models=["gemini-2.5-flash"])` is built
- **THEN** `allow` is `[("POST", "/v1beta/models/gemini-2.5-flash:streamGenerateContent"), ("POST", "/v1beta/models/gemini-2.5-flash:generateContent")]`

#### Scenario: an invalid Azure resource
- **WHEN** `azure_openai_gateway("k", resource="a.b")` is built
- **THEN** it raises `InvalidArgumentException`

#### Scenario: LiteLLM over plain http
- **WHEN** `litellm_gateway("k", upstream="http://litellm.example.com")` is built
- **THEN** it raises `InvalidArgumentException`

### Requirement: The runner builds the model through the gateway and enforces permissions
The runner SHALL build `ChatBedrockConverse(endpoint_url=<gateway>)`, `ChatAnthropic(base_url=<gateway>, api_key=<placeholder>)`, `ChatOpenAI(base_url=<gateway + base_path>, api_key=<placeholder>)` for `openai-compatible`, `ChatOpenAI(base_url=<gateway>/v1, api_key=<placeholder>, use_responses_api=True)` for `openai`, `ChatGoogleGenerativeAI(base_url=<gateway>, google_api_key=<placeholder>)` for `google` or `AzureChatOpenAI(base_url=<gateway>/openai/v1, api_key=<placeholder>, api_version="v1", use_responses_api=True)` for `azure` (the key travels in `api-key`, never in `Authorization`), exactly as `testdata/agent/deepagents-models.json` lists, and never read a credential. Without an entry point it SHALL build `create_deep_agent(model, system_prompt=instructions, subagents, backend=LocalShellBackend(root_dir=workdir, virtual_mode=False), middleware=ctx.middleware)`; with one it SHALL import `module` from the workdir and call `function(RunnerContext(model, instructions, subagents, backend, middleware, workdir))`. `virtual_mode=False` makes the file tools take absolute paths as they are, the same paths the shell tool and OpenCode see (with the default virtual mode `/home/user/x` would land in `<workdir>/home/user/x`). `ctx.middleware` SHALL contain a `wrap_tool_call` middleware that answers a denied call with an error `ToolMessage`, mapping `read→read_file`, `edit→write_file,edit_file,delete`, `list→ls`, `glob`, `grep`, `bash→execute`, `task`, `todowrite→write_todos`; `bash` patterns match the command with `fnmatchcase`, the last match winning. deepagents' prompt-caching middlewares SHALL stay on by default and SHALL be removed when `prompt_caching` is false. Every subagent in `subagents` SHALL carry that middleware, built from its own permissions or, when it has none, from the main agent's; `subagents` SHALL also include an explicit `general-purpose` subagent (deepagents' `GENERAL_PURPOSE_SUBAGENT`) with the main agent's rules, so that `task` cannot bypass them. Token usage of subagent model calls SHALL be added to the next main-agent `step_finished`.

#### Scenario: a denied command
- **WHEN** the permissions deny `bash` except `git *` and the model calls `execute` with `rm -rf x`
- **THEN** the tool is not run and the run emits a `tool_call` with status `error`

#### Scenario: a subagent cannot bypass the permissions
- **WHEN** the permissions deny `bash` and the model calls `task` with `subagent_type` `general-purpose`, whose model then calls `execute`
- **THEN** the command is not run

#### Scenario: deepagents with Azure
- **WHEN** `sbx.agent.run()` runs with `runtime="deepagents"` and `AgentModel(provider="azure", ...)`
- **THEN** the runner builds `AzureChatOpenAI` against `<gateway>/openai/v1` with the Responses API, and its requests reach `POST /openai/v1/responses` with only the `api-key` placeholder header

## ADDED Requirements

### Requirement: OpenCode reaches the native providers only through the gateway
For `openai`, `google` and `azure`, the OpenCode adapter SHALL write the provider ids `openai`, `google` and `azure` with `options.baseURL` set to the gateway URL plus `/v1`, `/v1beta` and `/openai/v1` respectively, `npm` set to `@ai-sdk/openai`, `@ai-sdk/google` and `@ai-sdk/azure` (bundled in the pinned binary), `options.apiKey` set to the placeholder, and a `models` map with the main, small and subagent model ids; `enabled_providers` SHALL contain only that id. The adapter SHALL never write an `auth.json`, nor an `auth` or `plugin` key, so a consumer-subscription login cannot be used. `gen_ai.provider.name` SHALL be `openai`, `gcp.gemini` and `azure.ai.openai` for those providers. Every run SHALL set `OPENCODE_EXPERIMENTAL_WEBSOCKETS=0`, so the `openai` provider never opens a WebSocket the gateway does not forward. The golden files `testdata/agent/opencode-config/<provider>.json` SHALL match byte for byte in both SDKs.

#### Scenario: OpenAI through the gateway
- **WHEN** the spec's model is `AgentModel(provider="openai", id="m", gateway="modelo")` and the gateway is at `http://127.0.0.1:18005`
- **THEN** `opencode.json` has `provider.openai.options.baseURL` = `http://127.0.0.1:18005/v1` and `apiKey` = `placeholder-not-a-secret`

#### Scenario: no subscription credential
- **WHEN** the adapter builds the files for any provider
- **THEN** the only file is `opencode.json` (plus `AGENTS.md` with instructions) and it has no `auth` or `plugin` key

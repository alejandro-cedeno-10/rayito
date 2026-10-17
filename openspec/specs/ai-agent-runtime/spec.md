# ai-agent-runtime Specification

## Purpose
`sbx.agent`: running a coding agent (OpenCode or deepagents) inside the sandbox through a closed event contract, SDK-side limits and a model credential that only the secrets gateway holds.

## Requirements

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

### Requirement: The agent run span never carries content, only with tracer_provider
With `tracer_provider=`/`tracerProvider`, `sbx.agent.run()`/`stream()` SHALL open one span `rayito.agent.run` with `gen_ai.operation.name`, `gen_ai.provider.name`, `gen_ai.request.model`, `gen_ai.agent.name` at start and, when the run ends, `gen_ai.conversation.id`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens` and the `rayito.agent.*` attributes from `ALLOWED_SPAN_ATTRIBUTES`. Without the option, no span SHALL be created. No attribute SHALL ever carry the prompt, the response text or a tool argument.

#### Scenario: no span without tracer_provider
- **WHEN** `sbx.agent.run()` is called on a sandbox created without `tracer_provider=`
- **THEN** no span is created and no `opentelemetry` import happens

#### Scenario: span attributes are a subset of the allowed list
- **WHEN** `sbx.agent.run()` is called with `tracer_provider=` set
- **THEN** the `rayito.agent.run` span's attributes are all members of `ALLOWED_SPAN_ATTRIBUTES`/`ALLOWED_SPAN_ATTRIBUTES` and none is the prompt or the response text

### Requirement: A deepagents runtime runs a LangGraph agent through the same contract
Both SDKs SHALL provide `DeepAgents(entrypoint=None)` (TypeScript `new DeepAgents({ entrypoint })`), an `AgentRuntime` registered as `"deepagents"`. `entrypoint`, when given, SHALL match `module.path:function`, otherwise construction raises `InvalidArgumentException` / `InvalidArgumentError`. `build_config` SHALL write one file, `AGENT_STATE_DIR/deepagents/config.json` with mode 0600, holding the provider, model id, region, the gateway URL (plus `base_path`), `MODEL_CREDENTIAL_PLACEHOLDER`, `prompt_caching`, instructions, workdir, sessions directory, entry point, permissions and subagents, serialised canonically so that Python and TypeScript produce the same bytes and sha256 (`testdata/agent/deepagents-config.json`). A spec with `mcp`, a non-empty `raw_config`, a tool name deepagents does not have, or argument patterns on any tool other than `bash` SHALL raise before any RPC.

#### Scenario: the same config in both SDKs
- **WHEN** the three golden specs are built with the same gateway URLs in Python and TypeScript
- **THEN** both config files and their sha256 equal `testdata/agent/deepagents-config.json`

#### Scenario: MCP is not supported
- **WHEN** a caller runs `runtime="deepagents"` with a spec that has an `McpLocal`
- **THEN** it raises `InvalidArgumentException` and no file is written

### Requirement: The deepagents run command keeps the prompt out of argv
`command()` SHALL return a script that takes the same `flock -n` run lock as OpenCode (a held lock prints `rayito.busy`), prints `rayito.runtime_missing` when the venv Python or the runner is absent, and otherwise `exec`s `/opt/agents/deepagents/bin/python /opt/agents/rayito/deepagents_runner.py <config>`. The prompt, session id, model override and reasoning flag SHALL travel only on stdin as `{"v":1,"prompt","session_id","model","reasoning"}`. `envs` SHALL carry no secret (only `HOME`, Python flags, the Bedrock placeholder token and the region). `attach=True`, a session id that is not `rda_` plus 32 hex characters, and an invalid model id SHALL raise before any RPC.

#### Scenario: resuming a session
- **WHEN** `command()` is built with `session_id="rda_0000000000000000000000000000000a"` and a model override
- **THEN** the script equals the golden script, which contains neither the prompt nor the session id, and stdin equals the golden request

### Requirement: The runner speaks the Rayito JSONL protocol v1
The runner SHALL write one `{"v":1,"type":…}` line per event on a private duplicate of its original stdout, after pointing fd 1 and `sys.stdout` at stderr. Types SHALL be `session`, `text_delta`, `text`, `reasoning` (only when the request asks for it), `tool_call`, `step_started`, `step_finished`, `agent_failed` and `done`, with the field names of the Rayito events. `step_finished.usage.input` SHALL exclude cached tokens. Tool output SHALL be cut to `MAX_TOOL_OUTPUT_PREVIEW_BYTES`. Every run SHALL end with `done` or `agent_failed`, whose `reason` is `model_error` (exceptions from the provider client modules), `runtime_error` or `protocol_error` and whose `detail_code` is an exception class name or a fixed code, never provider text. The adapter SHALL ignore and count lines that are not JSON, have another `v` or an unknown type, SHALL turn any other runner `reason` into `protocol_error`, and SHALL return `Done` only on exit 0 after `done` and a valid `session`.

#### Scenario: a user print does not reach the protocol
- **WHEN** a user entry point prints to stdout while building its graph
- **THEN** stdout contains only protocol lines

#### Scenario: the shared vector
- **WHEN** both SDKs parse `testdata/agent/rayito-protocol-v1.jsonl`
- **THEN** they produce the events, ignored-line count and `Done` of `rayito-protocol-v1-expected.json`

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

### Requirement: deepagents sessions live in the sandbox
The runner SHALL generate session ids `rda_<uuid4 hex>`, emit them in a `session` line before any other event, and store the history as `messages_to_dict` JSON in `sessions/<id>.json`, written atomically with mode 0600. A session id that does not match the pattern, or a missing session file, SHALL end the run with `agent_failed` (`runtime_error`, `invalid_session_id` / `session_not_found`) without touching other paths.

#### Scenario: continuing a run
- **WHEN** a second run passes the `session_id` of the first
- **THEN** the graph receives the first run's messages followed by the new prompt

### Requirement: sbx.agent is lazy, enforces the SDK's own limits and aborts by stopping the process tree
`Sandbox.agent`/`AsyncSandbox.agent`/`Sandbox.agent` (TypeScript) SHALL be constructed with the sandbox and SHALL make no RPC until `run()`, `stream()` or `prepare()` is called. `run()` and `stream()` SHALL NOT accept an `attach` option. `run()` SHALL raise `AgentException`/`AgentError` when the run fails; `stream()` SHALL return an iterable (`AgentStream`) that never raises for an agent failure — its last event is `Done` or `AgentFailed` — and SHALL raise only for a sandbox or transport error. The SDK SHALL enforce `AgentLimits.max_steps`/`maxSteps` when a `StepStarted` index exceeds it and `AgentLimits.max_total_tokens`/`maxTotalTokens` after any `StepFinished` whose accumulated usage exceeds it, independent of what the runtime itself enforces. The runtime's configuration SHALL be written with `files.write_files`/`files.writeFiles` only when its sha256 differs from the last one applied to that `Agent`/`AsyncAgent` instance. Every run SHALL start its command with `kill_tree=True`/`killTree: true`, so `rayd` stops and kills every descendant of the runtime, including those that started their own session or were daemonised, on the run's timeout and on a `SIGKILL`. `AgentStream.abort()` SHALL kill the underlying command handle, with no other command run in the sandbox, and the stream's final event SHALL be `AgentFailed(reason="aborted")`. When the SDK ends a run for `max_steps` or `token_budget`, it SHALL kill the handle the same way and consume it until its end event before the stream ends, so the run lock is free for the next run. When the handle ends with the command's timeout end status (which `wait()` reports as `TimeoutException`/`TimeoutError`), the final event SHALL be `AgentFailed(reason="timeout")`.

#### Scenario: touching sbx.agent makes no call
- **WHEN** `sbx.agent` is read right after `Sandbox.create()`/`connect()`
- **THEN** no `commands.run`, `files.write_files` or boto3 call happens

#### Scenario: max_steps is enforced by the SDK, not only the runtime
- **WHEN** a runtime emits `StepStarted(index=2)` and `AgentLimits(max_steps=1)` was passed
- **THEN** the stream's next event is `AgentFailed(reason="max_steps")`, and `run()` raises `AgentException` with that reason

#### Scenario: the config is written once per sha
- **WHEN** two runs use the same `Agent`/`AsyncAgent` instance and the runtime's `build_config()` returns the same `config_sha256` both times
- **THEN** `files.write_files`/`files.writeFiles` is called only on the first run

#### Scenario: abort stops the process tree, then kills the handle
- **WHEN** `stream.abort()` is called
- **THEN** the run's command was started with `kill_tree`, the handle is killed, no other command is run in the sandbox, and `stream.result()` raises `AgentException`/`rejects` with `reason="aborted"`

#### Scenario: an SDK limit stops the runtime and its process tree
- **WHEN** the stream ends with `AgentFailed(reason="max_steps")` or `AgentFailed(reason="token_budget")`
- **THEN** the handle of the `kill_tree` run was killed and consumed to its end before the stream ended, and no other command was run

#### Scenario: a timeout reported only by the end event is a timeout
- **WHEN** the command handle ends without raising while iterating and its `wait()` raises `TimeoutException`/`TimeoutError`
- **THEN** the stream's final event is `AgentFailed(reason="timeout")`

#### Scenario: a timeout leaves nothing the runtime daemonised
- **WHEN** a run whose runtime daemonises a process (`setsid` and a parent that exits) reaches `AgentLimits.timeout_seconds`/`timeoutMs`
- **THEN** the stream's final event is `AgentFailed(reason="timeout")` and, when it arrives, the daemonised process is no longer alive

### Requirement: The OpenCode adapter execs opencode run directly, headless and byte-identical across SDKs

The `opencode` runtime SHALL write an `opencode.json` that only carries the
model credential placeholder, SHALL pass the prompt on stdin and never in
argv, SHALL always pass `--title` and SHALL hold a per-sandbox run lock.
After taking the lock and checking that `opencode` is on the `PATH`, the run
script SHALL `exec` `opencode run --format json` directly: it SHALL NOT
probe, start or attach to an `opencode serve`, and SHALL NOT print any
`rayito.` line other than `rayito.busy` and `rayito.runtime_missing`. A
`session_id` that is not an OpenCode id SHALL raise
`InvalidArgumentException` / `InvalidArgumentError`. Python and TypeScript
SHALL produce the same configuration bytes, sha256 and run script for the
same spec (`testdata/agent/`).

#### Scenario: Error events decide failure, not the exit code

- **WHEN** OpenCode emits an `error` event and then exits with code 0
- **THEN** the run ends with `AgentFailed(reason="model_error")`, whose
  `detail_code` is the error name and never its message

#### Scenario: A second run while one holds the lock

- **WHEN** a run starts while another holds the run lock
- **THEN** it ends with `AgentFailed(reason="busy")` without starting OpenCode

#### Scenario: The run script execs OpenCode

- **WHEN** a unit test builds the run command for a spec in either SDK
- **THEN** the script equals `testdata/agent/opencode-run-commands.json`,
  its last line is `exec 'opencode' 'run' …`, and it contains neither
  `--attach` nor `curl`

### Requirement: OpenCode reaches the native providers only through the gateway
For `openai`, `google` and `azure`, the OpenCode adapter SHALL write the provider ids `openai`, `google` and `azure` with `options.baseURL` set to the gateway URL plus `/v1`, `/v1beta` and `/openai/v1` respectively, `npm` set to `@ai-sdk/openai`, `@ai-sdk/google` and `@ai-sdk/azure` (bundled in the pinned binary), `options.apiKey` set to the placeholder, and a `models` map with the main, small and subagent model ids; `enabled_providers` SHALL contain only that id. The adapter SHALL never write an `auth.json`, nor an `auth` or `plugin` key, so a consumer-subscription login cannot be used. `gen_ai.provider.name` SHALL be `openai`, `gcp.gemini` and `azure.ai.openai` for those providers. Every run SHALL set `OPENCODE_EXPERIMENTAL_WEBSOCKETS=0`, so the `openai` provider never opens a WebSocket the gateway does not forward. The golden files `testdata/agent/opencode-config/<provider>.json` SHALL match byte for byte in both SDKs.

#### Scenario: OpenAI through the gateway
- **WHEN** the spec's model is `AgentModel(provider="openai", id="m", gateway="modelo")` and the gateway is at `http://127.0.0.1:18005`
- **THEN** `opencode.json` has `provider.openai.options.baseURL` = `http://127.0.0.1:18005/v1` and `apiKey` = `placeholder-not-a-secret`

#### Scenario: no subscription credential
- **WHEN** the adapter builds the files for any provider
- **THEN** the only file is `opencode.json` (plus `AGENTS.md` with instructions) and it has no `auth` or `plugin` key

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

### Requirement: One Node bridge runs JavaScript agents through adapters
The host SHALL provide the driver `node-callable`, which runs `node node_bridge.mjs <config>` and speaks protocol v1, and SHALL keep `strands-ts` as an alias of it. The bridge SHALL select an adapter from `config.adapter`: `strands` (the default), `langgraph` (a compiled LangGraph.js or deepagents-js graph, run with `graph.stream({messages}, {configurable: {thread_id}, streamMode: "updates"})`), `vercel-ai` (an AI SDK agent, run with `stream()` or, without it, `generate()`) and `callable` (a function receiving `{prompt, session_id, model, reasoning}` and returning a string, an iterable of strings or `{text, usage}`). `entrypoint` SHALL be a path under the copied source, optionally followed by `#export`; a named export that does not exist SHALL fail with `entrypoint_not_found`, an unknown adapter with `unknown_adapter` and an export of the wrong shape with `invalid_entrypoint`. Messages SHALL map to events by shape: an AI message is a step with its text and usage, a tool message a `tool_call` with the arguments the AI message asked for. A framework without its own memory (the AI SDK, a graph without a checkpointer) SHALL keep the last `HISTORY_MAX_MESSAGES` turns of each session in a `0600` file under `sessions_dir`. Tool permissions SHALL be enforced only by the `strands` adapter. When `AWS_ENDPOINT_URL_BEDROCK_RUNTIME` is set the bridge SHALL replace `NodeHttp2Handler` by `NodeHttpHandler` in every loaded copy of `@smithy/node-http-handler`; otherwise it SHALL change nothing. The Python and TypeScript SDKs SHALL generate the same `CustomRuntime` and `StrandsRuntime` host configuration for the cases of `testdata/agent/framework-host-config.json`.

#### Scenario: a graph without a checkpointer keeps the conversation
- **WHEN** a `langgraph` agent runs twice with the same session id
- **THEN** the second call receives the first turn's user and assistant text before the new prompt

#### Scenario: real frameworks
- **WHEN** a graph built with LangChain `createAgent`, one built with `createDeepAgent` and a `ToolLoopAgent` run through the bridge with a scripted model
- **THEN** each produces `step_started`, `tool_call`, `step_finished` and `text` events with the model's usage

#### Scenario: HTTP/1.1 behind the gateway
- **WHEN** the bridge starts with `AWS_ENDPOINT_URL_BEDROCK_RUNTIME` set and user code loads the AWS SDK
- **THEN** the SDK client reaches an HTTP/1.1-only endpoint

### Requirement: The install plan brings the interpreter, the package manager and the dependencies of a project
`CustomRuntime` SHALL accept `package_manager` (`pip`, `uv`, `npm`, `pnpm`, `yarn`), `python_version` (`3.11` to `3.14`), `node_version` (`22`, `24`) and `adapter` (`callable`, `langgraph`, `vercel-ai`, only with an `entrypoint` and a Node package manager); `StrandsRuntime(language="typescript")` SHALL accept `package_manager` and `node_version`; the `langgraph` runtime SHALL accept `requirements` and `package_manager`. A version other than the image's CPython SHALL be installed from the pinned python-build-standalone build and Node from the pinned nodejs.org build, both checked against the sha256 in `limits.json`; `uv`, `pnpm` and `yarn` SHALL be installed at the pinned version. Node dependencies SHALL be installed with the manager's frozen lockfile command and `--ignore-scripts`. `requirements` SHALL be `requirements.txt`-style, or with `uv` `pyproject.toml` or `uv.lock` at the source root; `pip` SHALL NOT accept the last two. With a venv that already has Rayito's pins, dependencies SHALL be added without removing anything. Combinations that contradict each other SHALL raise `InvalidArgumentException` before any call. Both SDKs SHALL produce the same steps for the cases of `testdata/agent/install-plans/` and `testdata/agent/framework-install-steps.json`, and Hermes SHALL use the same CPython download.

#### Scenario: uv with a lockfile and a pinned Python
- **WHEN** a project uses `package_manager="uv"`, `requirements="uv.lock"` and `python_version="3.13"`
- **THEN** the image downloads the pinned CPython 3.13, installs the pinned `uv` and runs `uv sync --frozen --no-dev --no-install-project`

#### Scenario: a contradiction
- **WHEN** `python_version` is combined with a Node package manager, or `node_version` with a Python one
- **THEN** `InvalidArgumentException` is raised before any RPC

### Requirement: CustomRuntime runs the user's app and command from the copied source with its venv first
`CustomRuntime.host_setup` SHALL add `app_dir` (`AGENT_APP_DIR`) to the options of the `http-app` and `command` drivers and, when `requirements` is set, `path_prepend` with the `bin` directory of the runtime's venv. The `http-app` driver SHALL start the app with its working directory set to `app_dir` (or `workdir` when `app_dir` is absent) and the `command` driver SHALL keep `workdir`. Both drivers SHALL start the child with `path_prepend` first in `PATH` and `app_dir` first in `PYTHONPATH`. When `argv[0]` is `python` and no `python` is on the child `PATH`, the driver SHALL run the interpreter that runs the host instead. Both SDKs SHALL produce the same options for the cases of `testdata/agent/framework-host-config.json`.

#### Scenario: an app started as a module from the source directory
- **WHEN** an `HttpApp(command=["python", "-m", "agent.server"])` is started with `app_dir` set to a directory holding the package `agent`
- **THEN** the app runs with that directory as its working directory and first in `PYTHONPATH`, and the import of `agent` succeeds

#### Scenario: the venv python wins
- **WHEN** `requirements` is set and the command starts with `python`
- **THEN** the first `PATH` entry is `<install_dir>/custom/bin` and `python` resolves to the venv interpreter

#### Scenario: no python on the path
- **WHEN** the command starts with `python`, no `requirements` is set and the image has only `python3`
- **THEN** the driver runs the host interpreter and the app starts

### Requirement: The langgraph runtime translates a plain LangGraph graph
The deepagents driver SHALL treat `model` and `agent` as the model nodes and `tools` as the tools node, so a graph built with `langgraph.prebuilt.create_react_agent` (or a `StateGraph` with nodes of those names) produces text, steps, tool calls and usage. A node with any other name SHALL NOT be read as the model. `rayito agent init --runtime langgraph` SHALL write a plain `create_react_agent` graph and the guide SHALL show it.

#### Scenario: create_react_agent graph
- **WHEN** a graph whose model node is named `agent` streams an `AIMessage` with a tool call, a `ToolMessage` and a final `AIMessage`
- **THEN** the driver emits two steps, one `tool_call` event with the tool output and the usage of each step

#### Scenario: unrelated node
- **WHEN** a graph streams messages from a node named `planner`
- **THEN** the driver emits no text, step or tool event for them

### Requirement: A generic Node driver hosts JavaScript agent frameworks
The host SHALL provide a `node-callable` driver that loads a JavaScript module export and runs it through one adapter per framework: LangGraph.js and deepagents-js (`invoke` and `stream` with `thread_id`), the Vercel AI SDK (`generate` and `stream`) and Strands TypeScript. `strands-ts` SHALL remain an accepted name for the Strands TypeScript driver. A driver SHALL report usage when the framework returns it and unknown usage otherwise.

#### Scenario: LangGraph.js
- **WHEN** a compiled LangGraph.js graph is the entrypoint
- **THEN** an invocation returns the final text and the session id is the thread id

#### Scenario: alias
- **WHEN** a project declares the `strands-ts` runtime
- **THEN** it runs on the Strands TypeScript adapter as before

### Requirement: JavaScript Bedrock providers reach the gateway with the standard variables
For an agent whose provider package is `@ai-sdk/amazon-bedrock`, `@langchain/aws`, `@strands-agents/sdk` or `@aws-sdk/client-bedrock-runtime`, the host SHALL set `AWS_BEARER_TOKEN_BEDROCK` to the placeholder and `AWS_ENDPOINT_URL_BEDROCK_RUNTIME` to the gateway URL, and SHALL never set the real key. A package that cannot be wired this way SHALL be reported by detection as `unsupported`.

#### Scenario: gateway variables
- **WHEN** a sample using `@ai-sdk/amazon-bedrock` is served through the local gateway
- **THEN** its request reaches the fake Bedrock route with the placeholder as bearer token

### Requirement: Samples of existing agents run without rayito code
The repository SHALL hold fifteen sample agents under `testdata/agent-samples/` that import nothing from rayito and have their own lockfile: Python LangGraph with FastAPI, LangChain `create_agent`, deepagents, Strands, Strands harness, Hermes, Flask custom HTTP and AgentCore-native; TypeScript LangGraph.js, deepagents-js, Strands TypeScript, Strands harness TypeScript, Vercel AI SDK and Express or Hono custom HTTP; and OpenCode. A test SHALL fail when a sample imports rayito, lacks a lockfile or holds a secret-like value.

#### Scenario: no rayito import
- **WHEN** the sample check runs
- **THEN** no sample file mentions `rayito` and every sample has a lockfile

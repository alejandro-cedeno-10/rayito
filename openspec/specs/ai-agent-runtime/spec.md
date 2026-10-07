# ai-agent-runtime Specification

## Purpose
`sbx.agent`: running a coding agent (OpenCode or deepagents) inside the sandbox through a closed event contract, SDK-side limits and a model credential that only the secrets gateway holds.

## Requirements

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

### Requirement: The AgentRuntime port resolves by name or by object
Both SDKs SHALL define a pure `AgentRuntime` port (`build_config`/`buildConfig`, `command`, `new_state`/`newState`, `parse_line`/`parseLine`, `finish`, `template_steps`/`templateSteps`, `warmup_steps`/`warmupSteps`) and a `nombre -> AgentRuntime` registry. `warmup_steps()`/`warmupSteps()` SHALL take no argument. `runtime=`/`runtime` SHALL accept either a registered name or any object that implements the port. An unregistered name SHALL raise `UnimplementedError`/`UnimplementedError` naming the runtime, never a raw `KeyError`/`undefined` access; any other value SHALL raise `InvalidArgumentException`/`InvalidArgumentError`.

#### Scenario: an unregistered runtime name
- **WHEN** `sbx.agent.run(prompt, spec=spec, runtime="opencode")` is called before any adapter registers `"opencode"`
- **THEN** it raises `UnimplementedError` naming `runtime="opencode"`, before any RPC

#### Scenario: a caller's own runtime object
- **WHEN** `runtime=` is an object that implements every method of `AgentRuntime`
- **THEN** `sbx.agent.run()` uses it directly, with no registry lookup

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
The runner SHALL build `ChatBedrockConverse(endpoint_url=<gateway>)`, `ChatAnthropic(base_url=<gateway>, api_key=<placeholder>)` or `ChatOpenAI(base_url=<gateway + base_path>, api_key=<placeholder>)`, and never read a credential. Without an entry point it SHALL build `create_deep_agent(model, system_prompt=instructions, subagents, backend=LocalShellBackend(root_dir=workdir, virtual_mode=False), middleware=ctx.middleware)`; with one it SHALL import `module` from the workdir and call `function(RunnerContext(model, instructions, subagents, backend, middleware, workdir))`. `virtual_mode=False` makes the file tools take absolute paths as they are, the same paths the shell tool and OpenCode see (with the default virtual mode `/home/user/x` would land in `<workdir>/home/user/x`). `ctx.middleware` SHALL contain a `wrap_tool_call` middleware that answers a denied call with an error `ToolMessage`, mapping `read→read_file`, `edit→write_file,edit_file,delete`, `list→ls`, `glob`, `grep`, `bash→execute`, `task`, `todowrite→write_todos`; `bash` patterns match the command with `fnmatchcase`, the last match winning. deepagents' prompt-caching middlewares SHALL stay on by default and SHALL be removed when `prompt_caching` is false. Every subagent in `subagents` SHALL carry that middleware, built from its own permissions or, when it has none, from the main agent's; `subagents` SHALL also include an explicit `general-purpose` subagent (deepagents' `GENERAL_PURPOSE_SUBAGENT`) with the main agent's rules, so that `task` cannot bypass them. Token usage of subagent model calls SHALL be added to the next main-agent `step_finished`.

#### Scenario: a denied command
- **WHEN** the permissions deny `bash` except `git *` and the model calls `execute` with `rm -rf x`
- **THEN** the tool is not run and the run emits a `tool_call` with status `error`

#### Scenario: a subagent cannot bypass the permissions
- **WHEN** the permissions deny `bash` and the model calls `task` with `subagent_type` `general-purpose`, whose model then calls `execute`
- **THEN** the command is not run

### Requirement: deepagents sessions live in the sandbox
The runner SHALL generate session ids `rda_<uuid4 hex>`, emit them in a `session` line before any other event, and store the history as `messages_to_dict` JSON in `sessions/<id>.json`, written atomically with mode 0600. A session id that does not match the pattern, or a missing session file, SHALL end the run with `agent_failed` (`runtime_error`, `invalid_session_id` / `session_not_found`) without touching other paths.

#### Scenario: continuing a run
- **WHEN** a second run passes the `session_id` of the first
- **THEN** the graph receives the first run's messages followed by the new prompt

### Requirement: sbx.agent is lazy, enforces the SDK's own limits and aborts by stopping the process tree
`Sandbox.agent`/`AsyncSandbox.agent`/`Sandbox.agent` (TypeScript) SHALL be constructed with the sandbox and SHALL make no RPC until `run()`, `stream()` or `prepare()` is called. `run()` and `stream()` SHALL NOT accept an `attach` option. `run()` SHALL raise `AgentException`/`AgentError` when the run fails; `stream()` SHALL return an iterable (`AgentStream`) that never raises for an agent failure — its last event is `Done` or `AgentFailed` — and SHALL raise only for a sandbox or transport error. The SDK SHALL enforce `AgentLimits.max_steps`/`maxSteps` when a `StepStarted` index exceeds it and `AgentLimits.max_total_tokens`/`maxTotalTokens` after any `StepFinished` whose accumulated usage exceeds it, independent of what the runtime itself enforces. The runtime's configuration SHALL be written with `files.write_files`/`files.writeFiles` only when its sha256 differs from the last one applied to that `Agent`/`AsyncAgent` instance. `AgentStream.abort()` SHALL run `stop_tree_command(pid)`/`stopTreeCommand(pid)` (which stops the runtime process and stops and kills every descendant of it, including those that started their own session), before killing the underlying command handle, and the stream's final event SHALL be `AgentFailed(reason="aborted")`. When the SDK ends a run for `max_steps` or `token_budget`, it SHALL stop the runtime the same way and consume the handle until its end event before the stream ends, so the run lock is free for the next run. When the handle ends with the command's timeout end status (which `wait()` reports as `TimeoutException`/`TimeoutError`), the final event SHALL be `AgentFailed(reason="timeout")`.

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
- **THEN** `stop_tree_command(pid)` is the only command run in the sandbox before the handle is killed, and `stream.result()` raises `AgentException`/`rejects` with `reason="aborted"`

#### Scenario: an SDK limit stops the runtime and its process tree
- **WHEN** the stream ends with `AgentFailed(reason="max_steps")` or `AgentFailed(reason="token_budget")`
- **THEN** `stop_tree_command(pid)` ran and the handle was killed and consumed to its end before the stream ended

#### Scenario: a timeout reported only by the end event is a timeout
- **WHEN** the command handle ends without raising while iterating and its `wait()` raises `TimeoutException`/`TimeoutError`
- **THEN** the stream's final event is `AgentFailed(reason="timeout")`

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

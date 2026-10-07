# ai-agent-runtime Specification

## Purpose
TBD - created by archiving change ai-agent-core. Update Purpose after archive.

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
Both SDKs SHALL define a pure `AgentRuntime` port (`build_config`/`buildConfig`, `command`, `new_state`/`newState`, `parse_line`/`parseLine`, `finish`, `abort_command`/`abortCommand`, `template_steps`/`templateSteps`, `warmup_steps`/`warmupSteps`) and a `nombre -> AgentRuntime` registry. `runtime=`/`runtime` SHALL accept either a registered name or any object that implements the port. An unregistered name SHALL raise `UnimplementedError`/`UnimplementedError` naming the runtime, never a raw `KeyError`/`undefined` access; any other value SHALL raise `InvalidArgumentException`/`InvalidArgumentError`.

#### Scenario: an unregistered runtime name
- **WHEN** `sbx.agent.run(prompt, spec=spec, runtime="opencode")` is called before any adapter registers `"opencode"`
- **THEN** it raises `UnimplementedError` naming `runtime="opencode"`, before any RPC

#### Scenario: a caller's own runtime object
- **WHEN** `runtime=` is an object that implements every method of `AgentRuntime`
- **THEN** `sbx.agent.run()` uses it directly, with no registry lookup

### Requirement: sbx.agent is a lazy property that enforces the SDK's own limits
`Sandbox.agent`/`AsyncSandbox.agent`/`Sandbox.agent` (TypeScript) SHALL be constructed with the sandbox and SHALL make no RPC until `run()`, `stream()` or `prepare()` is called. `run()` SHALL raise `AgentException`/`AgentError` when the run fails; `stream()` SHALL return an iterable (`AgentStream`) that never raises for an agent failure — its last event is `Done` or `AgentFailed` — and SHALL raise only for a sandbox or transport error. The SDK SHALL enforce `AgentLimits.max_steps`/`maxSteps` when a `StepStarted` index exceeds it and `AgentLimits.max_total_tokens`/`maxTotalTokens` after any `StepFinished` whose accumulated usage exceeds it, independent of what the runtime itself enforces. The runtime's configuration SHALL be written with `files.write_files`/`files.writeFiles` only when its sha256 differs from the last one applied to that `Agent`/`AsyncAgent` instance. `AgentStream.abort()` SHALL call the runtime's `abort_command`/`abortCommand` (if any), then run `stop_tree_command(pid)`/`stopTreeCommand(pid)` (which stops the runtime process and stops and kills every descendant of it, including those that started their own session), before killing the underlying command handle, and the stream's final event SHALL be `AgentFailed(reason="aborted")`. When the SDK ends a run for `max_steps` or `token_budget`, it SHALL stop the runtime the same way and consume the handle until its end event before the stream ends, so the run lock is free for the next run. When the handle ends with the command's timeout end status (which `wait()` reports as `TimeoutException`/`TimeoutError`), the final event SHALL be `AgentFailed(reason="timeout")`.

#### Scenario: touching sbx.agent makes no call
- **WHEN** `sbx.agent` is read right after `Sandbox.create()`/`connect()`
- **THEN** no `commands.run`, `files.write_files` or boto3 call happens

#### Scenario: max_steps is enforced by the SDK, not only the runtime
- **WHEN** a runtime emits `StepStarted(index=2)` and `AgentLimits(max_steps=1)` was passed
- **THEN** the stream's next event is `AgentFailed(reason="max_steps")`, and `run()` raises `AgentException` with that reason

#### Scenario: the config is written once per sha
- **WHEN** two runs use the same `Agent`/`AsyncAgent` instance and the runtime's `build_config()` returns the same `config_sha256` both times
- **THEN** `files.write_files`/`files.writeFiles` is called only on the first run

#### Scenario: abort runs the runtime's abort command, then kills the handle
- **WHEN** `stream.abort()` is called and the runtime's `abort_command()` returns a shell command
- **THEN** that command and then `stop_tree_command(pid)` run in the sandbox before the handle is killed, and `stream.result()` raises `AgentException`/`rejects` with `reason="aborted"`

#### Scenario: an SDK limit stops the runtime and its process tree
- **WHEN** the stream ends with `AgentFailed(reason="max_steps")` or `AgentFailed(reason="token_budget")`
- **THEN** `stop_tree_command(pid)` ran and the handle was killed and consumed to its end before the stream ended

#### Scenario: a timeout reported only by the end event is a timeout
- **WHEN** the command handle ends without raising while iterating and its `wait()` raises `TimeoutException`/`TimeoutError`
- **THEN** the stream's final event is `AgentFailed(reason="timeout")`

### Requirement: The agent run span never carries content, only with tracer_provider
With `tracer_provider=`/`tracerProvider`, `sbx.agent.run()`/`stream()` SHALL open one span `rayito.agent.run` with `gen_ai.operation.name`, `gen_ai.provider.name`, `gen_ai.request.model`, `gen_ai.agent.name` at start and, when the run ends, `gen_ai.conversation.id`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens` and the `rayito.agent.*` attributes from `ALLOWED_SPAN_ATTRIBUTES`. Without the option, no span SHALL be created. No attribute SHALL ever carry the prompt, the response text or a tool argument.

#### Scenario: no span without tracer_provider
- **WHEN** `sbx.agent.run()` is called on a sandbox created without `tracer_provider=`
- **THEN** no span is created and no `opentelemetry` import happens

#### Scenario: span attributes are a subset of the allowed list
- **WHEN** `sbx.agent.run()` is called with `tracer_provider=` set
- **THEN** the `rayito.agent.run` span's attributes are all members of `ALLOWED_SPAN_ATTRIBUTES`/`ALLOWED_SPAN_ATTRIBUTES` and none is the prompt or the response text

### Requirement: The OpenCode adapter is headless, credential-free and byte-identical across SDKs

The `opencode` runtime SHALL write an `opencode.json` that only carries the
model credential placeholder, SHALL pass the prompt on stdin and never in
argv, SHALL always pass `--title`, SHALL hold a per-sandbox run lock and
SHALL attach to a resident `opencode serve` only when it answers its health
check. Python and TypeScript SHALL produce the same configuration bytes,
sha256 and run script for the same spec (`testdata/agent/`).

#### Scenario: Error events decide failure, not the exit code

- **WHEN** OpenCode emits an `error` event and then exits with code 0
- **THEN** the run ends with `AgentFailed(reason="model_error")`, whose
  `detail_code` is the error name and never its message

#### Scenario: A second run while one holds the lock

- **WHEN** a run starts while another holds the run lock
- **THEN** it ends with `AgentFailed(reason="busy")` without starting OpenCode

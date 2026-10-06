## ADDED Requirements

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
The runner SHALL build `ChatBedrockConverse(endpoint_url=<gateway>)`, `ChatAnthropic(base_url=<gateway>, api_key=<placeholder>)` or `ChatOpenAI(base_url=<gateway + base_path>, api_key=<placeholder>)`, and never read a credential. Without an entry point it SHALL build `create_deep_agent(model, system_prompt=instructions, subagents, backend=LocalShellBackend(root_dir=workdir), middleware=ctx.middleware)`; with one it SHALL import `module` from the workdir and call `function(RunnerContext(model, instructions, subagents, backend, middleware, workdir))`. `ctx.middleware` SHALL contain a `wrap_tool_call` middleware that answers a denied call with an error `ToolMessage`, mapping `read→read_file`, `edit→write_file,edit_file,delete`, `list→ls`, `glob`, `grep`, `bash→execute`, `task`, `todowrite→write_todos`; `bash` patterns match the command with `fnmatchcase`, the last match winning. deepagents' prompt-caching middlewares SHALL stay on by default and SHALL be removed when `prompt_caching` is false. Every subagent in `subagents` SHALL carry that middleware, built from its own permissions or, when it has none, from the main agent's; `subagents` SHALL also include an explicit `general-purpose` subagent (deepagents' `GENERAL_PURPOSE_SUBAGENT`) with the main agent's rules, so that `task` cannot bypass them. Token usage of subagent model calls SHALL be added to the next main-agent `step_finished`.

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

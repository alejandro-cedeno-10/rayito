## ADDED Requirements

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

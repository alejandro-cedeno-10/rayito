## ADDED Requirements

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

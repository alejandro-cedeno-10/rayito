## Why

A first outside user deployed an existing LangGraph agent (a ReAct graph behind
a FastAPI app) with the 0.14 agent runtimes and measured it. The deployment
worked, but nine specific things cost them time or hid information: the
documented `HttpApp(command=["python", "serve.py"])` fails because the image has
no `python` on the `PATH`; the app does not run from its source directory;
a startup failure only says an exception class and `deploy()` kills the VM, so
the cause is lost; `rayito agent init --runtime custom` and the project-file key
table do not mention `[runtime.app]`; the "LangGraph" tab shows deepagents code
and a plain `create_react_agent` graph produces no output; `deploy()` without an
`access_token` creates a token nobody can read and `AgentDeployment` cannot
pause or resume; `result.usage` is all zeros for an HTTP app; the default
`agentcore` protocol hides the app's own `/health` and `/invoke`; and the
one-time setup (base image, base image version, bucket) is spread over several
pages. Version 0.14.0 has not been released, so these are fixed before it ships,
with no breaking change to existing agent APIs.

## What Changes

- **Run environment of `CustomRuntime`**: the host starts an `HttpApp` and a
  `command` with the venv `bin` of `requirements` first on `PATH`, the copied
  source directory first on `PYTHONPATH` and, for an `HttpApp`, as its working
  directory. `python` without any on `PATH` is the interpreter that runs the host.
- **Startup diagnostics**: the failed marker carries the failure code
  (`app_exited`, `app_not_ready`, or an exception class) and, for an HTTP app,
  the end of its stderr, redacted of environment values and credentials and
  bounded by `AGENT_STARTUP_DIAGNOSTICS_MAX_BYTES`; `deploy()` puts it in the
  `SandboxException`. `deploy(keep_on_failure=True)` keeps the sandbox (off by
  default; needs an access token the caller knows).
- **Access token, pause and resume**: `AgentDeployment` (sync, async, TypeScript)
  exposes `access_token` and maps `pause()` and `resume()` to the sandbox ones.
- **Unknown usage**: `usage_reported` on `Done`, `AgentResult` and the service
  JSON; an HTTP app reports usage when its response carries it, otherwise it is
  unknown and not zero. The CLI prints `tokens=desconocido`.
- **LangGraph**: the `langgraph` runtime also translates the `agent` node of
  `create_react_agent`; docs and the `init` scaffold use a plain LangGraph graph.
- **Protocol guidance**: docs say when to use `raw`; `HttpApp.hides_own_routes`
  and a warning in `rayito agent deploy`; `init --runtime custom` writes an HTTP
  app with `[runtime.app]`, `requirements`, `install` and `protocol = "raw"`.
- **Project file docs**: the key table lists every key of every table.
- **Setup**: the one-time prerequisites in one place, and `rayito image publish
  --base-image-version latest` resolves the newest managed base version.

Nothing is deprecated or removed.

## Impact

- Capabilities: `ai-agent-runtime`, `ai-agent-service`, `ai-agent-project` and
  `cli` (added requirements).
- Code: `clients/python/src/rayito/_agent/` (host drivers, custom runtime,
  deployment, project service), `cli/agent.py`, `cli/image.py`, `cli/_publish.py`,
  `clients/typescript/src/agent/` and `src/cli/`, `limits.json` and the
  generated limits, `testdata/agent/`, `testdata/cli/`.
- No change to `rayd`, the `.proto`, the base image or `infra/`. An image built
  before this change needs a rebuild to get the new host.

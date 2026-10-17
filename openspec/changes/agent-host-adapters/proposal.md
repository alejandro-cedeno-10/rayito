## Why

The 0.14 host runs an agent you already have, but four things stop it from
running agents as they exist in the wild without changes: an HTTP app with its
own contract (`POST /chat`, `/health`) cannot be both invoked and reached; an
app that crashes after it was ready stays dead until the VM is replaced; the
host answers `429` the moment its slot is taken, so a burst of callers must be
retried by the caller; and JavaScript agents other than Strands (LangGraph.js,
deepagents-js, the Vercel AI SDK, a plain function) and Python or Node projects
with `uv`, `pnpm` or `yarn` have no way in. The JavaScript AWS SDK also speaks
HTTP/2 to Bedrock while the secret gateway of the sandbox speaks HTTP/1.1, so a
JavaScript agent using Bedrock through the gateway fails with a protocol error.
Version 0.14.0 has not been released and everything here is additive.

## What Changes

- **`json` protocol for `HttpApp`**: the host translates `/invocations` to the
  app's own contract (`request_field`, `response_field`) and forwards every other
  route. A bare `HttpApp` keeps `agentcore`.
- **Bounded FIFO queue** in the service (`queue_max`, `queue_timeout_seconds`
  in `host.json`; off by default, so `429` stays the 0.14 behaviour).
- **Supervision** of long-lived children (`http-app`, `hermes`): restart with
  exponential backoff inside a window; `/ping` is `HealthyBusy` while it
  restarts and `503` `Unhealthy` past the budget, invocations get `503` with
  `Retry-After`, and the failed marker carries `app_crashloop` with redacted
  stderr.
- **One Node bridge** (`node_bridge.mjs`, driver `node-callable`) with an
  adapter per framework: `strands` (the old `strands-ts`, kept as an alias),
  `langgraph` (LangGraph.js and deepagents-js), `vercel-ai` and `callable`.
  `CustomRuntime(entrypoint="path.ts#export", package_manager=...)` uses it.
  Frameworks without their own memory keep the conversation in a per-session
  file. The bridge forces HTTP/1.1 for the AWS SDK when the model goes through
  the gateway.
- **Install plan** (`_install_plan.py`, `install-plan.ts`): `pip`, `uv`, `npm`,
  `pnpm` and `yarn`, pinned CPython and Node builds, and `requirements` /
  `package_manager` / `python_version` / `node_version` on `CustomRuntime`,
  `StrandsRuntime(language="typescript")` and the `langgraph` runtime.
- Hermes uses the shared pinned CPython; `limits.json` carries every default and
  pin.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `ai-agent-service`: the `json` protocol, the queue and the supervision of the
  host.
- `ai-agent-runtime`: the Node bridge and its adapters, the install plan and the
  new runtime options.

## Impact

- `clients/python/src/rayito/_agent/` (`_install_plan.py`, `_custom.py`,
  `_strands.py`, `_hermes.py`, `_deepagents.py`, `_runtimes.py`, `_recipes.py`,
  `_runner/agent_host/`, `_assets/node_bridge.mjs`), their TypeScript mirrors,
  `limits.json`, `scripts/gen_agent_template_assets.py`, `testdata/agent/`.
- No `rayd` change. No new AWS call, no new cost: nothing runs unless an agent is
  deployed.

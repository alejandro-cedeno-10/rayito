## Why

`sbx.agent` (ADR-025) runs OpenCode or deepagents as a one-shot task from a
process that holds the sandbox handle, and `AgentTemplate` installs only those
two with hand-written recipes. A user who already has an agent in Strands
Agents (SDK or harness), Hermes Agent, LangGraph or their own code cannot build,
configure and serve it on Rayito without redoing the recipe, and cannot take the
same agent to Docker or Bedrock AgentCore. Version 0.14 makes "bring your own
agent" a first-class, configurable flow with one abstraction, while keeping the
existing guarantees: the model credential never reaches the agent (secrets
gateway, ADR-023), everything that costs is off by default, and versions are
pinned and hash-checked. ADR-028 holds the decision record.

## What Changes

- **New runtimes** behind the existing `AgentRuntime` port: `StrandsRuntime`
  (Python SDK, harness, and TypeScript through a Node bridge), `HermesRuntime`
  and `CustomRuntime` (install steps plus a command, a Python callable or an
  HTTP app). Registry names `strands`, `strands-harness`, `hermes` and
  `langgraph` (alias for `DeepAgents` with a required `entrypoint`). OpenCode
  and deepagents are re-expressed through two new optional ports,
  `InstallableRuntime` and `ServableRuntime`, with no behaviour change.
- **`AgentTemplate`** composes each runtime's `install_steps()` and accepts
  runtime objects as well as names. Defaults, Dockerfile and manifest of
  `("opencode", "deepagents")` stay byte-identical; the manifest gains additive
  keys.
- **In-VM agent host** (`agent_host`): `deepagents_runner.py` generalised into
  a stdlib-only host that speaks the Rayito JSONL protocol v1 over stdio (task)
  and HTTP (service), with drivers per framework. `deepagents_runner.py` stays
  as a compatibility wrapper.
- **Service contract `rayito.agent-service/1`**: the AgentCore contract
  (`GET /ping`, `POST /invocations`, JSON or SSE of v1 events) on
  `AGENT_SERVICE_PORT` in a MicroVM and on port 8080 in an exported container.
- **`AgentProject`** (Python sync and async, TypeScript): one typed object,
  or one `rayito-agent.toml` / `rayito-agent.json` file
  (`rayito.agent-project/1`), with `build()`, `run()`, `deploy()` (returns
  `AgentDeployment`: `url`, `headers`, `invoke`, `stream`, `status`, `stop`,
  `connect`) and `export()` (Docker or AgentCore build context, no AWS call).
- **CLI in both SDKs**: `rayito agent init|build|run|deploy|export|invoke|
  status|stop`; `rayito agent template build` is unchanged. The surface golden
  is regenerated.
- **Docs** (Spanish and English): a new guide "Despliega tu agente / Deploy your
  agent" with tabs per framework, updates to the agent guide, agent templates,
  optional features and pricing, a Strands `RayitoSandbox` example, and the
  "Coste y activación" box.

No breaking change. Nothing is deprecated.

## Impact

- Capabilities: `ai-agent-runtime` and `ai-agent-template` (modified and
  added requirements), new `ai-agent-project` and `ai-agent-service`, and `cli`
  (added requirement).
- Code: `clients/python/src/rayito/_agent/`, `clients/python/src/rayito/cli/
  agent.py`, `clients/typescript/src/agent/`, `clients/typescript/src/cli/
  commands/agent.ts`, `limits.json` and the generated limits, `testdata/agent/`,
  new `testdata/agent-project/`, `testdata/cli/surface.json`,
  `scripts/check_sdk_parity.py` allow-list, cost declarations.
- No change to `rayd`, the `.proto`, the base image or `infra/`.
- New pinned third-party code installed into user images only on request:
  `strands-agents`, `strands-harness` (Apache-2.0), `hermes-agent` (MIT,
  pinned by commit), Node.js (MIT) for TypeScript agents. TypeScript gains an
  optional peer `smol-toml` (BSD-3-Clause) loaded only to read a `.toml` file.
- Regression tier: the change adds features with cost and changes what goes
  into agent images, so it runs the full tier (CI, local Docker + Floci e2e,
  Python and TypeScript AWS e2e, E2B corpus) before 0.14.0, which ships after
  0.13.0 is published.

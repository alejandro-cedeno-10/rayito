## Why

Version 0.14 lets someone who already has an agent deploy it, but only after
they write a project file that names the runtime, the entrypoint, the install
recipe and the serving options by hand, run four separate one-time setups
(base image, bucket, model secret, token) and keep their own session routing
if they want more than one MicroVM. The goal of this change is that an
existing agent project, in any of the frameworks the 0.14 runtimes already
host, deploys with one call or one command and no code changes, and that every
detected value can be overridden with a typed option. Version 0.14.0 has not
been released, so this ships inside it, additive, with no breaking change to
any existing agent API.

## What Changes

- **Detection**: `rayito agent deploy`, `deploy_agent(path)` and
  `deployAgent(path)` read the project files (never run user code) and decide
  language, framework, entrypoint, HTTP app routes, protocol, install plan,
  Python/Node version and model provider. The rules are one shared data file
  generated into both SDKs. Order: project file > flag or argument > detection
  > named default. A guess is never silent: an unclear case fails with
  `ambiguous` or `not_detected` and names the flag that resolves it.
- **Typed options**: `ServeOptions` gains a bounded queue and the per-VM
  session cap is bounded by the connection limit of the VM size. New
  `ScalingOptions` (`min_vms`, `max_vms`, `warm_vms`, `sessions` shared or
  dedicated, `scale_in_after_seconds`), project `env`, `network.allow_out`,
  region, runtime versions and an `AgentDeployOptions` for the one-call deploy. File
  tables and CLI flags exist for all of it, and every default and bound is a
  named constant in `limits.json`.
- **Frameworks**: one host driver per framework and no framework knowledge in
  the domain. A new generic Node driver covers LangGraph.js, deepagents-js, the
  Vercel AI SDK and Strands TypeScript. The minimum contract for any other
  framework is an HTTP app, a callable or a command.
- **`json` protocol** for HTTP apps: `invoke()` works and the app keeps its own
  routes. Explicit `HttpApp` keeps `agentcore` as its default.
- **Fleet**: client-side session routing over the MicroVMs of the project's
  image (`AgentFleet`), warm VMs through the existing pool, scale-in through the
  existing sandbox timeout, a bounded queue and 429 handling, and replacement of
  an unhealthy VM. No server of our own.
- **Restarts**: the in-VM host restarts a crashed agent app with bounded backoff.
- **AWS defaults**: newest base image, artifact bucket, model secret and access
  token resolved in a fixed order, each overridable, none created silently.
- **Acceptance assets**: fifteen realistic sample agents that import nothing
  from rayito (`testdata/agent-samples/`), a local Docker and Floci end-to-end
  suite and an AWS end-to-end suite in both SDKs that deploy each sample
  zero-config and with custom options.
- **Docs**: bilingual guide, project-file reference, CLI, optional features,
  limits and a five-minute tutorial.

Nothing is deprecated or removed.

## Impact

- Capabilities: new `ai-agent-detection` and `ai-agent-fleet`; added
  requirements in `ai-agent-project`, `ai-agent-service`, `ai-agent-runtime`
  and `cli`.
- Code: `clients/python/src/rayito/_agent/`, `cli/agent.py`,
  `clients/typescript/src/agent/` and `src/cli/`, `limits.json`,
  `agent-detection.json` and their generators, `testdata/`, `docs/site/`.
- No change to `rayd`, the `.proto`, the base image or `infra/`. An image built
  before this change needs a rebuild to get the new host files. The Rust side
  was checked: supervision, routing, health and idle are covered by the host,
  the client and existing `rayd` features, so no Rust lane exists. Reopen that
  decision if an AWS run shows the host does not survive suspend and resume or
  that the connection cap needs admission in `rayd`.

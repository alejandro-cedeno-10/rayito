## Context

Version 0.14 hosts an existing agent through `AgentProject`, a project file and
the in-VM `agent_host` (stdlib only, shared by both SDKs). This change adds the
layer that fills that project file from the files the developer already has,
and the layer that runs more than one MicroVM of the same agent. Everything
below is additive; `AgentProject`, `AgentProject.deploy()`, `sbx.agent.run`,
`AgentTemplate`, an existing `rayito-agent.toml` and the `strands-ts` driver
name keep working as before.

## Decisions

### D1. Detection is a pure domain service over a `ProjectTree` port and never runs user code
`detect(tree, overrides) -> DetectionReport`. The port lists paths (bounded by
files, depth and bytes from `limits.json`) and reads text; the adapter is the
local file system and the unit tests use an in-memory tree. The detector reads
manifests and scans line patterns. It does not import, `exec` or `require` the
project, because it runs on a developer laptop over any repository, and it does
not use Python `ast`, because a TypeScript user can deploy a Python agent and the
reverse. Identical behaviour in both SDKs comes from one rule file
(`agent-detection.json`) generated into both, with a `--check` gate like
`gen_limits.py`; the same shared vectors (`testdata/agent-detection/`) run in
both test suites.

Order of precedence for every key: project file, then flag or argument, then
detection, then a named default. Framework priority is data: `langgraph.json`,
Strands harness, Strands, deepagents, LangGraph or LangChain `create_agent`,
Vercel `ai`, Hermes config, OpenCode config, then a bare web framework, then
`not_detected`. When both a framework object and an HTTP app exist (the usual
LangGraph plus FastAPI project), the framework driver wins because it brings
`invoke()`, streaming events and usage, and the app is listed under
`alternatives`; `--entry http` serves the app instead. `.env*` values are never
read. Ambiguity is `AgentProjectException(code="ambiguous")` and missing
evidence is `code="not_detected"`, both naming the flag that resolves them.

### D2. Four small value objects, no renames
`ServeOptions` gains `queue_max` and `queue_timeout_seconds` and its
`max_concurrency` (sessions per VM) is bounded by the connection cap of the VM
size. `ScalingOptions` holds `min_vms`, `max_vms`, `warm_vms`, `sessions` and
`scale_in_after_seconds` with defaults equal to today's single VM.
`AgentProject` gains `scaling`, `env`, `network`, `region` and runtime versions.
`DeployOptions` (bucket, base image version, secret names, create-secret,
token, dry run) describe where and how to deploy, so they are arguments of the
deploy facade, not fields of the project. `env` rejects secret-like names and
values. Memory is fixed in the image, so changing it produces a new image
version; the docs and `--dry-run` say so.

### D3. Framework knowledge lives in data and in host drivers
The domain imports no framework, no AWS client and no proto. A generic Node
driver with one small adapter per JavaScript framework (LangGraph.js and
deepagents-js, Vercel AI SDK, Strands TypeScript) joins the existing drivers.
The contract for a framework that is not listed is exactly one of: an HTTP app
with a health GET and an invoke POST, a callable, or a command.

### D4. `json` protocol for HTTP apps
The host serves `/ping` and `/invocations`, translates an invocation into a POST
on the app's own invoke path and forwards every other path unchanged, so
`invoke()` works and the app's own routes stay reachable. Explicit `HttpApp`
keeps `agentcore` as default; only detection chooses `json`.

### D5. Sessions and VMs are routed by the client
`shared` sessions fill a VM up to `max_concurrency`; `dedicated` gives each
session its own MicroVM. With more than one VM the session id is a routable
handle that names its VM, so routing needs no lookup. Members of a fleet are the
non-terminated MicroVMs of the project's image, because MicroVMs cannot be
tagged. Warm VMs are the existing pool, scale-in uses the existing sandbox
timeout and self-termination, and a bounded queue (host first, then client with
`429 Retry-After`) ends in `CapacityException`. An index of caller-chosen ids
for dedicated mode is opt-in and reuses the existing metadata-index stack.

### D6. The host supervises the app
`http-app` and long-lived child drivers restart a child that exits after ready,
with exponential backoff capped by a restart budget; while restarting `/ping`
reports busy and invocations get 503 with `Retry-After`; past the budget the
failed marker carries `app_crashloop` and the fleet replaces the VM.

### D7. Zero-config AWS defaults
For region, base image, bucket, model secret and access token the order is
explicit argument or flag, project file, environment variable, discovery, then a
Spanish error before anything is created. A missing base image is published only
after confirmation, a bucket is never created, and a secret is created only with
`--create-secret`. The model credential is never given to the agent.

### D8. Cost and compatibility
The deploy facade and `AgentFleet` carry the "Coste y activación" box. `detect`,
`load` and `--dry-run` make no AWS call and load no AWS peer, and a test in each
SDK checks it.

### D9. No Rust change
The agent in `crates/` is `rayd` itself. Supervision, session routing, health and
idle are covered by the host, the client and existing `rayd` features. Starting
the host inside the image snapshot is rejected for 0.14 and noted as a 0.15
candidate.

## JavaScript providers

Verified on 2026-10-16 by reading the published packages (versions
`@ai-sdk/amazon-bedrock` 5.0.117, `@langchain/aws` 1.4.7,
`@strands-agents/sdk` 1.21.0, `@aws-sdk/client-bedrock-runtime` 3.1152.0):

| Package | Reads `AWS_BEARER_TOKEN_BEDROCK` | Honours `AWS_ENDPOINT_URL_BEDROCK_RUNTIME` |
|---|---|---|
| `@ai-sdk/amazon-bedrock` | yes (`createAmazonBedrock`, no SigV4 when set) | yes, through its base URL resolver |
| `@langchain/aws` | yes (`resolveBedrockBearerToken`) | yes, through the AWS SDK v3 client |
| `@strands-agents/sdk` (`BedrockModel`) | yes, through the AWS SDK v3 client | yes, through the AWS SDK v3 client |
| `@aws-sdk/client-bedrock-runtime` | yes (`fromEnvSigningName` for `bedrock`) | yes (standard endpoint configuration) |

All four therefore reach the secrets gateway with the same two variables the
Python providers use, with no code change in the agent. The local end-to-end
suite repeats the check at run time against a fake Bedrock route. A provider
that failed it would be reported by detection as `unsupported` with the
one-line change, and never wired by giving the agent the key.

## Risks

- Heuristic detection can guess wrong. Mitigation: the detection summary is
  printed before building, `--dry-run` shows the plan, and ambiguity is an
  error, not a guess.
- Fleet membership by image needs the listing to carry the image ARN. The lane
  that implements it reads `AWS_API_NOTES.md` first and stops and asks if the
  field is not documented there.
- The connection cap below 2 GB is not published. The limit table uses a
  conservative value marked as assumed and the AWS run measures it.

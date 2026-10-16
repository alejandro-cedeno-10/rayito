## Context

ADR-028 holds the decision record; this file closes the details the lanes
need. Starting point on `main` (0.12.x, 0.13.0 in release):

- `rayito/_agent/_runtime.py` / `src/agent/runtime.ts`: the pure
  `AgentRuntime` port (`build_config`, `command`, `new_state`, `parse_line`,
  `finish`, `template_steps`, `warmup_steps`). `template_steps()` exists but
  `AgentTemplate` ignores it and installs OpenCode and deepagents by hand.
- `_runtimes.py` / `runtimes.ts`: registry `{"opencode", "deepagents"}` and
  `resolve_runtime`.
- `_deepagents.py` already parses the Rayito JSONL protocol v1 and supports
  `DeepAgents(entrypoint="pkg.mod:fn")`; the in-VM runner
  `_runner/deepagents_runner.py` is stdlib-only and speaks v1.
- `AgentSpec` (model, instructions, MCP, permissions, subagents) and the 12
  gateway presets are the model wiring; no type carries a key.
- `rayd` owns ports 8080 (gRPC) and 9000 (hooks). `get_host(port)` exposes any
  other port through the AWS proxy with a 60-minute JWE.
- The base image is AL2023 with Python 3.12 and no Node.

Research inputs (2026-10-16): Strands `strands-agents` 1.61.1,
`strands-harness` 0.3.1, `@strands-agents/sdk` 1.21.0,
`@strands-agents/harness` 0.3.0 (Apache-2.0); Hermes Agent v0.22.2 (MIT, no
PyPI package); `bedrock-agentcore` 1.24.1 (AgentCore HTTP contract).

## Goals / Non-goals

- Goals: one abstraction to build, run, serve and export an agent written for
  OpenCode, deepagents/LangGraph, Strands (SDK and harness, Python and
  TypeScript), Hermes or custom code; typed object and declarative file; CLI in
  both SDKs; credentials never readable by the agent inside Rayito; no breaking
  change.
- Non-goals: calling AgentCore, ECS, App Runner or Lambda APIs (not in
  `AWS_API_NOTES.md`); hosting anything outside the user's account;
  `custom-domain` integration for deployments (follow-up, needs
  `Sandbox.create(domain=)` first, ADR-024); multi-replica load balancing;
  CrewAI, OpenAI Agents SDK, Google ADK, Claude Agent SDK and Vercel AI SDK as
  named runtimes (they run through `CustomRuntime`, with docs recipes).

## Decisions

### D1 — Ports: `AgentRuntime` unchanged, two optional ports

```python
class InstallTarget(Protocol):          # pure description of where we install
    platform: Literal["microvm", "container"]
    python: str                          # interpreter used to create venvs
    install_dir: str                     # AGENT_INSTALL_DIR

@runtime_checkable
class InstallableRuntime(Protocol):
    name: str
    def install_steps(self, target: InstallTarget) -> Sequence[TemplateStep]: ...
    def context_files(self) -> Mapping[str, bytes]: ...      # package data to copy
    def manifest_entry(self) -> Mapping[str, object]: ...    # versions + hashes
    def smoke_checks(self) -> Sequence[str]: ...             # run as `user`
    def prefetch_paths(self) -> Sequence[str]: ...

@runtime_checkable
class ServableRuntime(Protocol):
    name: str
    def host_config(self, spec: AgentSpec, *, wiring: ModelWiring,
                    workdir: str) -> HostConfig: ...
```

`HostConfig` is a frozen value: `driver` (closed list), `python` (interpreter
that runs the host), `files: RuntimeFiles` (config written before start),
`envs`, and driver options. `ModelWiring` is either
`GatewayWiring(urls: Mapping[str, str])` (MicroVM) or
`DirectWiring()` (export; the host reads the provider's standard credential
source). TypeScript mirrors both as interfaces with `installSteps`,
`contextFiles`, `manifestEntry`, `smokeChecks`, `prefetchPaths`,
`hostConfig`. `resolve_runtime` is unchanged; `is_installable()` /
`is_servable()` are `isinstance` checks against the runtime-checkable
protocols. A caller's runtime that implements only `AgentRuntime` keeps working
for `sbx.agent.run`; passing it to `AgentTemplate` or `deploy()` raises
`InvalidArgumentException` naming the missing port.

### D2 — Runtime catalogue

| Registry name | Adapter | Task mode (`sbx.agent.run`) | Service driver | Install |
|---|---|---|---|---|
| `opencode` | `OpenCodeRuntime()` | unchanged (SDK parses OpenCode JSONL) | `opencode` (host translator) | unchanged |
| `deepagents` | `DeepAgents(entrypoint=None)` | unchanged (v1 via runner) | `deepagents` | unchanged |
| `langgraph` | `DeepAgents(entrypoint=...)` (required) | v1 via host | `deepagents` | deepagents venv + user requirements |
| `strands` | `StrandsRuntime()` | v1 via host | `strands` | `/opt/agents/strands` venv, hashed |
| `strands-harness` | `StrandsRuntime(harness=True)` | v1 via host | `strands` | `/opt/agents/strands-harness` venv, hashed |
| — | `StrandsRuntime(language="typescript")` | v1 via host | `strands-ts` (Node bridge) | Node + `npm ci --ignore-scripts` of the user's lockfile |
| `hermes` | `HermesRuntime()` | v1 via host | `hermes` | git clone pinned commit, verified, hashed venv |
| — | `CustomRuntime(...)` | v1 via host | `callable`, `command` or `http-app` | user steps + optional requirements |

`StrandsRuntime(entrypoint: str | None = None, harness: bool = False,
language: Literal["python", "typescript"] = "python")`. Without `entrypoint`
the host builds `Agent(model=..., system_prompt=instructions, tools=[])`
(harness: `create_harness(...)` with the safe defaults of D9); with it, the
host imports `module:attr`, which is either an `Agent` instance or a factory
`fn(ctx) -> Agent` receiving the gateway-wired model in `ctx.model`.

`HermesRuntime(toolsets: Sequence[str] = HERMES_DEFAULT_TOOLSETS,
skills_dir: str | None = None)`.

`CustomRuntime(install: Sequence[str] = (), requirements: str | None = None,
command: Sequence[str] | None = None, entrypoint: str | None = None,
app: HttpApp | None = None, protocol: Literal["rayito-v1", "text"] =
"rayito-v1", python: str | None = None)`. Exactly one of `command`,
`entrypoint`, `app`. `HttpApp(command: Sequence[str], port: int,
health_path: str = "/ping", invoke_path: str = "/invocations",
protocol: Literal["agentcore", "openai-chat", "raw"] = "agentcore")`.
`raw` (including MCP servers on Streamable HTTP) is only exposed, never
translated: `deploy()` returns `url`/`headers` and `invoke()` raises
`UnimplementedError`.

### D3 — Agent host (`agent_host`)

- Package data `rayito/_agent/_runner/agent_host/` (`__main__.py`,
  `protocol.py`, `limits.py`, `serve.py`, `drivers/*.py`), stdlib only, never
  imports `rayito`. Copied to `/opt/agents/rayito/agent_host/`, root-owned,
  0755 directories and 0644 files. TypeScript embeds the same bytes in
  `assets/template-assets.gen.ts` (generated, checked by `pack:check`).
- `python /opt/agents/rayito/agent_host task <config.json>` reads one request
  `{"v":1,"prompt","session_id","model","reasoning"}` on stdin and writes v1
  lines on a private fd (the existing runner's stdout discipline).
- `python /opt/agents/rayito/agent_host serve <config.json> --port N` serves
  `rayito.agent-service/1` (D4) with `http.server.ThreadingHTTPServer` bound
  to `0.0.0.0`, writes `AGENT_SERVICE_READY_MARKER` when the driver is warm,
  and logs only ids, statuses, durations, byte counts and codes.
- The `<config>` of both modes is `host.json` (`rayito.agent-host/1`): `driver`
  (a key of the registry dict in `__main__.py`, one line per driver), `options`
  (exactly `HostConfig.options`, which the driver receives as its configuration;
  the deepagents driver reads its own JSON from `options.config_path`), and, for
  `serve`, `limits`, `max_concurrency`, `max_request_bytes`, `session_header`
  and `ready_marker`. `HostConfig.envs` are set by whoever launches the host
  (the SDK command or the Dockerfile `ENV`), never written into `host.json`.
  `deploy()` removes any `ready` or `ready.failed` marker left by an earlier
  host, and a host that cannot start leaves `ready.failed` with an exception
  class name so `deploy()` fails at once instead of at the ready timeout.
- `deepagents_runner.py` becomes a wrapper that execs the host's `task` mode
  with the `deepagents` driver; its path, argv and output are unchanged, so
  `testdata/agent/deepagents-run-commands.json` and
  `rayito-protocol-v1*.json[l]` stay as they are.
- Drivers are imported lazily, so the host starts under any venv. Each driver
  maps framework exceptions to the closed reasons (`model_error`,
  `runtime_error`, `protocol_error`) with a class-name `detail_code`, as the
  runner does today.
- Limits in service mode: `limits.py` mirrors the SDK `LimitTracker`
  (`max_steps` at step `n+1`, token budget after each step, `timeout_seconds`
  per invocation, `max_output_bytes` per response). Shared vectors:
  `testdata/agent/limit-vectors.json`, read by the SDK tests (Python, TS) and
  the host tests.
- The `opencode` driver spawns `opencode run --format json` with the same
  script the SDK adapter builds and translates its lines. Its tests read
  `testdata/agent/opencode-v1.18.34-events.jsonl` and `expected-events.json`,
  the vectors the two SDK adapters already use.
- The `hermes` driver starts `hermes gateway run` on loopback
  (`AGENT_HERMES_API_PORT`, `API_SERVER_HOST=127.0.0.1`) with a random
  `API_SERVER_KEY` generated per start (`secrets.token_urlsafe`, kept in
  memory and passed by env to the child only), waits for `GET /health`, and
  drives `POST /v1/runs` plus its SSE events, mapping them to v1 events. The
  prompt never goes in argv. First task of lane B is a spike that records the
  exact `/v1/runs` event schema of the pinned version in
  `testdata/agent/hermes-v0.22.2-events.jsonl`; if `/v1/runs` lacks tool
  events, the driver falls back to `/v1/chat/completions` SSE and tool calls
  are reported only as `text`.
- The `strands-ts` driver runs `node /opt/agents/rayito/strands_bridge.mjs
  <config.json>`, a bridge that imports the user's module (exporting `agent`),
  iterates `agent.stream()` and writes v1 lines; the host relays them.
- The `command` driver spawns the user's argv with the request on stdin and
  either relays v1 lines (`rayito-v1`) or wraps stdout as `text_delta` plus a
  final `text` and `done` (`text`), with exit code mapping as in ADR-025.
- The `http-app` driver starts the user's server, waits for `health_path` and
  proxies `/invocations` (translating `openai-chat` SSE deltas into
  `text_delta`/`text`/`done`).

### D4 — Service contract `rayito.agent-service/1`

- `GET /ping` → `200 {"status": "Healthy" | "HealthyBusy"}`;
  `time_of_last_update` (epoch seconds) changes only when the status changes.
- `POST /invocations`, body at most `AGENT_SERVICE_MAX_REQUEST_BYTES`:
  `{"prompt": str, "session_id"?: str, "model"?: str, "reasoning"?: bool}`.
  When `session_id` is absent, the AgentCore header
  `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` is used if present.
  - `Accept: text/event-stream` → `200`, one SSE message per v1 event
    (`event: <type>`, `data: <v1 json>`), ending with `done` or
    `agent_failed`.
  - Otherwise → `200 {"v": 1, "text", "session_id", "usage": {...},
    "failed": null}` or `200` with `failed: {"reason", "detail_code"}` and
    `text` empty. HTTP errors are reserved for the contract: `400`
    (`invalid_request`), `413`, `429` (busy, with `Retry-After`), `503` (not
    ready).
- Concurrency `AGENT_SERVICE_MAX_CONCURRENCY` (1); `/ping` reports
  `HealthyBusy` while at the cap.
- Optional bearer check for exported containers only:
  `RAYITO_AGENT_SERVICE_TOKEN` in the container environment makes the host
  require `Authorization: Bearer <token>` (constant-time compare). In a
  MicroVM the AWS proxy JWE is the authentication.
- Port: `AGENT_SERVICE_PORT` (8088) in a MicroVM, `AGENT_SERVICE_EXPORT_PORT`
  (8080, AgentCore) in a container.

### D5 — `AgentProject`, file format and loading

```python
@dataclass(frozen=True)
class AgentProject:
    name: str                                         # validate_name; image + deployment label
    runtime: str | AgentRuntime                       # registry name or adapter object
    spec: AgentSpec                                   # model, instructions, mcp, permissions, agents
    gateways: Mapping[str, SecretGateway] = {}
    source: str | None = None                         # local dir copied to AGENT_APP_DIR (root, read-only)
    exclude: Sequence[str] = ()                       # added to the .dockerignore rules of templates
    limits: AgentLimits = AgentLimits()
    serve: ServeOptions = ServeOptions()
    allow_internet_access: bool = False
    egress: Sequence[str] | None = None               # Sandbox.create(egress=) allowlist
    memory_mib: int = AGENT_MIN_MEMORY_MIB
    base: str = DEFAULT_AGENT_TEMPLATE_BASE
    template: str | None = None                       # prebuilt image name; default = name

    @classmethod
    def load(cls, path: str | os.PathLike[str] = ".") -> AgentProject: ...
    @classmethod
    def from_dict(cls, data: Mapping[str, object], *, base_dir: str) -> AgentProject: ...
    def to_dict(self) -> dict[str, object]: ...       # canonical, round-trips from_dict

@dataclass(frozen=True)
class ServeOptions:
    port: int = AGENT_SERVICE_PORT
    timeout_seconds: float = DEFAULT_AGENT_DEPLOY_TIMEOUT_SECONDS   # sandbox lifetime, <= 8 h
    idle: IdlePolicy | None = DEFAULT_IDLE_POLICY                   # auto-suspend + auto-resume
    max_concurrency: int = AGENT_SERVICE_MAX_CONCURRENCY
    metadata: Mapping[str, str] = {}
```

TypeScript: `new AgentProject({ name, runtime, spec, gateways, source,
exclude, limits, serve: { port, timeoutMs, idle, maxConcurrency, metadata },
allowInternetAccess, egress, memoryMib, base, template })`,
`AgentProject.load(path)`, `AgentProject.fromObject(data, { baseDir })`,
`toObject()`.

File: `rayito-agent.toml` (preferred) or `rayito-agent.json`, looked up in
that order in a directory. Schema `rayito.agent-project/1`, published as JSON
Schema at `testdata/agent-project/schema.json` and in the docs. Example:

```toml
schema = "rayito.agent-project/1"
name = "support-bot"

[runtime]
kind = "strands"                  # opencode | deepagents | langgraph | strands | strands-harness | hermes | custom
entrypoint = "agent:agent"        # strands/langgraph/custom callable
# language = "typescript"         # strands only

[source]
path = "."
requirements = "requirements.txt"
exclude = [".venv", "node_modules"]

[model]
provider = "bedrock"
id = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
region = "us-east-1"
gateway = "bedrock"

[gateways.bedrock]
preset = "bedrock"                # one of the 12 presets, or "custom" with upstream/allow
secret = "rayito/bedrock-key"     # the secret's NAME in Secrets Manager, never a value
region = "us-east-1"
models = ["us.anthropic.claude-haiku-4-5-20251001-v1:0"]

[agent]
instructions_file = "AGENTS.md"   # or instructions = "..."
[agent.permissions]
default = "allow"
tools = { webfetch = "deny" }

[limits]
max_steps = 30
timeout_seconds = 300

[serve]
timeout_seconds = 3600
idle_seconds = 300

[network]
allow_internet_access = false
egress = []
```

Rules: unknown keys at any level raise `InvalidArgumentException` with the
dotted path (`runtime.entrpoint`); relative paths resolve against the file's
directory and may not escape it; `instructions` and `instructions_file` are
mutually exclusive; times are seconds in the file for both SDKs and
TypeScript converts to `*Ms`; no field accepts a secret value (`secret` is a
name, validated with the existing secret-name rules); a gateway preset maps 1:1
to the existing preset functions. Parsing is file → plain mapping (adapter:
`tomllib` or `JSON.parse`; TypeScript loads `smol-toml` with
`loadOptionalPeer` only for `.toml`) → `from_dict` (pure domain). Shared
vectors in `testdata/agent-project/`: `valid/<case>.toml` plus
`valid/<case>.expected.json` (the canonical `to_dict()`), and
`invalid/<case>.toml` plus `invalid/cases.json` (`{case, path, code}`), read
by both SDKs.

Details closed while implementing lane A:

- The dataclass gains `requirements` (the `[source] requirements` file, resolved
  like `source`) and `base_dir` (the directory `to_dict()` writes relative paths
  against; `load()` and `from_dict()` set it). `source` and `requirements` hold
  the resolved path, so `build()` can use `source` directly.
- Paths in the file resolve against the file's directory without touching the
  disk (`from_dict`); an absolute path or one that leaves the directory is
  `path_escape`. `instructions_file` goes through a reader that `load()` injects
  (it also refuses symlinks that leave the directory); `to_dict()` writes the
  text as `agent.instructions`.
- Sections added to the example: `[image]` (`base`, `template`, `memory_mib`).
  `[serve] idle_seconds = 0` removes the idle policy and
  `[limits] max_total_tokens = 0` removes the token budget.
- `to_dict()` writes every gateway as `preset = "custom"` (`upstream`, `headers`
  mapping header to secret name, `allow`): a built `SecretGateway` cannot be
  turned back into the preset that made it, and the expanded form round-trips.
- Read errors are `AgentProjectException` / `AgentProjectError` (an
  `InvalidArgumentException` / `InvalidArgumentError`) with a dotted `path` and
  a `code` from a closed list: `unknown_key`, `missing_key`, `invalid_type`,
  `invalid_value`, `path_escape`, `conflict`, `secret_value`,
  `unsupported_schema`, `file_not_found`. An unknown key named like a
  credential (`api_key`, `token`...) and a secret name that looks like a value
  (whitespace or a known credential prefix) are `secret_value`.
- `schema.json` rejects every structural invalid case; the cases that need a
  file, a cross-reference or a runtime rule are marked `"schema": "semantic"` in
  `invalid/cases.json`.
- `DeepAgents(entrypoint=None, require_entrypoint=False)` is the adapter behind
  the registry name `langgraph` (`require_entrypoint=True`); `deepagents` takes
  no `entrypoint` in the file, `langgraph` requires it.
- `AgentTemplate` also takes `app_exclude` (dockerignore patterns applied after
  the `.dockerignore` of `app`), which `AgentProject.exclude` feeds. Template
  steps are `run` and `copy`; a copy step marked `executable` ends 0755.
- `DirectWiring` (export) writes the host configuration without a gateway URL or
  placeholder (`base_url` and `credential_placeholder` are `null` for
  deepagents, no `baseURL` or `endpoint` for OpenCode); a provider without a
  standard endpoint (`openai-compatible`, `litellm`) and an `McpRemote` fail
  with `InvalidArgumentException`.

### D6 — Operations and the `AgentDeployment` client

```python
# Python sync
project = AgentProject.load("./support-bot")
info: BuildInfo = project.build(bucket="amzn-s3-demo-bucket")
result: AgentResult = project.run("Resume el README.", session_id=None)
deployment: AgentDeployment = project.deploy()
deployment.url            # https://<host> of AGENT_SERVICE_PORT
deployment.headers        # fresh AWS proxy headers on every read
deployment.sandbox_id
result = deployment.invoke("Hola", session_id=None, model=None)   # AgentResult
with deployment.stream("Hola") as events:                          # AgentStream-like
    for event in events: ...
deployment.status()       # DeploymentStatus(state, ready, busy, port, project)
deployment.stop()         # kills the sandbox
AgentDeployment.connect(sandbox_id)                                # from any process
export: AgentExport = project.export(target="agentcore", out_dir="./build")

# Python async
project = AsyncAgentProject.load(".")
info = await project.build(bucket=...)
result = await project.run("...")
deployment = await project.deploy()
result = await deployment.invoke("...")
async with deployment.stream("...") as events:
    async for event in events: ...
await deployment.stop()
deployment = await AsyncAgentDeployment.connect(sandbox_id)
```

```ts
// TypeScript
const project = await AgentProject.load("./support-bot");
const info = await project.build({ bucket: "amzn-s3-demo-bucket" });
const result = await project.run("Resume el README.");
const deployment = await project.deploy();
const reply = await deployment.invoke("Hola", { sessionId });
for await (const event of deployment.stream("Hola")) { /* AgentEvent */ }
await deployment.status();
await deployment.stop();
const again = await AgentDeployment.connect(sandboxId);
const exported = await project.export({ target: "docker", outDir: "./build" });
```

- `build()` = `AgentTemplate(name=template or name, base, runtimes=(runtime,),
  memory_mib, prefetch=True, kernel_warmup=False, app=source)`. `build()`
  keeps the `Template.build` options (`force`, `timeout`, `on_build_logs`,
  `region`, `session`).
- `run()` = `Sandbox.create(template, gateways, allow_internet_access, egress,
  timeout=limits.timeout_seconds + AGENT_RUN_SANDBOX_MARGIN_SECONDS)`, then
  `sbx.agent.run(prompt, spec=spec, runtime=runtime, limits=limits)`, then
  `kill()` (also on error). Accepts `session_id`, `model` and `reasoning`.
  A caller who wants the sandbox to survive uses `deploy()` instead.
- `deploy()` = `Sandbox.create(..., timeout=serve.timeout_seconds,
  idle=serve.idle, metadata={AGENT_DEPLOYMENT_LABEL: name,
  AGENT_DEPLOYMENT_PORT_LABEL: port, **serve.metadata})`, write the host
  config with gateway URLs, start `agent_host serve` as a background command
  (uid 1000, no timeout, tag `rayito-agent-serve`), wait for the ready marker
  within `AGENT_SERVICE_READY_TIMEOUT_SECONDS`, then `get_host(port)`. On any
  failure it kills the sandbox and raises.
- Transport of the client: Python sync uses `http.client.HTTPSConnection`;
  Python async uses `asyncio.open_connection` with TLS and the HTTP/1.1 framing
  helpers that already exist for the proxy (`cli/_http_framing.py`, moved to a
  shared private module if needed); TypeScript uses global `fetch`. The SSE
  parser and the v1 event decoder are pure and shared (`_sse.py` /
  `sse.ts`, `_protocol_v1.py` / `protocol-v1.ts`, the latter extracted from
  the deepagents adapter so both use one decoder). A `403` from the proxy
  re-mints the token once (the existing `proxy_rejected` rule).
- `export(target="docker" | "agentcore", out_dir=...)` renders, without any
  network call: `Dockerfile` (base `AGENT_EXPORT_BASE_IMAGE`, pinned by digest,
  `linux/arm64`), the runtime's `install_steps(container target)`, the host,
  the host config with `DirectWiring`, the user's source, a `.dockerignore`
  and a `README.md` with the build and run commands. `agentcore` adds the
  `agentcore` CLI hint and checks the contract constraints (arm64, port 8080).
  Returns `AgentExport(path, files, port, platform)`. It refuses to write into
  a non-empty directory unless `overwrite=True`.

### D7 — Provider wiring per runtime

The catalogue `testdata/agent/provider-catalogue.json` gains a
`runtimes` matrix (`runtime -> provider -> {supported, client, base_path}`),
read by both SDKs and by the host tests. Gateway wiring (MicroVM):

| Provider | Strands (Python) | Strands (TS) | Hermes |
|---|---|---|---|
| `bedrock` | `BedrockModel(model_id, region_name, endpoint_url=<gw>, api_key=<placeholder>)`, plus `AWS_ENDPOINT_URL_BEDROCK_RUNTIME` and `AWS_BEARER_TOKEN_BEDROCK` in the environment | `BedrockModel({ region, clientConfig: { endpoint: <gw> } })` with `AWS_BEARER_TOKEN_BEDROCK` in the environment (`apiKey` alone fails before sending: SigV4 still wants credentials) | `provider: bedrock` with `AWS_ENDPOINT_URL_BEDROCK_RUNTIME=<gw>` and `AWS_BEARER_TOKEN_BEDROCK=<placeholder>` (measured: boto3 honours the endpoint variable and sends the bearer, no SigV4) |
| `anthropic` | `AnthropicModel(client_args={"base_url": <gw>, "api_key": <placeholder>})` | `AnthropicModel({ clientArgs: { baseURL, apiKey } })` | `ANTHROPIC_BASE_URL=<gw>`, `ANTHROPIC_API_KEY=<placeholder>` |
| `openai`, `openai-compatible` | `OpenAIModel(client_args={"base_url": <gw><base_path>, "api_key": <placeholder>})` | `OpenAIModel({ clientConfig: { baseURL, apiKey } })` | `model: {provider: custom, base_url: <gw><base_path>, api_key: <placeholder>}` |
| `google` | `GeminiModel(client_args={api_key, http_options: {base_url}})` | `GoogleModel({ apiKey, clientConfig: { httpOptions: { baseUrl } } })` | unsupported |
| `azure` | unsupported | unsupported | unsupported |

`azure` is unsupported by the three framework runtimes: the Azure preset
injects the `api-key` header while these clients send `Authorization`, and
nothing measured proves the gateway accepts that. Use `CustomRuntime` with an
Azure SDK. Unsupported combinations raise `InvalidArgumentException` before
any RPC. Every adapter sets `MODEL_CREDENTIAL_PLACEHOLDER` as the key. The host never
reads AWS credentials, and `allow_internet_access=False` keeps IMDS and the
internet closed (ADR-012).

### D8 — Install recipes and pins (`limits.json`)

New keys (generated into `_limits.py` / `limits.ts`; names illustrative of the
generator's casing):

| Key | Value | Source |
|---|---|---|
| `agentServicePort` | 8088 | free port; 8080/9000 are `rayd` |
| `agentServiceExportPort` | 8080 | AgentCore runtime contract |
| `agentServiceHealthPath` / `agentServiceInvokePath` | `/ping` / `/invocations` | AgentCore |
| `agentServiceSessionHeader` | `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` | AgentCore |
| `agentServiceMaxConcurrency` | 1 | ADR-025 one run per sandbox |
| `agentServiceMaxRequestBytes` | 1048576 | bound on the request body |
| `agentServiceReadyTimeoutSeconds` | 120 | measured in lane C, Hermes cold start |
| `defaultAgentDeployTimeoutSeconds` | 3600 | same as `Sandbox.create` default |
| `agentRunSandboxMarginSeconds` | 60 | `run()` sandbox lifetime over the run timeout |
| `agentServiceReadyMarkerPath` | `/home/user/.rayito/agent/host/ready` | under `agentStateDir` |
| `agentHermesApiPort` | 8642 | Hermes default, loopback only |
| `agentStrandsVersion`, `agentStrandsHarnessVersion` | 1.61.1, 0.3.1 | PyPI 2026-10-16 |
| `agentStrandsRequirementsSha256`, `agentStrandsHarnessRequirementsSha256` | sha256 of the package-data files | generated |
| `agentStrandsTsVersion`, `agentStrandsTsHarnessVersion` | 1.21.0, 0.3.0 | npm 2026-10-16 |
| `agentHermesVersion`, `agentHermesCommit` | 0.22.2, full commit sha | GitHub release tag |
| `agentHermesRequirementsSha256` | sha256 | generated |
| `agentNodeVersion`, `agentNodeSha256` | 22.x LTS linux-arm64 | nodejs.org SHASUMS256 |
| `agentExportBaseImage`, `agentExportBaseImageDigest` | `python:3.12-slim-bookworm`, `sha256:...` | Docker Hub, pinned by digest |
| `agentProjectSchema` | `rayito.agent-project/1` | this change |
| `agentProjectFileNames` | `["rayito-agent.toml", "rayito-agent.json"]` | this change |
| `agentServiceSchema` | `rayito.agent-service/1` | this change |

Requirements files are produced with `uv pip compile --generate-hashes
--python-version 3.12 --python-platform aarch64-manylinux_2_34` (AL2023 glibc
2.34) and committed as package data, with a `scripts/` regeneration command
documented in the file header. Hermes is installed with `git clone --depth 1
--branch v<ver>`, `test "$(git rev-parse HEAD)" = <commit>`, then
`pip install --require-hashes --no-deps -r requirements-hermes.txt` and
`pip install --no-deps --no-build-isolation .`; any extra the checkout needs
(Node for browser tools) is skipped with its documented `--skip-*` switches.
User requirements are installed into the runtime's venv after the pinned set;
`--require-hashes` is added automatically when every requirement line has a
`--hash`, and the build always ends with `pip check`.

### D9 — Safe defaults per framework

- Strands SDK: no `OTEL_*` exporter variables; `callback_handler=None`; tools
  only what the user's entrypoint defines plus, when a sandbox is wanted, none
  (the agent already runs inside the MicroVM).
- Strands harness: `builtin_tools` with `programmatic_tool_caller=False`,
  `web_search=False`, `web_fetch` following `AgentPermissions` (denied by
  default, `DEFAULT_DENIED_TOOLS`); `shell`, `read`, `write`, `edit` and
  `subagent` follow `AgentPermissions`; `session` and `memory` dirs under
  `AGENT_STATE_DIR/strands`; `interventions` never `"ask"` (headless).
- Hermes: `HERMES_HOME=AGENT_STATE_DIR/hermes/home`,
  `HERMES_IGNORE_USER_CONFIG=1`, `TERMINAL_ENV=local`, toolsets limited to
  `HERMES_DEFAULT_TOOLSETS`, no CORS, API on 127.0.0.1 only, no cron jobs
  enabled, skills copied read-only when `skills_dir` is set.
- All: `allow_internet_access=False` by default; autoupdate off where the
  framework has it; logs content-free.

### D10 — CLI (both SDKs, golden updated)

```
rayito agent init [DIR] --runtime {opencode|deepagents|langgraph|strands|strands-harness|hermes|custom}
                  [--language python|typescript] [--provider P] [--model ID] [--region R] [--force]
rayito agent build [--file PATH] --bucket B [--force] [--timeout S]
rayito agent run PROMPT [--file PATH] [--session-id ID] [--model ID] [--stream]
rayito agent deploy [--file PATH] [--timeout S] [--port N]
rayito agent export [--file PATH] --target {docker|agentcore} --out DIR [--overwrite]
rayito agent invoke SANDBOX_ID PROMPT [--session-id ID] [--stream]
rayito agent status SANDBOX_ID
rayito agent stop SANDBOX_ID
rayito agent template build ...        # unchanged
```

`--file` defaults to the current directory (lookup of
`agentProjectFileNames`). `--json` everywhere. `init` writes the file and a
minimal scaffold per runtime (`agent.py` for Strands, `agent.ts` plus
`package.json` for Strands TS, `graph.py` for LangGraph, `run.sh` for custom)
and never overwrites without `--force`. Defaults come from SDK constants.
`testdata/cli/surface.json` is regenerated by its pytest, and the TypeScript
surface test and `check_sdk_parity.py --cli` must pass in the same PR.

### D11 — Errors and logging

No new exception class: validation is `InvalidArgumentException` /
`InvalidArgumentError`; run failures are `AgentException` / `AgentError` with
the existing closed `reason` list; a missing port or a `raw` app invoke is
`UnimplementedError`; deployment HTTP failures map by status
(`503` → `TimeoutException` after the ready timeout, `429` →
`AgentException(reason="busy")`, `403` twice → `AuthenticationException`).
Messages are Spanish and never carry the prompt, URLs with tokens, header
values or secret names.

## Test plan

- **Unit, both SDKs, with fakes** (no AWS): domain validation; file loading
  over `testdata/agent-project/`; registry and port detection; each adapter's
  `install_steps`, `host_config`, `command` and the provider matrix; the
  Dockerfile and manifest of `AgentTemplate()` byte-identical to today's
  `testdata/agent/agent-template/cases.json` plus new cases for each runtime;
  `deploy()` orchestration over a `FakeSandbox` (create kwargs, files written,
  background command, ready wait, kill on failure); `AgentDeployment` over a
  fake HTTP transport (JSON, SSE, 429, 403 re-mint); `export()` file tree
  snapshots in `testdata/agent/export/`; the zero-call test for every new
  entry point; Python sync/async signature parity.
- **Host tests** (Python, the host is tested once): each driver against fake
  framework modules in `sys.modules`; the serve transport over a real loopback
  socket; `limit-vectors.json`; OpenCode translation against the shared
  vectors; Hermes against the recorded event file.
- **Local e2e, Docker + Floci** (`make local-up`, `make local-e2e`): the
  custom runtime (command v1 and http-app) and the project file flow through
  `rayito agent run|deploy|invoke|stop` in both CLIs against the local `rayd`
  container, with the local provider fake (`dev/local/agent/litellm.sh`); and
  `rayito agent export --target docker` built with local Docker and probed on
  `/ping` and `/invocations`.
- **AWS e2e** (`RAYITO_E2E=1`, Bedrock through `bedrock_gateway`, Claude
  Haiku 4.5, prefix `rayito-v014-<run>`): two throwaway images
  (`<prefix>-py`: deepagents, langgraph, strands, strands-harness, custom;
  `<prefix>-cli`: opencode, hermes, strands TS); per runtime one `run()` and
  one `deploy()` + `invoke()` + `stream()` + `stop()`; Python lane and
  TypeScript lane in parallel on different images; plus the full regression
  tier (existing Python and TypeScript e2e, E2B corpus). Inventory before and
  after; delete only the run's resources.
- **Budget cap**: 2 image builds per lane (4 total, about 5 min each), image
  versions deleted at the end (one-week minimum storage, about $0.06 to $0.10
  each), at most 40 MicroVM launches of up to 4 GiB and 10 min, model spend
  under $1 (Haiku, short prompts). Total under $5, stated in the PR.

### Spike results (lane B, 2026-10-16)

Recorded in `testdata/agent/strands-spike.json` and
`testdata/agent/hermes-v0.22.2-spike.json`, against local fake servers (no
AWS):

- Strands (Python and TS): Bedrock, Anthropic, OpenAI chat and Gemini all reach
  the gateway URL with only the placeholder as credential. The harness
  `builtin_tools` names are `shell`, `read`, `write`, `edit`, `web_fetch`,
  `web_search`, `programmatic_tool_caller` and `subagent`.
- Hermes v0.22.2 only ships a lock for CPython 3.14, so the image installs a
  python-build-standalone 3.14 (sha256 pinned) and a venv from the lock
  (`uv export`, hashes), runs from the checkout at the pinned commit
  (`python -P -m hermes_cli.main gateway run`, `PYTHONPATH` the checkout) and
  adds `aiohttp` (extra `slack` without its Slack packages), `boto3` and
  `anthropic`. `POST /v1/runs` answers 202 and its SSE has `tool.started`,
  `tool.completed`, `message.delta`, `reasoning.available` and `run.*`
  events; tool arguments are not exposed. `HERMES_IGNORE_USER_CONFIG=1` only
  affects the CLI: the gateway reads `$HERMES_HOME/config.yaml` (written as
  JSON, which is YAML).
- The task contract for all three runtimes goes through the host (`task`
  mode) with a `wiring.env` file the script loads, because `command()` does
  not know the gateway URLs.

## Risks

1. **Release pace**: Strands 1.x ships weekly and harness/CLI are 0.x; Hermes
   releases almost daily and migrates config on start. Pin exact versions and
   commits; a unit test asserts the pinned CLI flags and config keys still
   appear in the recorded fixtures; bumps are Dependabot-free manual PRs.
2. **Licences**: Strands, `bedrock-agentcore` Apache-2.0 (NOTICE kept if
   anything is vendored; nothing is planned); Hermes MIT; Node MIT;
   `smol-toml` BSD-3-Clause. `langgraph-api` (Elastic-2.0) is never installed.
   `check_license.py` and `THIRD_PARTY_LICENSES.md` are unaffected because
   these are installed into user images, not shipped in our packages; the docs
   list them.
3. **Image size and cold start**: Hermes (and Node) make bigger snapshots and
   slower first starts; measured in AWS and published in a "Medido en AWS
   real" box; separate images per runtime are recommended.
4. **Hermes Bedrock** (closed): the spike proved the endpoint override and
   bearer auth work through environment variables, so Bedrock is supported.
5. **Hermes `/v1/runs` schema** (closed): recorded in
   `testdata/agent/hermes-v0.22.2-events.jsonl`; `/v1/runs` has tool events, so
   the chat-completions fallback is not needed.
6. **Harness defaults**: tool names in `builtin_tools` must match the pinned
   version; a fixture test guards them.
7. **Third OpenCode translator**: drift guarded by the shared vectors.
8. **8-hour lifetime**: a deployment is not a permanent service; the docs say
   so and show rotation with `deploy()` plus `connect()`.
9. **Native wheels**: `pydantic-monty` (harness) and others must have
   aarch64 manylinux wheels for glibc 2.34; `--only-binary=:all:` fails the
   build otherwise, which is the desired signal.

## Implementation lanes (stacked PRs, AWS acceptance once at the end)

| Lane | Branch | Owns (exclusive) | Depends on |
|---|---|---|---|
| A, contract | `feat/agent-byo-contract` | `_agent/_runtime.py`, `_runtimes.py`, `_protocol_v1.py` (extracted), `_deepagents.py` (decoder moved out only), `_project_domain.py`, `_project_file.py`, `_recipes.py`, `_template.py`, `_agent/__init__.py`, `rayito/__init__.py`; TS `runtime.ts`, `runtimes.ts`, `protocol-v1.ts`, `deepagents.ts`, `project.ts`, `project-file.ts`, `recipes.ts`, `template.ts`, `index.ts`; `limits.json` + generated; `testdata/agent-project/`, `testdata/agent/agent-template/`, `testdata/agent/limit-vectors.json`; `check_sdk_parity.py` allow-list; `tests/unit/public-exports.test.ts` | none (lands first; publishes the interfaces of D1 and D5) |
| B, frameworks | `feat/agent-byo-frameworks` | `_agent/_strands.py`, `_hermes.py`, `_custom.py`; `_assets/requirements-{strands,strands-harness,hermes}.txt`, `_assets/strands_bridge.mjs`; `_runner/agent_host/drivers/{strands,strands_ts,hermes,callable,command}.py`; TS `strands.ts`, `hermes.ts`, `custom.ts`; `testdata/agent/{strands,hermes,custom}-*`, `provider-catalogue.json` runtimes matrix | A |
| C, host and deploy | `feat/agent-byo-deploy` | `_runner/agent_host/{__main__,protocol,limits,serve}.py`, `drivers/{deepagents,opencode,http_app}.py`, `_runner/deepagents_runner.py` (wrapper); `_project_service.py`, `_project_service_async.py`, `_deployment_base.py`, `_deployment.py`, `_deployment_async.py`, `_sse.py`, `_export.py`; TS `project-service.ts`, `deployment.ts`, `sse.ts`, `export.ts`, `assets/template-assets.gen.ts` (regenerated); `testdata/agent/export/`; local e2e under `clients/python/tests/local/` and `dev/local/agent/` | A (and B's drivers to register; the driver registry is a dict in `__main__.py` with one line per driver, so B adds lines in its PR after C or C leaves the slots) |
| D, CLI, docs, AWS e2e | `feat/agent-byo-cli-docs` | `cli/agent.py`, `cli/_agent_scaffold.py`; TS `cli/commands/agent.ts`, `cli/agent-scaffold.ts`; `testdata/cli/surface.json`; `docs/site/docs/guias/despliega-tu-agente{,.en}.md`, updates to `agente-en-el-sandbox`, `templates-de-agente`, `optional-features`, `cost`, `mkdocs.yml` nav and `nav_translations`; `docs/examples/strands_rayito_sandbox.py`; `clients/typescript/scripts/cost-declarations/agent-project.json`; both CHANGELOGs; AWS e2e `tests/e2e/test_agent_project_*.py` and `clients/typescript/tests/e2e/agent-project*.test.ts` | A, B, C |

Stack order: A → B → C → D, each PR open for review; lanes B, C and D start
in parallel against the interfaces fixed in this design and rebase on A. The
AWS acceptance (full tier) runs once on top of D.

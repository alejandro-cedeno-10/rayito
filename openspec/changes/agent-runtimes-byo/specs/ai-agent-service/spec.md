## ADDED Requirements

### Requirement: The agent host speaks protocol v1 over stdio and HTTP
The agent image SHALL contain the agent host under `/opt/agents/rayito/agent_host/`, written with the Python standard library only and never importing `rayito`. `task <config>` SHALL read one request on stdin and write Rayito JSONL protocol v1 lines; `serve <config> --port N` SHALL serve `rayito.agent-service/1`. Drivers SHALL be imported lazily and SHALL be `deepagents`, `strands`, `strands-ts`, `hermes`, `opencode`, `callable`, `command` and `http-app`. `/opt/agents/rayito/deepagents_runner.py` SHALL remain, with the same argv and output as before, as a wrapper over the host's `task` mode. In service mode the host SHALL enforce `AgentLimits` with the semantics of the SDK's limit tracker, and both SDKs and the host SHALL pass `testdata/agent/limit-vectors.json`. The host's OpenCode translation SHALL produce the events of the shared OpenCode vectors. The host SHALL log only ids, states, durations, byte counts and codes.

#### Scenario: the deepagents runner path still works
- **WHEN** the existing deepagents runner tests run against the wrapper
- **THEN** they pass without changing any vector

#### Scenario: limits in service mode
- **WHEN** the host replays each case of `testdata/agent/limit-vectors.json`
- **THEN** it stops with the case's reason at the case's event index

### Requirement: rayito.agent-service/1 is the AgentCore contract
The host in service mode SHALL answer `GET /ping` with `200 {"status": "Healthy" | "HealthyBusy"}` (with `time_of_last_update` changing only when the status changes) and `POST /invocations` with a body of at most `AGENT_SERVICE_MAX_REQUEST_BYTES` holding `prompt` and optional `session_id`, `model` and `reasoning`; when `session_id` is absent it SHALL use the `AGENT_SERVICE_SESSION_HEADER` header. With `Accept: text/event-stream` it SHALL stream one SSE message per v1 event, ending in `done` or `agent_failed`; otherwise it SHALL return `{"v": 1, "text", "session_id", "usage", "failed"}`. It SHALL return `400` for an invalid body, `413` above the size limit, `429` with `Retry-After` above `max_concurrency` and `503` before it is ready. In a container, when `RAYITO_AGENT_SERVICE_TOKEN` is set, it SHALL require `Authorization: Bearer <token>` compared in constant time; it SHALL never log the token, the prompt or any header value.

#### Scenario: busy agent
- **WHEN** a contract test sends a second invocation while the first is running with `max_concurrency=1`
- **THEN** the second gets `429` and `/ping` reports `HealthyBusy` until the first ends

#### Scenario: streaming
- **WHEN** a contract test posts a prompt with `Accept: text/event-stream` to a host with a fake driver
- **THEN** the response is `text/event-stream` and its last message is a `done` event with a `session_id`

### Requirement: deploy() serves the agent from a MicroVM and returns an AgentDeployment
`AgentProject.deploy()` SHALL create one sandbox from the project's image with the project's gateways, network options, `serve.timeout_seconds`, `serve.idle` and metadata labels `AGENT_DEPLOYMENT_LABEL` and `AGENT_DEPLOYMENT_PORT_LABEL`; write the host configuration with the gateway URLs; start the host in service mode as `user` in the background with no timeout; wait for the ready marker for at most `AGENT_SERVICE_READY_TIMEOUT_SECONDS`; and return an `AgentDeployment` (Python also `AsyncAgentDeployment`) with `url`, `headers` (fresh AWS proxy headers on every read), `sandbox_id`, `invoke(prompt, session_id=None, model=None) -> AgentResult`, `stream(...)`, `status()`, `stop()` and the class method `connect(sandbox_id)` that rebuilds the client from the sandbox metadata in any process. If any step fails, `deploy()` SHALL kill the sandbox before raising. A `403` from the AWS proxy SHALL re-mint the token once. `invoke()` and `stream()` SHALL raise `UnimplementedError` for a `raw` `HttpApp`.

#### Scenario: deploy cleans up on a host that never gets ready
- **WHEN** a unit test deploys over a fake sandbox whose ready marker never appears
- **THEN** `deploy()` raises `TimeoutException` / `TimeoutError` after the ready timeout and the fake recorded one `kill()`

#### Scenario: connect from another process
- **WHEN** a unit test calls `AgentDeployment.connect(sandbox_id)` over a fake whose metadata carries the deployment labels
- **THEN** the deployment's `url` uses the labelled port and `invoke()` posts to `/invocations`

### Requirement: export() renders a portable container without calling AWS
`AgentProject.export(target="docker" | "agentcore", out_dir=..., overwrite=False)` SHALL write, with no network call, a build context for `linux/arm64`: a `Dockerfile` from `AGENT_EXPORT_BASE_IMAGE` (pinned by digest) with the runtime's container install steps, the agent host, the host configuration with direct model wiring, the user's source, a `.dockerignore` and a `README.md` with the build and run commands; the container SHALL serve `rayito.agent-service/1` on `AGENT_SERVICE_EXPORT_PORT` (8080). `agentcore` SHALL add AgentCore-specific instructions and check its constraints. It SHALL refuse a non-empty `out_dir` unless `overwrite` is true, and SHALL return `AgentExport(path, files, port, platform)`. The documentation SHALL state that in an exported container the model credential comes from the platform and the gateway guarantee no longer applies.

#### Scenario: export file tree
- **WHEN** a unit test exports each project of `testdata/agent/export/` in both SDKs
- **THEN** the written file names and the `Dockerfile` equal the case's snapshot, and no AWS client was created

#### Scenario: local container honours the contract
- **WHEN** the local e2e builds an exported custom-runtime project with Docker and probes it
- **THEN** `GET /ping` returns `Healthy` and `POST /invocations` returns the fake model's text

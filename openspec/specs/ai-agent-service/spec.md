# ai-agent-service Specification

## Purpose
TBD - created by archiving change agent-runtimes-byo. Update Purpose after archive.

## Requirements

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

### Requirement: The service keeps a bounded FIFO queue in front of max_concurrency
The service SHALL accept `queue_max` and `queue_timeout_seconds` in `host.json`, clamped to `AGENT_SERVICE_QUEUE_MAX` and `AGENT_SERVICE_QUEUE_TIMEOUT_SECONDS_MAX`, with the defaults `AGENT_SERVICE_QUEUE_MAX_DEFAULT` (`0`, no queue) and `AGENT_SERVICE_QUEUE_TIMEOUT_SECONDS_DEFAULT`. An invocation that finds every slot taken SHALL wait its turn in arrival order when the queue has room, SHALL NOT be overtaken by a later request, and SHALL get `429` with `Retry-After` and the error `busy` when the queue is full or off, or `queue_timeout` after waiting `queue_timeout_seconds`. `/ping` SHALL stay `HealthyBusy` while requests wait.

#### Scenario: a queued invocation runs when a slot frees up
- **WHEN** `queue_max` is 1, one invocation holds the only slot and a second one arrives
- **THEN** the second waits and runs after the first finishes, and both return `200`

#### Scenario: the queue is full
- **WHEN** the slot is taken and `queue_max` invocations already wait
- **THEN** the next one gets `429` with `Retry-After` and the error `busy`

#### Scenario: the wait times out
- **WHEN** a queued invocation waits `queue_timeout_seconds` without a slot
- **THEN** it gets `429` with the error `queue_timeout` and leaves the queue

#### Scenario: no queue by default
- **WHEN** `host.json` has no `queue_max` and a second invocation arrives while the slot is taken
- **THEN** it gets `429` immediately, as in 0.14

### Requirement: The host restarts a crashed app with backoff and reports a crash loop
The `http-app` and `hermes` drivers SHALL run their child under a supervisor. A child that exits after it was ready SHALL be restarted after a wait that starts at `AGENT_APP_RESTART_BACKOFF_SECONDS`, doubles on each death and is capped at `AGENT_APP_RESTART_BACKOFF_MAX_SECONDS`, at most `AGENT_APP_RESTART_MAX` times within `AGENT_APP_RESTART_WINDOW_SECONDS`; a restarted child that does not become ready SHALL count as another death. A child that exits before it is ready SHALL NOT be restarted. While it restarts, `/ping` SHALL answer `200` `HealthyBusy` and `/invocations` and forwarded routes SHALL answer `503` with `Retry-After` and the error `app_restarting`. Past the budget, `/ping` SHALL answer `503` with the status `Unhealthy`, invocations SHALL answer `503` with the error `app_crashloop`, and the failed marker SHALL carry `app_crashloop` followed by the end of the app's stderr, redacted of environment values and credentials. The restart values SHALL be overridable in the driver options (`restart_max`, `restart_window_seconds`, `restart_backoff_seconds`, `restart_backoff_max_seconds`).

#### Scenario: a crashed app is served again
- **WHEN** the app process dies after it was ready
- **THEN** `/ping` is `HealthyBusy` and invocations get `503` `app_restarting` until the app is ready again, and then invocations succeed

#### Scenario: the budget runs out
- **WHEN** the app dies more than `restart_max` times inside the window
- **THEN** `/ping` is `503` `Unhealthy` and the failed marker's first line is `app_crashloop`

#### Scenario: a bad startup is not retried
- **WHEN** the app exits before it answers its health route
- **THEN** the start fails with `app_exited` and the child is not restarted

### Requirement: An HttpApp with the json protocol is invoked and reachable
`HttpApp` SHALL accept `protocol="json"`, `request_field`, `response_field` and `port_env`. With `json` the `http-app` driver SHALL post `{request_field: prompt, "session_id": id}` (`request_field` defaults to `prompt`) to `invoke_path` and take the text from `response_field` (dotted for nested fields), failing with `protocol_error` `invalid_response` when it is missing, or, when no `response_field` is set, from the first string among `answer`, `output`, `text`, `response`, `result` and `content`. The host SHALL forward every other path and method unchanged to the app, with the same authentication and body limit as `raw`. `hides_own_routes` SHALL be `false` for `json`. An explicit `HttpApp` SHALL keep `agentcore` as its default protocol. With `port_env` the host SHALL set that variable to `port` in the app's environment. `request_field` and `response_field` SHALL be rejected with another protocol.

#### Scenario: invoke and reach the app's own routes
- **WHEN** a `json` app answers `POST /chat` and `GET /health` and is deployed
- **THEN** `invoke()` returns its `answer` and `GET /health` through the deployment URL returns the app's own response

#### Scenario: agentcore does not forward
- **WHEN** the protocol is `agentcore` or `openai-chat`
- **THEN** a path other than `/ping` and `/invocations` is `404`

### Requirement: A failed startup says why, bounded and without secrets
When the host cannot start, it SHALL write the failed marker with the failure code on the first line (the `detail_code` of an `AgentFailure` such as `app_exited` or `app_not_ready`, or the class name of any other exception) and, if there is one, the diagnostics on the following lines. For an `http-app` the diagnostics SHALL be the end of the app's stderr; for any other exception, its message. The host SHALL remove control characters, every environment value of at least 8 characters except those of structural variables (`PATH`, `PYTHONPATH`, `HOME` and the like), credential patterns and long token-like runs, and SHALL bound the result to `AGENT_STARTUP_DIAGNOSTICS_MAX_BYTES` (2048) before writing it. `deploy()` SHALL raise `SandboxException` whose message is the code and, when present, the diagnostics, bounded again by the SDK. The host SHALL NOT log the diagnostics. A marker with only a code (an older host) SHALL read as before.

#### Scenario: the app exits at startup
- **WHEN** the app prints a traceback to stderr and exits
- **THEN** the marker's first line is `app_exited`, the rest is the end of the traceback and `deploy()` raises `SandboxException` containing it

#### Scenario: a secret in the output
- **WHEN** the app prints the value of an environment variable named like a key, or `token=<value>`, to stderr before failing
- **THEN** neither value appears in the marker nor in the exception

#### Scenario: a noisy app
- **WHEN** the app writes 400 lines to stderr before failing
- **THEN** the diagnostics are at most 2048 bytes and end with the last line

### Requirement: Usage the agent does not report is unknown, not zero
`Done`, `AgentResult` and the `POST /invocations` JSON SHALL carry `usage_reported` (`usageReported` in TypeScript). The host SHALL set it to `false` when no `step_finished` event of the run carried a `usage` object, and the decoders SHALL read an absent field as `true`. The `http-app` driver SHALL populate `usage` from the app's response when it carries `usage` with `input`/`output`, `input_tokens`/`output_tokens` or `prompt_tokens`/`completion_tokens` (and the cache and reasoning spellings), and SHALL emit `step_finished` without a `usage` object otherwise; the same applies to a `callable` that returns no usage and to a `command` with `protocol="text"`. `usage` SHALL stay a `TokenUsage` of zeros when unreported. The CLI SHALL print `tokens=desconocido` and, with `--json`, `usageReported: false`.

#### Scenario: an app that reports usage
- **WHEN** the app answers `{"text": "hola", "usage": {"input_tokens": 12, "output_tokens": 5}}`
- **THEN** the result has `usage.input == 12`, `usage.output == 5` and `usage_reported` true

#### Scenario: an app that reports nothing
- **WHEN** the app answers `{"text": "hola"}`
- **THEN** the result has zero usage and `usage_reported` false

#### Scenario: an older service
- **WHEN** a client reads a response without `usage_reported`
- **THEN** `usage_reported` is true

### Requirement: The json protocol translates invocations and forwards the rest
With protocol `json` the host SHALL serve `GET /ping` and `POST /invocations`, translate an invocation into a POST on the app's `invoke_path` with `{request_field: prompt, "session_id": id}`, read the answer from `response_field` or, when it is unset, from the first string field among the documented candidates, and forward every other path unchanged to the app.

#### Scenario: invoke and own route
- **WHEN** a client invokes through `/invocations` and then calls the app's own `/health` through the same URL
- **THEN** the invocation returns the app's text and `/health` returns the app's own response

### Requirement: The host holds a bounded queue
With `queue_max > 0` the host SHALL hold up to `queue_max` invocations above `max_concurrency`, in order, for at most `queue_timeout_seconds`, and answer 429 with `Retry-After` for an invocation that finds the queue full or waits longer. With `queue_max == 0` it SHALL answer 429 at once as before.

#### Scenario: queue is full
- **WHEN** the queue holds `queue_max` invocations and one more arrives
- **THEN** it gets 429 with `Retry-After`

### Requirement: The host restarts a crashed app within a budget
For `http-app` and long-lived child drivers, the host SHALL restart a child that exits after it was ready, up to the named maximum within the named window, with exponential backoff from the named base capped at the named maximum. While restarting, `/ping` SHALL report busy and invocations SHALL get 503 with `Retry-After`. Past the budget `/ping` SHALL answer 503 and the failed marker SHALL carry `app_crashloop` and the redacted stderr tail.

#### Scenario: restart
- **WHEN** the app process is killed once
- **THEN** the host starts it again and `/ping` returns healthy

#### Scenario: crash loop
- **WHEN** the app exits more than the budget allows within the window
- **THEN** `/ping` answers 503 and the failed marker's code is `app_crashloop`

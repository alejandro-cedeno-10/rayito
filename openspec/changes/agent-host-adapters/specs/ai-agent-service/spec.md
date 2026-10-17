## ADDED Requirements

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

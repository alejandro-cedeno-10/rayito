## MODIFIED Requirements

### Requirement: rayito.agent-service/1 is the AgentCore contract
The host in service mode SHALL answer `GET /ping` with `200 {"status": "Healthy" | "HealthyBusy"}` (with `time_of_last_update` changing only when the status changes) and `POST /invocations` with a body of at most `AGENT_SERVICE_MAX_REQUEST_BYTES` holding exactly one of `prompt` (a non-empty string) or `input` (a JSON object), and optional `session_id`, `model` and `reasoning`; a body with both or neither, or with any other key, SHALL be invalid. When `session_id` is absent it SHALL use the `AGENT_SERVICE_SESSION_HEADER` header. With `Accept: text/event-stream` it SHALL stream one SSE message per v1 event, ending in `done` or `agent_failed`; otherwise it SHALL return `{"v": 1, "text", "session_id", "usage", "failed"}`, plus `output` (a JSON object) only when the run produced one, which the `done` event SHALL also carry. It SHALL return `400` for an invalid body, `413` above the size limit, `429` with `Retry-After` above `max_concurrency` and `503` before it is ready. In a container, when `RAYITO_AGENT_SERVICE_TOKEN` is set, it SHALL require `Authorization: Bearer <token>` compared in constant time; it SHALL never log the token, the prompt, the input, the output or any header value.

#### Scenario: busy agent
- **WHEN** a contract test sends a second invocation while the first is running with `max_concurrency=1`
- **THEN** the second gets `429` and `/ping` reports `HealthyBusy` until the first ends

#### Scenario: streaming
- **WHEN** a contract test posts a prompt with `Accept: text/event-stream` to a host with a fake driver
- **THEN** the response is `text/event-stream` and its last message is a `done` event with a `session_id`

#### Scenario: input or prompt
- **WHEN** the host parses every `hostCases` body of `testdata/agent-deploy/invocation-bodies.json`
- **THEN** each body is accepted or rejected with `400 invalid_request` as `expected` says

## ADDED Requirements

### Requirement: A structured graph is invoked with input and returns output
For the deepagents/langgraph driver with an `entrypoint`, the host SHALL introspect the loaded graph with `get_input_jsonschema()` and treat it as a message graph when the schema has `properties.messages` or when introspection fails; a message graph called with `prompt` SHALL run with exactly the `stream` arguments used before this change. A `prompt` sent to a structured graph whose schema has a non-empty `required` list SHALL fail with `protocol_error` / `input_required`; a `prompt` sent to a structured graph without `required` keys SHALL run on the message path with exactly the `stream` arguments used before this change, as 0.15 did. An `input` sent to a structured graph SHALL be checked with the standard library (object, `required` keys, no extra keys when `additionalProperties` is false, top-level types) and fail with `invalid_input` otherwise (a pydantic `ValidationError` raised by the graph, classified by module and class, likewise); the run SHALL pass `configurable.thread_id` equal to the session id and SHALL return the last state filtered to `get_output_jsonschema()["properties"]` as `output`, failing with `output_not_serializable` when it cannot be encoded or exceeds `agentServiceMaxOutputBytes`. An `input` sent to a message graph SHALL be passed as is. The Node bridge SHALL do the same when `getInputJsonSchema` exists. Neither `input` nor `output` SHALL appear in logs or errors.

#### Scenario: ticket triage graph
- **WHEN** a deployment of the sample `langgraph-structured` graph `triage` is invoked with `input={"ticket_id": "T-1"}`
- **THEN** the result's `output` has `priority` and `summary` and its `text` is empty

#### Scenario: prompt to a structured graph
- **WHEN** the same deployment is invoked with the prompt `hola`
- **THEN** the SDK raises `AgentException` with reason `protocol_error` and `detail_code` `input_required`

#### Scenario: prompt to a graph without required input
- **WHEN** a graph whose input schema has `properties` without `messages` and no `required` keys (a `TypedDict` with `total=False`) is invoked with the prompt `hola`
- **THEN** the host runs it on the message path with `{"messages": [...]}`, exactly as before this change

### Requirement: The SDKs invoke with input and read output
`invoke` and `stream` of `AgentDeployment`, `AgentFleet`, `AgentCoreFleet` and their async twins SHALL take `prompt` as optional and a keyword-only `input` (TypeScript: an overload `invoke({ input }, options?)`), require exactly one of them, require `input` to be a JSON object that fits `AGENT_SERVICE_MAX_REQUEST_BYTES`, and build the body of `testdata/agent-deploy/invocation-bodies.json` `cases`, raising `InvalidArgumentException` / `InvalidArgumentError` with its messages otherwise and before any call. `AgentResult.output` and `Done.output` SHALL default to `None` / `undefined`. A `400 invalid_request` answered to a request that carried `input` SHALL raise the `host_too_old` message. Every existing call SHALL keep its behaviour.

#### Scenario: shared bodies
- **WHEN** both SDKs evaluate every case of `cases` and `resultCases`
- **THEN** the bodies, messages and parsed `output` equal `expected`

### Requirement: The host loads Secrets Manager values into its environment before the agent
When `RAYITO_SECRET_ENV` is set, the host SHALL, before creating the driver, remove it from its environment, run the bundled helper with the spec on stdin within `agentSecretEnvHelperTimeoutSeconds`, expand the returned values with the algorithm of `testdata/agent-secret-env/expand.json`, refuse reserved names (`reserved.json` and the runtime's own variables), export the variables and log only `secret_env_loaded count=N`. On any failure it SHALL fail closed with `secret_env_unavailable`, `secret_env_denied`, `secret_env_invalid`, `secret_env_invalid_key` or `secret_env_reserved`, never report Healthy, and never write a secret name or value. Startup diagnostics SHALL also redact injected values shorter than the default redaction minimum, down to `agentSecretEnvRedactMinChars`. On MicroVM the SDK SHALL resolve the same values with the caller's credentials and pass them to the host process as process secrets, for single deployments, fleets and pool slots. Without secret env, the host, the export and the image SHALL be unchanged.

#### Scenario: order and fail closed
- **WHEN** a host test uses a fake helper that denies one secret
- **THEN** the driver is never created, the startup failure carries `secret_env_denied`, and neither the secret name nor any value appears in logs, diagnostics or the failure

#### Scenario: zero cost without the feature
- **WHEN** a project without secret env is exported and deployed with fakes
- **THEN** no Secrets Manager client is created and the export is byte-identical to before this change

### Requirement: The HTTP app can speak the LangGraph Platform API
`HttpApp(protocol="langgraph-api", assistant_id=...)` SHALL be valid only with `assistant_id`, and `assistant_id` SHALL be refused with any other protocol. The host SHALL check health on `agentLanggraphHealthPath`, wait up to `agentLanggraphReadyWaitSeconds`, and translate each invocation into `POST` on `agentLanggraphInvokePath` for the thread `uuid5(agentLanggraphThreadNamespace, session_id)` (a new session gets an `app_` id first) with the body and reading rules of `testdata/agent-langgraph-api/`: a prompt becomes one user message, `input` goes as is, `if_not_exists` is `create`, `multitask_strategy` is `reject`, the text comes from `response_field` or the last message, a run with `input` whose final state has no `messages` SHALL return that state as `output` (failing with `output_not_serializable` when it is not strict JSON or exceeds `agentServiceMaxOutputBytes`) with no text, a run with `input` whose final state has `messages` SHALL return only the text of the last message (no `output`), the JSON text of a state without messages SHALL be strict (`invalid_response` otherwise), usage from its `usage_metadata` (unknown otherwise), `__interrupt__` is `protocol_error` / `graph_interrupted`, and other statuses keep `app_http_<n>`. The child process SHALL get `LANGGRAPH_CLI_NO_ANALYTICS=1` and `LANGSMITH_TRACING=false` unless the project sets them. Only `/ping` and `/invocations` SHALL be exposed. Every other protocol SHALL behave as before.

#### Scenario: same session, same thread
- **WHEN** a host test with a fake Agent Server invokes twice with the same `session_id`
- **THEN** both requests go to the same thread path of `requests.json` and carry the body of `bodyCases`

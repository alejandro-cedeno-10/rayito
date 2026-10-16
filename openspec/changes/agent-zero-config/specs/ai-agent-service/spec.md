## ADDED Requirements

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

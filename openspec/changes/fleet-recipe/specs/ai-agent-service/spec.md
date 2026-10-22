## ADDED Requirements

### Requirement: The agent host keeps HTTP client loggers quiet
The agent host SHALL set the `httpx` and `httpcore` loggers (`QUIET_HTTP_LOGGERS`) to `WARNING` when it starts serving, so that request lines with URLs that may carry credentials are not written to the agent logs at INFO. The documentation SHALL warn about HTTP client loggers in agent code.

#### Scenario: agent code logs an httpx request
- **WHEN** agent code performs an `httpx` request to a URL with a query token while the host serves
- **THEN** no line of the host's log contains that URL

### Requirement: A malformed request does not break the request logger
The host's request logger SHALL tolerate requests for which the HTTP server never parsed a method or path. A malformed request line SHALL get a `400` with one log line and no traceback, and later requests SHALL be served normally.

#### Scenario: malformed request line
- **WHEN** a client sends `GARBAGE\r\n\r\n` to the host port
- **THEN** the host answers 400, the log has no `AttributeError`, and a following `GET /ping` succeeds

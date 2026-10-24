## ADDED Requirements

### Requirement: A request whose connection never opened is repeated
When a unary RPC or the opening of a stream fails with `UNAVAILABLE` because the HTTP/2 connection to the proxy never opened (Python: `before receiving SETTINGS frame` or `failed to connect to all addresses` in the details; TypeScript: a Node error with `syscall: "connect"` in the cause chain), both SDKs, sync and async, SHALL repeat it up to three times, waiting 0.25, 0.5 and 1 s, before applying the usual handling. This SHALL apply to every RPC, also `Execute`, because the request never left the client. Any other error SHALL keep its handling and SHALL NOT be repeated by this rule.

#### Scenario: the first RPC after create
- **WHEN** opening a stream fails once with `UNAVAILABLE ... before receiving SETTINGS frame` and then succeeds
- **THEN** the caller gets the stream and no error

#### Scenario: a proxy 502 is not this case
- **WHEN** a unary RPC fails with `UNAVAILABLE` and a proxy 502
- **THEN** it is not repeated by this rule

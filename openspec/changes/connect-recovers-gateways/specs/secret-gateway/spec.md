## ADDED Requirements

### Requirement: connect() recovers sbx.gateways from the running sandbox without any secret material

`Sandbox.connect()`, `AsyncSandbox.connect()` and TypeScript `Sandbox.connect()`
(static and instance) SHALL, after readiness and when the agent advertises
`configure` and `secret_gateway` and the handle holds no gateway handle of
its own (a handle recovered by an earlier `connect()` does not count), read
`ConfigureStatus` once, with the request timeout of that `connect()` call,
and rebuild `sbx.gateways` from its
`SecretGatewayStatus` (route name, port, last error class). No upstream,
header name or header value SHALL be read or returned. The recovered
handle's `refresh()` SHALL only re-read `ConfigureStatus` and send no
`Configure`.

#### Scenario: a second process sees the gateways

- **WHEN** a process calls `connect()` on a sandbox whose `create()` applied `gateways={"bedrock": ...}`
- **THEN** `sbx.gateways["bedrock"].url` is `http://127.0.0.1:<port>` with the port `rayd` reports, and no `Configure` is sent

#### Scenario: the creating handle keeps its own gateways

- **WHEN** the handle that applied `gateways=` calls `connect()` again
- **THEN** its gateway handle is kept and no `ConfigureStatus` is read for recovery

#### Scenario: a recovered handle re-reads on each connect

- **WHEN** a handle whose `sbx.gateways` was recovered calls `connect(request_timeout=...)` again
- **THEN** `ConfigureStatus` is read again with that request timeout and `sbx.gateways` reflects the new ports

#### Scenario: an agent without the feature

- **WHEN** `Health.features` is absent or lacks `secret_gateway`
- **THEN** `sbx.gateways` stays empty and no `ConfigureStatus` is called

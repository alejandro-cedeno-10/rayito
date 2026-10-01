## ADDED Requirements

### Requirement: gateways is off by default and resolves no secret and opens no listener without it
No SDK call SHALL build a `secretsmanager`/Secrets Manager client for the gateway feature, resolve any header value, or populate `ConfigureRequest.secret_gateway`/`secretGateway` unless the caller sets `gateways=`/`gateways` to a non-empty mapping. `rayd` SHALL NOT open a loopback listener for this feature unless a `Configure` call carries a non-empty `SecretGatewayConfig`.

#### Scenario: gateways=None matches the 0.5.x golden trace
- **WHEN** a sandbox is created with `gateways` absent (together with the other six 0.6 options)
- **THEN** the boto3/AWS-SDK-v3 call sequence and the gRPC method sequence equal the 0.5.x zero-cost trace fixture exactly, and no `secretsmanager` client exists

#### Scenario: a fresh agent build runs no listener before any Configure call
- **WHEN** `rayd`'s `secret_gateway` feature slot is built and no `Configure` call has been made yet
- **THEN** `ConfigureStatus.secret_gateway.routes` is empty and no loopback port is bound

### Requirement: a SecretGateway route validates its shape before any AWS call or RPC
Constructing a `SecretGateway`/route entry SHALL raise `InvalidArgumentException`/`InvalidArgumentError` — before resolving any header value or building any request — when: `upstream` is not `https://host` with no path, query or fragment; `headers` is empty or exceeds 16 entries; `allow` is empty, exceeds 32 entries, or contains a non-uppercase method or a non-absolute path; or `rate_per_minute`/`ratePerMinute` is neither `0` nor within `1..6000`. A `gateways=`/`gateways` mapping SHALL raise the same way when empty, when it exceeds 8 entries, when a key is not `1-64` lowercase `[a-z0-9-]`, or when a value is not a `SecretGateway`.

#### Scenario: an invalid upstream is rejected before any resolution
- **WHEN** `SecretGateway(upstream="http://example.com", ...)` is constructed
- **THEN** `InvalidArgumentException`/`InvalidArgumentError` is raised and no header is resolved

### Requirement: SecretGatewayConfig replaces the gateway's whole state and SecretGatewayStatus reports each route's real port
A present `SecretGatewayConfig` (even with an empty `routes` list) SHALL become the feature's complete new state: every route not named in the new config SHALL stop, and every route named SHALL be (re)started with its new configuration. `SecretGatewayStatus.routes` SHALL report, for each currently running route, its real bound loopback port and the lowercase-snake `error_class` of its own most recent forwarding failure, if any.

#### Scenario: an empty SecretGatewayConfig tears every route down
- **WHEN** a `Configure` call carrying an empty `SecretGatewayConfig` is sent after a previous call configured one or more routes
- **THEN** `ConfigureStatus.secret_gateway.routes` is empty

#### Scenario: an invalid new config leaves the previous one running
- **WHEN** a `Configure` call's `SecretGatewayConfig` fails validation (for example a non-`https` upstream)
- **THEN** the section result is `INVALID`, and every route from the previous successful `Configure` call keeps running unchanged

### Requirement: a route forwards only allowed requests, within its rate limit, with the vaulted header injected and never readable by the sandbox
A loopback route's listener SHALL forward a request to its upstream only when the request's method and path match one of the route's `allow` entries (exact match, or a `/*`-suffixed entry matching any path under that prefix) and the route's per-minute token bucket has a token available; otherwise it SHALL respond without forwarding (403 for a disallowed method/path, 429 for the rate limit) and never open a connection to the upstream. Before forwarding, the listener SHALL strip from the inbound request every header name the route also injects (and every hop-by-hop header and `Host`), then inject each of the route's own header values.

#### Scenario: a disallowed method is rejected without touching the upstream
- **WHEN** a request using a method/path pair absent from the route's `allow` list reaches its listener
- **THEN** the listener responds without any connection to the upstream, and the route's rate-limit bucket is unchanged

#### Scenario: an inbound header matching an injected name is stripped before forwarding
- **WHEN** the sandbox's own request already sets a header name the route also injects
- **THEN** the upstream receives only the route's own vaulted value for that header name, never the sandbox-supplied one

### Requirement: gateways= requires a 0.6.0 image with the secret_gateway feature
Setting `gateways=`/`gateways` against an agent reporting no `Health.features` field at all SHALL raise `UnimplementedError`/`UnimplementedError` naming the required 0.6.0 image, before sending any `Configure` call. Setting it against an agent whose `Health.features.secret_gateway` flag is `false` SHALL raise the same way, naming the feature.

#### Scenario: a pre-0.6 agent raises before any Configure call
- **WHEN** `gateways=` is set and the agent's `Health` response has no `features` field
- **THEN** `UnimplementedError` is raised naming "necesita una imagen 0.6.0 o posterior", and `Configure` is never called

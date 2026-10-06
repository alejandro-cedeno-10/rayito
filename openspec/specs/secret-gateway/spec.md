# secret-gateway Specification

## Purpose
TBD - created by archiving change m15-secrets-gateway. Update Purpose after archive.

## Requirements

### Requirement: gateways is off by default and resolves no secret and opens no listener without it
No SDK call SHALL build a `secretsmanager`/Secrets Manager client for the gateway feature, resolve any header value, or populate `ConfigureRequest.secret_gateway`/`secretGateway` unless the caller sets `gateways=`/`gateways` to a non-empty mapping. `rayd` SHALL NOT open a loopback listener for this feature unless a `Configure` call carries a non-empty `SecretGatewayConfig`.

#### Scenario: gateways=None matches the 0.5.x golden trace
- **WHEN** a sandbox is created with `gateways` absent (together with the other six 0.6 options)
- **THEN** the boto3/AWS-SDK-v3 call sequence and the gRPC method sequence equal the 0.5.x zero-cost trace fixture exactly, and no `secretsmanager` client exists

#### Scenario: a fresh agent build runs no listener before any Configure call
- **WHEN** `rayd`'s `secret_gateway` feature slot is built and no `Configure` call has been made yet
- **THEN** `ConfigureStatus.secret_gateway.routes` is empty and no loopback port is bound

### Requirement: a SecretGateway route validates its shape before any AWS call or RPC
Constructing a `SecretGateway`/route entry SHALL raise `InvalidArgumentException`/`InvalidArgumentError` — before resolving any header value or building any request — when: `upstream` is not `https://host` with no path, query, userinfo or fragment; `headers` is empty or exceeds 16 entries, or a header name is not an RFC 9110 token, is a hop-by-hop or framing header (`host`, `content-length`, `transfer-encoding`, `connection`, `keep-alive`, `proxy-authenticate`, `proxy-authorization`, `te`, `trailer`, `trailers`, `upgrade`), or equals another of the route's header names ignoring case; `allow` is empty, exceeds 32 entries, or contains a non-uppercase method or a non-absolute path; or `rate_per_minute`/`ratePerMinute` is neither `0` nor within `1..6000`. A `gateways=`/`gateways` mapping SHALL raise the same way when empty, when it exceeds 8 entries, when a key is not `1-64` lowercase `[a-z0-9-]`, or when a value is not a `SecretGateway`.

#### Scenario: an invalid upstream is rejected before any resolution
- **WHEN** `SecretGateway(upstream="http://example.com", ...)` is constructed
- **THEN** `InvalidArgumentException`/`InvalidArgumentError` is raised and no header is resolved

#### Scenario: rayd and both SDKs agree on every header name
- **WHEN** each name in `testdata/secret-gateway/header-names.json` is used as a route's header name in Python, TypeScript and `rayd`'s `GatewaySpec::parse`
- **THEN** every `valid` name is accepted by all three, and every `invalid` name and every `duplicates` pair is rejected by all three (`rayd`: `INVALID` with `invalid_header_name` or `duplicate_header_name`)

#### Scenario: rayd re-validates what a client that bypasses the SDK sends
- **WHEN** a `Configure` call reaches `rayd` with a route name outside `[a-z0-9-]`, or an upstream with userinfo or a fragment
- **THEN** the section result is `INVALID` with `invalid_route_name` or `invalid_upstream_host`, and the previous routes keep running unchanged

### Requirement: SecretGatewayConfig replaces the gateway's whole state, keeps a still-present route's port, and SecretGatewayStatus reports each route's real port
A present `SecretGatewayConfig` (even with an empty `routes` list) SHALL become the feature's complete new state. A route whose name was also in the previous config SHALL keep its listener and its loopback port, and its headers, allow rules and rate limit SHALL be replaced in place so that the next request on any connection, including a keep-alive connection opened before the call, is judged and forwarded with the new state. A route not named in the new config SHALL stop accepting connections, close its idle keep-alive connections and let at most an in-flight request finish. A route named for the first time SHALL get a new listener. `SecretGatewayStatus.routes` SHALL report, for each currently running route, its real bound loopback port and the lowercase-snake `error_class` of its own most recent forwarding failure, if any.

#### Scenario: an empty SecretGatewayConfig tears every route down
- **WHEN** a `Configure` call carrying an empty `SecretGatewayConfig` is sent after a previous call configured one or more routes
- **THEN** `ConfigureStatus.secret_gateway.routes` is empty

#### Scenario: a removed route closes an idle keep-alive connection
- **WHEN** a client holds an idle keep-alive connection to a route and a `Configure` call no longer names that route
- **THEN** by the time the call returns, the connection is closed by `rayd` and no further request is forwarded on it

#### Scenario: the same route name keeps its port across a reapply
- **WHEN** a `Configure` call names a route already running, with a different header value
- **THEN** `ConfigureStatus` reports the same port for that route as before the call

#### Scenario: a reapply reaches an already-open keep-alive connection
- **WHEN** a client holds a keep-alive connection to a route and a `Configure` call keeps the route's name but changes its `allow` list
- **THEN** the client's next request on that same connection is decided by the new `allow` list

#### Scenario: an invalid new config leaves the previous one running
- **WHEN** a `Configure` call's `SecretGatewayConfig` fails validation (for example a non-`https` upstream)
- **THEN** the section result is `INVALID`, and every route from the previous successful `Configure` call keeps running unchanged

### Requirement: a route forwards only allowed requests, within its rate limit, with the vaulted header injected and never readable by the sandbox
A loopback route's listener SHALL refuse (403 `not_allowed`, without forwarding) any request whose path contains a dot-segment (`.` or `..`), a percent-encoded `.`, `/` or `\`, a backslash, or an empty segment, before consulting `allow`; it SHALL never normalise a path, so a path it forwards is exactly the one `allow` matched. It SHALL forward a request to its upstream only when the request's method and path match one of the route's `allow` entries (exact match, or a `/*`-suffixed entry matching any path under that prefix) and the route's per-minute token bucket has a token available; otherwise it SHALL respond without forwarding (403 for a disallowed method/path, 429 for the rate limit) and never open a connection to the upstream. Before forwarding, the listener SHALL strip from the inbound request every header name the route also injects (and every hop-by-hop header and `Host`), then inject each of the route's own header values. The upstream's status and body SHALL reach the sandbox unchanged (streamed); from its response headers the listener SHALL drop every hop-by-hop header, every header name the route injects, and every header whose value contains one of the route's vaulted values of at least `MIN_REFLECTED_VALUE_LEN` (8) bytes, shorter vaulted values being matched by name only. Because the body is not inspected, the documented guarantee SHALL be that the sandbox cannot read a vaulted value unless an allowed upstream endpoint reflects it.

#### Scenario: a disallowed method is rejected without touching the upstream
- **WHEN** a request using a method/path pair absent from the route's `allow` list reaches its listener
- **THEN** the listener responds without any connection to the upstream, and the route's rate-limit bucket is unchanged

#### Scenario: a dot-segment cannot walk around a wildcard rule
- **WHEN** a route allows `("POST", "/v1/*")` and the sandbox sends `/v1/../admin`, `/v1/%2e%2e/admin` or `/v1/..%2fadmin` (any `unsafe` entry of `testdata/secret-gateway/request-paths.json`)
- **THEN** the listener responds 403 without any connection to the upstream

#### Scenario: an upstream failure is never reported as a timeout
- **WHEN** the upstream accepts the connection and then fails the request (a TLS error, a reset, a malformed response)
- **THEN** the listener responds 502 and the route's `last_error_class` is `upstream_error`; only an elapsed connect timeout or `RESPONSE_HEAD_TIMEOUT` (the wait for the response status and headers; a streamed body is never bounded) yields 504 `upstream_timeout`

#### Scenario: an inbound header matching an injected name is stripped before forwarding
- **WHEN** the sandbox's own request already sets a header name the route also injects
- **THEN** the upstream receives only the route's own vaulted value for that header name, never the sandbox-supplied one

#### Scenario: an upstream echoing the credential in a response header does not hand it back
- **WHEN** the upstream's response carries the injected header name, or another header whose value contains the vaulted value
- **THEN** neither header reaches the sandbox, and the other response headers (`content-type`, a request id) do

### Requirement: gateways= requires a 0.6.0 image with the secret_gateway feature
Setting `gateways=`/`gateways` against an agent reporting no `Health.features` field at all SHALL raise `UnimplementedError`/`UnimplementedError` naming the required 0.6.0 image, before sending any `Configure` call. Setting it against an agent whose `Health.features.secret_gateway` flag is `false` SHALL raise the same way, naming the feature.

#### Scenario: a pre-0.6 agent raises before any Configure call
- **WHEN** `gateways=` is set and the agent's `Health` response has no `features` field
- **THEN** `UnimplementedError` is raised naming "necesita una imagen 0.6.0 o posterior", and `Configure` is never called

#### Scenario: a create that cannot configure the gateway terminates the MicroVM
- **WHEN** `create(gateways=...)` fails after `run-microvm` for any reason (a pre-0.6 agent, the flag `false`, a missing secret, a failed `Configure` call, a section result other than `APPLIED`)
- **THEN** the client is closed and `TerminateMicrovm` is called before the error is raised, unless `keep_on_failure`/`keepOnFailure` is set

#### Scenario: Health advertises the gateway only when the slot is real
- **WHEN** `rayd` starts and its `secret_gateway` slot degrades to `Unsupported` (no readable TLS trust store)
- **THEN** `Health.features.secret_gateway` is `false`, so the SDK refuses `gateways=` before sending any `Configure` call

### Requirement: refresh() rotates the vaulted values, re-reading them from Secrets Manager, and raises on any non-applied result
`sbx.gateways.refresh()`/`arefresh()` SHALL discard the `SecretCache` entries of every secret the gateway injects, resolve each header again from Secrets Manager, send a new `Configure` call carrying only the gateway section, and raise the same exception `create()` would for any section result other than `APPLIED`, without asking for `ConfigureStatus` in that case. Because route names do not change, every route SHALL keep its port across a `refresh()`.

#### Scenario: refresh pushes the value updated inside the cache TTL
- **WHEN** a secret a gateway injects is updated in Secrets Manager and `refresh()` is called before `SecretCache`'s TTL elapses
- **THEN** the `ConfigureRequest` sent by `refresh()` carries the new value

#### Scenario: refresh raises on a failed section
- **WHEN** `refresh()`'s `Configure` call returns the gateway section as `FAILED` or `INVALID`
- **THEN** `refresh()` raises with that section's `error_class`, and `ConfigureStatus` is not called

### Requirement: SandboxPool.take accepts gateways
`SandboxPool.take(gateways=)`/`AsyncSandboxPool.take(gateways=)`/`pool.take({ gateways })` SHALL validate `gateways` before claiming any slot and, once the sandbox is taken, apply it exactly as `create(gateways=)` does; warm slots SHALL never carry a gateway. A failure to apply it SHALL terminate the taken slot before the error is raised.

#### Scenario: an invalid gateways mapping claims no slot
- **WHEN** `take(gateways=...)` is called with a malformed mapping
- **THEN** `InvalidArgumentException`/`InvalidArgumentError` is raised and no control-plane call is made

#### Scenario: a pre-0.6 slot is terminated
- **WHEN** `take(gateways=...)` takes a slot whose agent reports no `Health.features`
- **THEN** `UnimplementedError` is raised and the slot is terminated

### Requirement: a route accepts request paths from an allowlist only
Before consulting `allow`, a route's listener SHALL refuse (403
`not_allowed`, without forwarding) any request path that is not absolute,
holds a raw byte other than an RFC 3986 path character (unreserved, `:`,
`@`, the sub-delims except `;`, or the `%` of a well-formed `%XX`), holds an
empty segment other than the last, or has a segment that, percent-decoded
exactly once, is not valid UTF-8, holds a control byte, `/`, `\`, `%` or `;`,
or equals `.` or `..`. The forwarded path SHALL remain the raw path,
unnormalised.

#### Scenario: path parameters and double encoding cannot leave a wildcard rule
- **WHEN** a route allows `("GET", "/v1/*")` and the sandbox sends
  `/v1/..;/admin`, `/v1/x;jsessionid=1`, `/v1/%252e%252e/admin` or
  `/v1/%c0%ae%c0%ae/admin` (any `unsafe` entry of
  `testdata/secret-gateway/request-paths.json`)
- **THEN** the listener responds 403 without any connection to the upstream

#### Scenario: a single encoded character is still forwarded
- **WHEN** the same route receives `/v1/files/a%20b`
- **THEN** the request reaches the allowlist check and is forwarded with the
  path unchanged

### Requirement: an allow path no request could match is refused before any listener changes
`rayd` SHALL refuse a `SecretGatewayConfig` whose `allow` entry has an
absolute path that fails the request-path allowlist, with
`SECTION_CODE_INVALID` and `error_class` `invalid_allow_path`, and both SDKs
SHALL raise `InvalidArgumentException`/`InvalidArgumentError` for it before
resolving any header value or making any RPC.

#### Scenario: rayd and both SDKs agree on every allow path
- **WHEN** each path in `testdata/secret-gateway/request-paths.json` is used
  as an `allow` path in Python, TypeScript and `rayd`'s `GatewaySpec::parse`
- **THEN** every `safe` path is accepted by all three, and every `unsafe`
  path is refused by all three (`rayd`: `empty_path` for a relative or empty
  one, `invalid_allow_path` otherwise)

## MODIFIED Requirements

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

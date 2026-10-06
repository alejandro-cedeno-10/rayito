## ADDED Requirements

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

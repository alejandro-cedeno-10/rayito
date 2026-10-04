## ADDED Requirements

### Requirement: The Host rewrite guarantee names its scope
`SECURITY.md` T17, the network page and the `NAME_RULE_PORTS` documentation
SHALL state that the local proxy rewrites `Host` only for absolute-form
`http://` requests, and that a `CONNECT` or SOCKS5 tunnel to a name-allowed
target on port 80 or 443 carries whatever `Host` header or SNI the client
sends toward that name's addresses (the shared-IP residual). The name
decision SHALL be the same whatever the request form.

#### Scenario: an allowed name is decided the same way on both ports
- **WHEN** the policy allows `api.example.test` under deny-by-default and a
  target names it on port 80 or 443
- **THEN** the decision is `ResolveByName("api.example.test")` on both ports
  and a deny on any other port

## ADDED Requirements

### Requirement: SECURITY.md and security.md document the M12 local proxy's JWE scope
`SECURITY.md` threats T2 (hook-port integrity) and T3 (proxy JWE) SHALL each gain an M12 appendix stating that `rayito sandbox proxy`:

- always mints its JWE scoped to a single port (`PortSpec.single(N)`, never `allPorts`) and never for port 9000 (the lifecycle-hooks port), rejecting that port before any AWS call;
- binds to loopback (`127.0.0.1`/`::1`/`localhost`) by default, and requires `--allow-remote` for any other bind address;
- strips any `x-aws-proxy-*` header a client sends before forwarding the request;
- never logs, prints or otherwise records the JWE, headers, bodies or paths of what passes through it.

The appendix SHALL name the residual risk with no new mitigation and no new threat number: while the proxy runs, any local process that reaches the forwarded port has the same access to the sandbox as the operator who started it (and, if `--allow-remote` was used, so does any host that can reach the bound address).

`docs/site/docs/security.md` SHALL carry the matching prose, cross-linking `cli.md`'s `proxy` command documentation.

#### Scenario: SECURITY.md names the port-9000 exclusion and the residual risk
- **WHEN** a reviewer reads the T2 and T3 rows of `SECURITY.md`
- **THEN** T2 states that `rayito sandbox proxy` never mints a token for port 9000, and T3 states the single-port JWE scope, the loopback default, the `x-aws-proxy-*` stripping, and the residual local-process risk

#### Scenario: the docs site names the same facts
- **WHEN** a reader opens `docs/site/docs/security.md`
- **THEN** it names the single-port JWE scope, the loopback default requiring `--allow-remote` otherwise, and that the proxy never logs the JWE

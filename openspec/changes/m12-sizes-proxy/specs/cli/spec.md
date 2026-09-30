## ADDED Requirements

### Requirement: rayito sandbox proxy exposes a guest port over a local TCP proxy
The CLI SHALL add `rayito sandbox proxy ID --port N [--local-port M] [--bind ADDR] [--allow-remote]` to the `sandbox` group, implemented in `rayito/cli/_proxy.py` (pure helpers plus an `asyncio` server) with no dependency beyond the standard library and the existing SDK transport (`rayito._transport.TokenRefresher`/`TokenStore`, `rayito._aws.PortSpec`).

It SHALL call `get-microvm` to resolve the sandbox's endpoint and SHALL exit with an error, calling no other AWS API, when the sandbox is already `TERMINATING` or `TERMINATED`.

**Port.** `--port N` SHALL be validated before any AWS call: `N == 9000` (the lifecycle-hooks port, ADR-006) or `N` outside `1..65535` SHALL raise without minting a token or resolving the endpoint. `--local-port` SHALL default to `--port`. The JWE SHALL always be minted for `PortSpec.single(N)`, never `allPorts`.

**Token lifecycle.** The command SHALL reuse the SDK's own `TokenRefresher`: mint once at startup, start the background refresh thread (45-minute cadence, the same `TOKEN_REFRESH_AFTER_MINUTES` the gRPC transport uses), and read the current JWE from the `TokenStore` on every connection without minting a new one per request.

**Listener.** It SHALL bind `--bind` (default `127.0.0.1`) and `--local-port`, speak HTTP/1.1, and per connection: read only the request header, capped at the `asyncio.StreamReader` default limit (64 KiB); strip any client-supplied header whose name starts with `x-aws-proxy-` (case-insensitive); replace any `Host` header with the sandbox's endpoint; add `X-aws-proxy-auth` (the current JWE) and `X-aws-proxy-port` (`N`); force `Connection: close` unless the request is an `Upgrade` request, in which case `Connection`/`Upgrade` SHALL pass through unmodified; then open a connection to the endpoint (TLS on port 443, SNI = the endpoint, via an injectable connector factory) and pipe bytes in both directions until either side closes.

**Bind safety.** `--bind` outside `127.0.0.1`, `::1` or `localhost` SHALL require `--allow-remote`; without it, the command SHALL exit with a usage error before touching AWS. With `--allow-remote`, it SHALL print a warning that any host reaching the port uses the sandbox.

**Logging.** The command SHALL NOT log, print or otherwise record the JWE, request/response headers, bodies, or paths of anything that passes through the proxy.

It SHALL require no sandbox access token (only the JWE), and it SHALL stop the refresher thread and close the listener on Ctrl-C.

#### Scenario: port 9000 is rejected before any AWS call
- **WHEN** `rayito sandbox proxy microvm-x --port 9000` runs
- **THEN** it exits with an error naming the lifecycle-hooks port, and the fake control plane records no `GetMicrovm` and no `CreateMicrovmAuthToken`

#### Scenario: client-supplied proxy headers never reach the upstream
- **WHEN** a client connects to the local listener and sends `GET / HTTP/1.1` with a forged `X-aws-proxy-auth` header
- **THEN** the request the upstream receives carries the proxy's own current JWE in `X-aws-proxy-auth`, the forged value does not appear anywhere in it, and `X-aws-proxy-port` equals `N`

#### Scenario: reading the JWE does not mint a new one
- **WHEN** ten requests are proxied inside the 45-minute refresh window
- **THEN** `CreateMicrovmAuthToken` is called exactly once (the initial mint), and the eleventh request after the fake clock advances past the refresh window sees a newly minted JWE

#### Scenario: an Upgrade request keeps Connection open
- **WHEN** a client sends `GET /ws HTTP/1.1` with `Connection: Upgrade` and `Upgrade: websocket`
- **THEN** the upstream receives `Connection: Upgrade` and `Upgrade: websocket` unchanged, and the proxy does not append `Connection: close`

#### Scenario: a remote bind without --allow-remote is a usage error
- **WHEN** `rayito sandbox proxy microvm-x --port 8080 --bind 0.0.0.0` runs without `--allow-remote`
- **THEN** it exits with the CLI's usage exit code and no listener is started

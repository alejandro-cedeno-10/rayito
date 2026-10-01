## ADDED Requirements

### Requirement: rayito sandbox proxy exposes a guest port over a local TCP proxy
The CLI SHALL add `rayito sandbox proxy ID --port N [--local-port M] [--bind ADDR] [--allow-remote]` to the `sandbox` group, implemented in `rayito/cli/_proxy.py` (pure helpers plus an `asyncio` server) with no dependency beyond the standard library and the existing SDK transport (`rayito._transport.TokenRefresher`/`TokenStore`, `rayito._aws.PortSpec`).

It SHALL call `get-microvm` to resolve the sandbox's endpoint and SHALL exit with an error, calling no other AWS API, when the sandbox is already `TERMINATING` or `TERMINATED`.

**Port.** `--port N` SHALL be validated before any AWS call: `N == 9000` (the lifecycle-hooks port, ADR-006) or `N` outside `1..65535` SHALL raise without minting a token or resolving the endpoint. `--local-port` SHALL default to `--port`, SHALL be validated to `1..65535` before any AWS call, and the local listener socket SHALL be bound before `get-microvm`/`create-microvm-auth-token` are called, so an invalid or already-in-use `--local-port` fails without spending either call. The JWE SHALL always be minted for `PortSpec.single(N)`, never `allPorts`.

**Malformed requests.** The listener SHALL parse the client's request head as strict HTTP/1.1: a bare CR or LF outside a `\r\n` line ending, an `obs-fold` continuation line, a header name outside RFC 9110's `tchar` set, or a header line without `:` SHALL cause the connection to receive `400 Bad Request` and close, without opening a connection to the upstream and without reading the JWE. Only the first request on a connection is rewritten; bytes sent afterward on the same connection (pipelining) are forwarded unmodified through the same duplex pipe.

**Token lifecycle.** The command SHALL reuse the SDK's own `TokenRefresher`: mint once at startup, start the background refresh thread (45-minute cadence, the same `TOKEN_REFRESH_AFTER_MINUTES` the gRPC transport uses), and read the current JWE from the `TokenStore` on every connection without minting a new one per request.

**Listener.** It SHALL bind `--bind` (default `127.0.0.1`) and `--local-port`, speak HTTP/1.1, and per connection: read only the request header, capped at the `asyncio.StreamReader` default limit (64 KiB); strip any client-supplied header whose name starts with `x-aws-proxy-` (case-insensitive); replace any `Host` header with the sandbox's endpoint; add `X-aws-proxy-auth` (the current JWE) and `X-aws-proxy-port` (`N`); force `Connection: close` (dropping the client's own `Connection`) unless the request is an upgrade request — an `Upgrade` header AND the `upgrade` token in `Connection` (RFC 9110 §7.8) — in which case `Connection`/`Upgrade` SHALL pass through unmodified; the header read and the upstream connect SHALL each be bounded by a timeout; when there is no current JWE or the upstream connect fails or times out, it SHALL answer `502 Bad Gateway` with `Connection: close` and write a stderr line carrying no JWE, header or path; then open a connection to the endpoint (TLS on port 443, SNI = the endpoint, via an injectable connector factory) and pipe bytes in both directions until either side closes.

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

#### Scenario: a header with a smuggled bare LF is rejected, not forwarded
- **WHEN** a client sends a request whose header block contains a bare `\n` inside a header value (no matching `\r`), followed by text that looks like another `x-aws-proxy-*` header
- **THEN** the connection receives `400 Bad Request` and closes, the fake upstream connector is never called, and the smuggled text never reaches any header sent upstream

#### Scenario: an invalid or busy --local-port fails before any AWS call
- **WHEN** `--local-port` is outside `1..65535`, or is already bound by another process
- **THEN** the command exits with an error message naming the port (no traceback) before calling `GetMicrovm` or `CreateMicrovmAuthToken`

#### Scenario: an Upgrade header without Connection: upgrade is not an upgrade
- **WHEN** a client sends a request with `Upgrade: x` and `Connection: keep-alive`
- **THEN** the upstream receives a single `Connection: close` and no `keep-alive`

#### Scenario: no JWE or no upstream answers 502
- **WHEN** the JWE provider returns nothing, or the upstream connect raises or times out
- **THEN** the client receives `502 Bad Gateway` with `Connection: close`, and stderr gets a reason line without the JWE, headers or path

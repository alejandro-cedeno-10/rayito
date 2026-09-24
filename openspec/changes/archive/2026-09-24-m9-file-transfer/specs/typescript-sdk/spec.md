## MODIFIED Requirements

### Requirement: gRPC transport through the AWS proxy with per-request headers
The SDK SHALL reach `rayd` with `createGrpcTransport` from `@connectrpc/connect-node` (HTTP/2; the v2 API has no `httpVersion` option) using `baseUrl = "<scheme>://<endpoint>:<port>"` (`https` and 443 by default; `TransportSettings` lets tests point at `http://127.0.0.1:<port>`, and `scheme: "http"` towards any host other than `127.0.0.1`, `::1` or `localhost` SHALL be refused with `InvalidArgumentError` before any connection, because the proxy JWE and `x-access-token` would travel in clear), `pingIntervalMs 30000`, `pingTimeoutMs 10000`, `pingIdleConnection true`, `readMaxBytes 64 MiB`. An interceptor SHALL set, on every unary and streaming request, the lowercase headers `x-aws-proxy-auth` (the JWE read from the `TokenStore` at call time), `x-aws-proxy-port` (`"8080"`), `x-aws-proxy-force-h2` (`"true"`) and `x-access-token`, and SHALL throw `AuthenticationError` before sending when the store has no token for the port. A `Sandbox` SHALL own at most two transports' worth of HTTP/2 sessions: `unary` (opened at construction; `Health`, `Metrics`, every unary including `StartImport`, `StartExport`, `GetTransfer` and `CancelTransfer`, foreground `Start`, `Read`, `Write`, `Execute`, `Reattach`) and `stream` (opened lazily; background `Start`, `Connect`, `Pty.Create`, `Pty.Connect`, `WatchDir`, `WatchTransfer`). A `Write` with `gzip: true` SHALL go through a third `Transport` object created lazily with `sendCompression: compressionGzip` and `compressMinBytes: 1024` on the **unary** transport's `Http2SessionManager`, so it never opens a session of its own. Every RPC SHALL carry a `timeoutMs` deadline following the Python vocabulary (`requestTimeoutMs` 60 000 for unaries; stream deadline `timeoutMs + 5 000` for process and PTY streams, `timeoutMs + 15 000` for `Execute`, none for `0`/`undefined`; `60 000 + 1 000 per 1 000 000 bytes` for `Read`/`Write`). Every stream SHALL own an `AbortController` so `disconnect()`, `stop()` and `close()` cancel it, and a stream that ends on its own (last event, `done`, replaced on re-subscribe) SHALL leave the sandbox's live-stream registry. Each transport SHALL be backed by an explicit `Http2SessionManager` that `close()` aborts, so no HTTP/2 session outlives the `Sandbox`.

#### Scenario: headers on a unary and on a stream
- **WHEN** a `Sandbox` bound to the fake `rayd` calls `commands.list()` and then `commands.run("sleep 1", { background: true })`
- **THEN** the fake saw both requests with `x-aws-proxy-auth` equal to the minted JWE, `x-aws-proxy-port === "8080"`, `x-aws-proxy-force-h2 === "true"` and `x-access-token` equal to the sandbox's access token

#### Scenario: token rotation reaches the next request without a new transport
- **WHEN** the refresher mints a second JWE and the sandbox then calls `files.exists("/")`
- **THEN** the fake saw the new JWE on that request and the fake's HTTP/2 server counts exactly one session for the unary transport

#### Scenario: two transports at most
- **WHEN** a test runs thirty `commands.run("echo i")`, one `commands.run("sleep 5", { background: true })`, one `files.watchDir("/w")` and one `pty.create()`
- **THEN** the fake's HTTP/2 server has seen exactly two client sessions

#### Scenario: gzip transport reuses the unary session
- **WHEN** a test runs `files.write("/a", data, { gzip: true })`, one background `commands.run` and one `files.watchDir("/w")`
- **THEN** the fake received the `Write` with `grpc-encoding: gzip` and its HTTP/2 server has seen exactly two client sessions

#### Scenario: missing token is refused locally
- **WHEN** the `TokenStore` is cleared and `sbx.getHost(3000).headers` is read
- **THEN** `AuthenticationError` is thrown naming port 3000, and no request reached the fake

#### Scenario: close ends both HTTP/2 sessions
- **WHEN** a sandbox with a background `run` (both transports open, the fake's `server.getConnections()` at 2) calls `close()`
- **THEN** the fake's `server.getConnections()` reaches 0 within a few hundred milliseconds and stays there (no session reopens on its own; the fake still counts exactly two sessions ever)

#### Scenario: finished streams leave the live registry
- **WHEN** five foreground `run`, one background `run` waited to its end, one PTY exited and waited, one `runCode` and one `files.write`/`read` complete
- **THEN** `SandboxCore.liveStreamCount` is 0 after each of them, before `close()`

#### Scenario: plaintext only towards loopback
- **WHEN** `openTransport` is called with `scheme: "http"` for `127.0.0.1`, `::1` or `localhost`, and separately for `abc.example` or `10.0.0.5`
- **THEN** the loopback hosts get a transport whose session manager is still `closed`, and the others throw `InvalidArgumentError` before any connection; a `Sandbox.create({ transport: { scheme: "http" } })` whose `run-microvm` endpoint is not loopback rejects with `InvalidArgumentError`, opens no session and terminates the VM

### Requirement: Filesystem surface
`sandbox.files` SHALL expose `read(path, { format = "text" | "bytes" | "blob" | "stream", user, requestTimeoutMs, gzip, streamIdleTimeoutMs })` returning `string`, `Uint8Array`, `Blob` (type `application/octet-stream`) or `ReadableStream<Uint8Array>`, `write(path, data, { user, mode, requestTimeoutMs, gzip, metadata, useOctetStream }) → EntryInfo`, `writeFiles(entries, options) → EntryInfo[]` (same options), `list(path, { depth = 1 })`, `exists(path)`, `getInfo(path)`, `remove(path, { recursive = true })`, `rename(oldPath, newPath)`, `makeDir(path) → boolean`, `watchDir(path, { onEvent, onExit, recursive, includeEntry, user, timeoutMs = 0 }) → WatchHandle`, `uploadUrl(...)` and `downloadUrl(...)` (capability `sdk-file-transfer`), every method accepting `user` and `requestTimeoutMs`. `EntryInfo` SHALL carry `metadata: Readonly<Record<string, string>>`. `read` SHALL `Stat` first and refuse directories and symlinks client-side, use the deadline `60 000 + 1 000 × ⌈size / 1 000 000⌉` ms unless `requestTimeoutMs`, and decode `"text"` as strict UTF-8 (`InvalidArgumentError` on invalid bytes). `write`/`writeFiles` SHALL accept `string | Uint8Array | ArrayBuffer | Blob | ReadableStream<Uint8Array>`, materialise the data, send one client-stream `Write` with 1 MiB chunks carrying `path`/`user`/`mode`/`metadata` only on each file's first message, and use the deadline `60 000 + 1 000 × ⌈total / 1 000 000⌉` ms unless `requestTimeoutMs`; when `sandbox.transfer` is set, payloads at or above its `thresholdBytes` (and `ReadableStream` data of unknown size) SHALL take the S3 route of `sdk-file-transfer` instead. `gzip: true` SHALL add the header `rayito-compress: gzip` to `Read` and send `Write` through the gzip transport; `metadata` SHALL be validated client-side with the Python rules (`InvalidArgumentError` before any RPC); `streamIdleTimeoutMs > 0` SHALL abort a read whose next chunk does not arrive in time with `TimeoutError`; `useOctetStream` SHALL be accepted with no effect. `makeDir` SHALL return `false` on `AlreadyExists`. `NotFound` SHALL map to `FileNotFoundError` in this module. `WatchHandle` SHALL resolve only after `WatchStarted`, consume the stream in a detached task, dispatch `onEvent` (callback errors logged, watch continues) or collect for `getNewEvents()` (which throws the stored terminal error once), ignore `keepalive`, expose `path`, `isRunning`, `reconnects`, `stop()` (aborts, awaits ≤ 5 s, idempotent) and `[Symbol.asyncDispose]`; `Sandbox.close()` SHALL stop every live handle; `timeoutMs > 0` SHALL be the stream deadline (`TimeoutError`). Unary RPCs, `Write` and `Read` SHALL use the unary transport; `WatchDir` the stream transport.

#### Scenario: formats agree
- **WHEN** a 3 MB file is written from a `Uint8Array` and read as `"text"`, `"bytes"`, `"blob"` and `"stream"`
- **THEN** `"bytes"` equals the written data, `"blob"` is a `Blob` whose `arrayBuffer()` equals it, `"stream"` yields chunks of at most 262 144 bytes whose concatenation equals it, and `"text"` rejects with `InvalidArgumentError` when the data is not UTF-8

#### Scenario: deadline arithmetic
- **WHEN** 8 000 000 bytes are written without `requestTimeoutMs`
- **THEN** the fake saw `grpc-timeout` of 68 s on the `Write`, and with `requestTimeoutMs 5000` it saw 5 s

#### Scenario: write data forms and one stream
- **WHEN** `writeFiles([{ path: "a", data: "hola" }, { path: "b", data: new Blob([bytes]) }, { path: "c", data: new Uint8Array(2_500_000) }])` runs
- **THEN** the fake received one `Write` stream with `path` only on each file's first message, the third file in three chunks (1 MiB, 1 MiB, rest), and the result has three `EntryInfo`s in order

#### Scenario: watch lifecycle
- **WHEN** `w = await files.watchDir("/w", { onEvent: seen.push })`, the fake emits a `create` event, then `await w.stop()`
- **THEN** `seen` has one event with `type "create"`, `w.isRunning` is `false`, `w.getNewEvents()` returns `[]`, and a second `stop()` resolves immediately

#### Scenario: metadata and gzip options
- **WHEN** `write("/m", "x", { metadata: { Owner: "alice" }, gzip: true })` and then `getInfo("/m")` run against the fake
- **THEN** the fake's first `Write` message carried `metadata {owner: "alice"}` with `grpc-encoding: gzip`, and `getInfo` returns `metadata` equal to `{ owner: "alice" }`

#### Scenario: idle read aborted
- **WHEN** the fake `Read` sends one chunk and stalls and `read(path, { format: "bytes", streamIdleTimeoutMs: 500 })` runs
- **THEN** it rejects with `TimeoutError` about 500 ms after the first chunk

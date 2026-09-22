## Context

State after M5 (accepted 2026-09-16 against real AWS, image `rayito-base`
10.0): `rayd` serves `HealthService`, `ProcessService`,
`FilesystemService`, `CodeService` and `PtyService` over h2c behind the AWS
proxy; `/suspend` closes every client stream with a recognisable form and
`/resume` bumps `resume_generation`. The Python SDK `rayito` 0.0.5
(`clients/python`) is the only client: boto3 control plane behind a
`ControlPlane` protocol with per-operation token buckets, `grpcio` channels
(one unary, one lazy stream) with an `AuthMetadataPlugin` injecting the four
proxy headers, pure helper modules (`_sandbox_base`, `_process_base`,
`_filesystem_base`, `_code_base`, `_pty_base`) shared by identical sync and
async trees, the M5 reconnection contract (D13/D14 of the archived M5
design: shared `Health` poll, wake rules, dormant handles, `Connect(from_seq)`
/ `Pty.Connect` / `WatchDir` re-issue / `Reattach`), 429 unit tests against
in-process fake services plus a botocore `Stubber`, and the M1–M5 e2e suite
(6 passed in 333 s). `_limits.py` is a hand-written module of API constants
(`AWS_API_NOTES.md` §2, §3, §6, §11).

`buf.gen.yaml` already carries the TypeScript target
(`buf.build/bufbuild/es:v2.15.0`, `target=ts`, `import_extension=js`, out
`clients/typescript/src/gen`); `clients/typescript` does not exist yet.
`ARCHITECTURE.md` "Capa 3 — TypeScript" fixes the stack: `@bufbuild/protobuf`
^2.15 + `@connectrpc/connect` ^2.2, `createGrpcTransport` from
`@connectrpc/connect-node` with an interceptor adding the lowercase headers,
limits generated from the same JSON as `_limits.py`.

Facts verified on 2026-09-16 for this design (versions from the npm
registry, sources read from the tarballs):

- `buf generate` with the remote `bufbuild/es:v2.15.0` plugin **works from
  the development box** (BSR reachable, `buf` 1.73.0): six files
  `code_pb.ts`, `common_pb.ts`, `filesystem_pb.ts`, `health_pb.ts`,
  `process_pb.ts`, `pty_pb.ts` importing `@bufbuild/protobuf/codegenv2`
  (`fileDesc`, `messageDesc`, `serviceDesc`), messages as `Message<"…">`
  types with `XxxSchema` descriptors, oneofs as `{ case, value }` unions,
  services as `GenService` constants (`ProcessService`, `PtyService`, …).
- `@aws-sdk/client-lambda-microvms` **exists** (3.1133.0, `engines.node >=
  20`): commands `RunMicrovmCommand`, `GetMicrovmCommand`,
  `ListMicrovmsCommand` (+ `paginateListMicrovms`), `SuspendMicrovmCommand`,
  `ResumeMicrovmCommand`, `TerminateMicrovmCommand`,
  `CreateMicrovmAuthTokenCommand`; request/response shapes match
  `AWS_API_NOTES.md` (`RunMicrovmRequest{imageIdentifier, imageVersion,
  executionRoleArn, idlePolicy{maxIdleDurationSeconds,
  suspendedDurationSeconds, autoResumeEnabled}, logging, runHookPayload,
  maximumDurationInSeconds, clientToken, ingressNetworkConnectors,
  egressNetworkConnectors}`, `GetMicrovmResponse{microvmId, state,
  endpoint, imageArn, imageVersion, executionRoleArn?, idlePolicy?,
  maximumDurationInSeconds, startedAt: Date, terminatedAt?, stateReason?}`,
  `MicrovmItem{microvmId, state, imageArn, imageVersion, startedAt}`,
  `CreateMicrovmAuthTokenRequest{microvmIdentifier, expirationInMinutes,
  allowedPorts: PortSpecification[]}` with `PortSpecification` =
  `{port}` | `{range:{startPort,endPort}}` | `{allPorts:{}}`, response
  `authToken: Record<string,string>`); error classes with `name`:
  `ResourceNotFoundException`, `ValidationException`,
  `AccessDeniedException`, `ThrottlingException` (`retryAfterSeconds?`),
  `ConflictException`, `ServiceQuotaExceededException` (`quotaCode?`),
  `InsufficientCapacityException`, `InternalServerException`.
- `@connectrpc/connect-node` 2.2.0: `createGrpcTransport(options:
  GrpcTransportOptions)` where `GrpcTransportOptions =
  NodeHttp2TransportOptions & { baseUrl, interceptors?, readMaxBytes?,
  writeMaxBytes?, defaultTimeoutMs?, … }` and `NodeHttp2TransportOptions =
  { sessionManager?, nodeOptions? } & Http2SessionOptions{pingIntervalMs?,
  pingIdleConnection?, pingTimeoutMs?, idleConnectionTimeoutMs?}`. **There
  is no `httpVersion` option** (gRPC is HTTP/2 only in v2); one transport
  owns one `Http2SessionManager`, i.e. one connection. `connectNodeAdapter({
  routes })` serves a `ConnectRouter` over `node:http2` (plaintext h2c is
  fine for the fake). Peer `@connectrpc/connect` 2.2.0, `@bufbuild/protobuf`
  ^2.7.
- `@connectrpc/connect` 2.2.0 error facts: a non-200 HTTP response becomes
  `new ConnectError("HTTP <status>", codeFromHttpStatus(status))` with
  400 → `Internal`, 401 → `Unauthenticated`, 403 → `PermissionDenied`, 404
  → `Unimplemented`, 429/502/503/504 → `Unavailable`, else `Unknown`; a
  missing `grpc-status` is `ConnectError("protocol error: missing status",
  Internal)`; HTTP/2 `RST_STREAM` codes map through
  `connectErrorFromH2ResetCode` (`CANCEL` → `Canceled`, `REFUSED_STREAM` →
  `Unavailable`, others → `Internal`, all with `rawMessage` starting
  `http/2 stream closed with error code`); Node reasons map through
  `connectErrorFromNodeReason` (`ECONNRESET`, `ERR_STREAM_DESTROYED`,
  `ERR_HTTP2_INVALID_STREAM` → `Aborted`; `ETIMEDOUT`, `ENOTFOUND`,
  `EAI_AGAIN`, `ECONNREFUSED` → `Unavailable`; default `Internal`, cause
  chain kept in `cause`). `CallOptions = { timeoutMs?, headers?, signal?,
  onHeader? }`; `ConnectError` exposes `code`, `rawMessage`, `metadata`,
  `cause`.
- npm names: `rayito` and `@rayito/*` are unclaimed (404 on
  `pnpm view`). Node on the box: 22.21.1, pnpm 9.15.4.

## Goals / Non-Goals

**Goals:**

- Ship `clients/typescript` as the npm package `rayito` with the same
  surface as the Python SDK (E2B's JS naming: camelCase, `*Ms` durations,
  `*Error` classes), generated from the same `.proto`, validating the same
  limits, talking to the same `rayd` through the same proxy headers.
- Reproduce the M5 reconnection contract exactly (shared `Health` poll,
  wake rules, dormant handles, re-subscribe forms) so a TypeScript client
  survives `pause()`/`resume()` and auto-resume like the Python one.
- One limits source for both SDKs with a drift test in each.
- Unit tests against an in-process fake `rayd` and a fake control plane;
  a real-AWS e2e covering `SPEC.md` §6 through the TypeScript SDK.
- `pnpm install`, `pnpm build`, `pnpm test`, `pnpm lint`, `pnpm typecheck`
  green on Windows and Linux; CI job; `Makefile` targets.

**Non-Goals:**

- Any change to `rayd`, the sidecar, the image or the `.proto`.
- A sync TypeScript tree, browser/Deno/Bun support, gRPC-web or Connect
  protocol (the proxy speaks gRPC to `rayd`), HTTP/1.1.
- Publishing to npm (`pnpm pack` is the last step; the release is a
  separate act), the `rayito.e2b` shim, docs site.
- The other M6 tracks (cgroups, IMDS block, egress, benchmark, sidecar in
  Rust, metadata, filesystem persistence).
- Feature parity beyond Python 0.0.5 (no `setTimeout`, no metadata, no
  per-sandbox size: `SPEC.md` §4).

## Decisions

### D1. Package, toolchain and gates

`clients/typescript/package.json`: `name: "rayito"`, `version: "0.0.5"`
(same SDK generation as Python 0.0.5: requires image ≥ 10.0),
`type: "module"`, `engines.node: ">=20"`, `packageManager: "pnpm@9.15.4"`,
`license: "MIT"`, `files: ["dist", "README.md"]`, `exports: { ".": {
"types": "./dist/index.d.mts", "import": "./dist/index.mjs", "require": {
"types": "./dist/index.d.cts", "default": "./dist/index.cjs" } } }`,
`main: "./dist/index.cjs"`, `module: "./dist/index.mjs"`, `types:
"./dist/index.d.mts"`, `sideEffects: false`. Not a pnpm workspace: the
repo root stays without `package.json`; every command runs from
`clients/typescript`.

Scripts: `build` = `tsdown`; `typecheck` = `tsc --noEmit`; `test` =
`vitest run --project unit`; `test:e2e` = `vitest run --project e2e`;
`lint` = `biome check .`; `format` = `biome format --write .`; `gen` =
`buf generate` run from the repo root (documented; `make proto` is the
canonical entry).

Dependencies (runtime): `@bufbuild/protobuf ^2.15.0` (floor = the plugin
version, as `protobuf>=7.36.1` mirrors `python:v36.1`), `@connectrpc/connect
^2.2.0`, `@connectrpc/connect-node ^2.2.0`, `@aws-sdk/client-lambda-microvms
^3.1133.0`, `@aws-sdk/client-sts ^3.1133.0`, `@smithy/node-http-handler
^4.12.1`. Dev: `typescript ^5.9.3` (not 7.x: tsdown/vitest/biome
integration is proven on 5.x), `tsdown ^0.23`, `vitest ^3.2` (a 5.x
vitest is acceptable if `vi.useFakeTimers` and `projects` keep the same
API; the implementer picks the newest that installs cleanly and records
it), `@biomejs/biome ^2.5`, `@types/node ^20` (the oldest supported
runtime's types, so `Symbol.asyncDispose` and `ReadableStream` typings
match Node 20). Lockfile committed.

`tsconfig.json`: `strict`, `exactOptionalPropertyTypes`,
`noUncheckedIndexedAccess`, `noImplicitOverride`, `verbatimModuleSyntax`,
`isolatedModules`, `module: "NodeNext"`, `moduleResolution: "NodeNext"`,
`target: "ES2022"`, `lib: ["ES2022", "ESNext.Disposable"]`, `types:
["node"]`, `skipLibCheck: true`, `include: ["src", "tests",
"tsdown.config.ts", "vitest.config.ts"]`. Generated files are excluded
from Biome (`biome.json` `files.includes` negates `src/gen/**`) and keep
their `/* eslint-disable */` header untouched; they are type-checked.

`tsdown.config.ts`: `entry: ["src/index.ts"]`, `format: ["esm", "cjs"]`,
`platform: "node"`, `target: "node20"`, `dts: true`, `fixedExtension:
true` (always `.mjs`/`.cjs`/`.d.mts`/`.d.cts`), `sourcemap: true`, `clean:
true`, `external` = every dependency (defaults: `dependencies` and
`peerDependencies` are external). The generated `*_pb.ts` files are
bundled into the output (they are sources of this package, not a
dependency).

`biome.json`: `formatter` 2-space, line width 100, double quotes;
`linter.recommended` plus `style/useNamingConvention` (camelCase members,
PascalCase types, `CONSTANT_CASE` for module-level constants), `suspicious/
noExplicitAny: error`, `correctness/noUnusedImports: error`; `organizeImports`
on. Comments follow the repo rule: WHY not WHAT, no inline comments inside
function bodies, JSDoc only on public API and non-obvious contracts.

`vitest.config.ts`: `projects: [{ name: "unit", include:
["tests/unit/**/*.test.ts"], testTimeout: 20_000 }, { name: "e2e",
include: ["tests/e2e/**/*.e2e.test.ts"], testTimeout: 900_000,
hookTimeout: 300_000, fileParallelism: false }]`; `pnpm test` runs only
`unit`; the e2e project is `describe.skipIf(!e2eEnabled())` so it also
reports as skipped when someone runs `vitest run` without the guard.

### D2. Codegen

`buf.gen.yaml` is used as committed (the es target already exists); `make
proto` = `buf lint` + `buf generate` regenerates Python and TypeScript
together. The output `clients/typescript/src/gen/rayito/v1/{code,common,
filesystem,health,process,pty}_pb.ts` is committed like the Python
`v1/*_pb2*`. No hand-written request/response types anywhere: the SDK
builds messages with `create(XxxSchema, {...})` from `@bufbuild/protobuf`
and reads oneofs through `{ case, value }`.

Fallback, documented in `clients/typescript/README.md` "Desarrollo" and
not wired: if the BSR is unreachable, `buf generate --template
buf.gen.local.yaml` where the template lists `local: ["pnpm", "--dir",
"clients/typescript", "exec", "protoc-gen-es"]` with the same `opt`s, after
`pnpm add -D @bufbuild/protoc-gen-es@2.15.0`. The output is byte-identical
(same plugin version), so the committed files do not change.

A unit test asserts the generated surface the SDK depends on exists
(`ProcessService.method.start.methodKind === "server_streaming"`,
`FilesystemService.method.write.methodKind === "client_streaming"`,
`PtyServerMessageSchema.field.seq`), so an accidental regeneration with a
different plugin fails fast.

### D3. Module layout (`clients/typescript/src`)

```
index.ts                  public exports (D18)
limits.ts                 GENERATED by scripts/gen_limits.py from ../../limits.json
errors.ts                 error hierarchy (D16)
models.ts                 SandboxInfo, SandboxListItem, IdlePolicy, HostAccess, CommandResult,
                          ProcessInfo, PtySize, SandboxHealth, SandboxMetrics, EntryInfo, FileType,
                          FilesystemEvent(Type), WriteEntry, CodeContext, OutputMessage, Logs,
                          ExecutionError, Result, Execution + validators (pure)
charts.ts                 Chart types + parseChart (pure, mirrors _charts.py)
payload.ts                access token + runHookPayload (pure, mirrors _payload.py)
aws/control-plane.ts      ControlPlane interface, PortSpec, LaunchRequest, TokenBucket,
                          LambdaMicrovmsControlPlane (SDK v3 adapter), sharedControlPlane,
                          translateAwsError, response → model mappers
transport/headers.ts      header names, proxyAuthInterceptor(store, port, accessToken)
transport/tokens.ts       ProxyToken, TokenStore, TokenRefresher
transport/transport.ts    TransportSettings, openTransport(host, settings, interceptor)
transport/errors.ts       isProxyForbidden, isPhaseGate, isKernelGate, isStreamReset,
                          isReconnectable, translateRpcError, translateStreamError
sandbox/launch.ts         resolveTemplate, resolveAccessToken, validateTimeoutMs, resolveIdlePolicy,
                          proxyPortSpecs, connectorArns, loggingConfig, buildLaunchPlan (pure)
sandbox/readiness.ts      ReadinessPoll, ReconnectPoll, reconnectFailure, healthFromProto (pure)
sandbox/sandbox.ts        Sandbox (lifecycle, channels, reconnect, wake rules)
sandbox/commands.ts       Commands, CommandHandle + pure helpers (buildStartRequest, OutputAccumulator,
                          CommandProgress, StreamAdapter, ProcessEvents)
sandbox/pty.ts            Pty, PtyHandle + pure helpers (buildPtyStartRequest, PtyMessages)
sandbox/filesystem.ts     Filesystem, WatchHandle + pure helpers (deadlines, request builders,
                          entryInfoFromProto, WatchState)
sandbox/code.ts           CodeClient (runCode, contexts), ExecutionBuilder + pure helpers
gen/rayito/v1/*_pb.ts     generated
```

Pure helpers are exported from the module that uses them (there is one
tree, so Python's `_x_base.py` split collapses into "helpers above the
class"); unit tests import them directly. Every module is ESM with `.js`
import extensions (`verbatimModuleSyntax` + `NodeNext`).

### D4. Transport and proxy headers

`TransportSettings` (`transport/transport.ts`): `{ scheme: "https" | "http"
= "https", port = ENDPOINT_TLS_PORT (443), pingIntervalMs = 30_000,
pingTimeoutMs = 10_000, pingIdleConnection = false (amendment 8.5 below;
it was `true` until the M6 acceptance run), readMaxBytes = 64 MiB,
nodeOptions?: http2.SecureClientSessionOptions }`; `baseUrl(host) =
`${scheme}://${host}:${port}``. Tests point at the fake with `{ scheme:
"http", port }`; `scheme: "http"` is refused with `InvalidArgumentError`
for any host other than `127.0.0.1`, `::1` or `localhost` (the proxy JWE
and `x-access-token` would travel in clear; Python cannot do it either).
`openTransport(host, settings, interceptor)` builds an explicit
`Http2SessionManager(baseUrl, { pingIntervalMs, pingTimeoutMs,
pingIdleConnection, idleConnectionTimeoutMs: 15 * 60_000 }, nodeOptions)`,
passes it as `sessionManager` to `createGrpcTransport({ baseUrl,
interceptors: [interceptor], readMaxBytes, sessionManager })` and returns
`{ transport, sessionManager }` so `close()` can `abort()` the session
(the equivalent of Python's `channel.close()`). The ping options
are the Connect equivalents of Python's `grpc.keepalive_*` channel
options; there is no client reconnect backoff to cap because connect-node
opens a fresh HTTP/2 session on the next request after a failure, which
already satisfies the M5 intent ("a resumed sandbox answers within
seconds").

`Sandbox` owns two transports at most: `unary` (opened in the constructor;
`Health`, `Metrics`, every unary, foreground `Start`, `Read`, `Write`,
`Execute`, `Reattach`) and `stream` (opened lazily on first use;
background `Start`, `Connect`, `Pty.Create`/`Connect`, `WatchDir`). One
transport = one `Http2SessionManager` = one connection, so the M2 rule "at
most two channels per sandbox" holds by construction. Clients are built
with `createClient(Service, transport)` per transport, memoised per
service.

`proxyAuthInterceptor(store, { port, accessToken })`
(`transport/headers.ts`): an `Interceptor` that, on every call (unary and
stream), reads `store.jweFor(port)` and sets `x-aws-proxy-auth`,
`x-aws-proxy-port` (`String(port)`), `x-aws-proxy-force-h2: "true"` and
`x-access-token` on `req.header`; when the store has no token for the port
it throws `AuthenticationError("no hay token del proxy para el puerto N")`
before the request leaves. Header names are lowercase constants
(`PROXY_AUTH_HEADER`, `PROXY_PORT_HEADER`, `PROXY_FORCE_H2_HEADER`,
`ACCESS_TOKEN_HEADER`); Node's `http2` lowercases anyway, the constants
make the intent explicit. Because the interceptor reads the store per
call, a refreshed JWE reaches the next request without touching the
transport, exactly like Python's `AuthMetadataPlugin`.

Deadlines: every RPC passes `timeoutMs` in `CallOptions` (Connect emits
`grpc-timeout` and fails client-side with `Code.DeadlineExceeded`); the
vocabulary is Python's: `requestTimeoutMs` for unaries (default 60 000),
per-operation `timeoutMs` (commands/PTY 60 000, `runCode` 300 000,
`watchDir` 0 = unlimited) enforced by the server, the stream deadline is
`timeoutMs + 5 000` for process/PTY streams and `timeoutMs + 15 000` for
`Execute` (none for `0`/`undefined`), file deadlines `60 000 + 1 000 per
1 000 000 bytes`. Cancellation: each stream owns an `AbortController`
passed as `signal`; `disconnect()`, `stop()`, `close()` and leaving a
`runCode` early abort it (a `Canceled` error whose `cause` is our own abort
is never classified as a reset).

**Amendment 8.5 (M6 acceptance, 2026-09-16) — no idle PINGs.** The
acceptance run of `tests/e2e/m6.e2e.test.ts` passed both tests but the
process died 30 s after each `close()` with an uncaught
`ERR_HTTP2_INVALID_SESSION` raised from connect-node's own PING timer
(`Timeout.onPingInterval → commonPing → ClientHttp2Session.ping`). Cause,
read in `@connectrpc/connect-node` 2.2.0 `http2-session-manager.js`:
`Http2SessionManager.abort()` calls `conn.destroy()` and then, synchronously,
`onExitState()` → `cleanup()`, which stops the timers and detaches the
session listeners; the streams that were open are destroyed by Node on the
next tick, and each stream's `close` handler runs `streamCount--` and, at
zero, `resetPingInterval()`, which with `pingIdleConnection: true` arms a
fresh unref'd 30 s timer that nobody clears any more; when it fires,
`ping()` on the destroyed session throws inside a timer callback. Any
`close()` with a live stream (background command, PTY, `watchDir`,
`runCode`) in a process that lives ≥ 30 s more reproduces it, so it is an SDK
crash, not a test artefact. The unit fake always passed
`pingIdleConnection: false`, which is why 291 unit tests never saw it. Fix:
`DEFAULT_TRANSPORT_SETTINGS.pingIdleConnection = false` (with
`streamCount == 0` the re-arm is a no-op; `requiresVerify()` still PINGs
before the first request after > `pingIntervalMs` of silence, so a dead
idle session is detected and replaced exactly as before) and a regression
test in `tests/unit/sandbox.test.ts` that opens a background command with
the default transport, calls `close()`, lets the stream `close` handlers run
under fake `setTimeout`, and asserts that advancing the clock by
2 × `pingIntervalMs` throws nothing (it throws the exact
`ERR_HTTP2_INVALID_SESSION` with the old default). Divergence from Python
(`keepalive_permit_without_calls=1`) is documented in the JSDoc of the
constant; deferring `sessionManager.abort()` until the streams have closed
was rejected because the stream `close` events depend on nghttp2 sending
`RST_STREAM`, which is not observable from the SDK.

### D5. Token store and refresher

`transport/tokens.ts` mirrors `_transport.py`: `ProxyToken { jwe, ports:
PortSpec[], mintedAt }` with `covers(port)`, `refreshDue(now)`,
`secondsUntilRefresh(now)`; `TokenStore` (`put`, `tokenFor`, `jweFor`,
`tokens`, `clear`); `TokenRefresher(store, mint, { now = Date.now })` with
`mint(ports)`, `ensure(port)`, `refreshAll()`, `refreshDue(now)`,
`secondsUntilNextRefresh(now)`, `start()`, `stop()`. `start()` schedules a
`setTimeout` chain (`timer.unref()` so an idle process can exit) that
calls `refreshDue()` at `TOKEN_REFRESH_AFTER_MINUTES` (45) and retries
after `TOKEN_REFRESH_RETRY_SECONDS` (60) when a mint rejects; a rejected
mint is logged as a warning without the token. The refresher keeps running
across `pause()` (wall-clock expiry). `stop()` clears the timer and is
idempotent; `close()` calls it. Unit tests inject `now` and use
`vi.useFakeTimers()` for the schedule.

### D6. Control plane (`aws/control-plane.ts`)

`ControlPlane` interface = the Python protocol in camelCase: `region`,
`resolveTemplateArn(template)`, `runMicrovm(request: LaunchRequest)`,
`getMicrovm(sandboxId)`, `listMicrovms({ imageArn?, imageVersion?,
states? }) → AsyncIterable<SandboxListItem>`, `terminateMicrovm(sandboxId)
→ boolean`, `suspendMicrovm(sandboxId) → boolean`, `resumeMicrovm(sandboxId)
→ boolean`, `createAuthToken(sandboxId, ports: PortSpec[]) → string`.

`LambdaMicrovmsControlPlane` adapts `@aws-sdk/client-lambda-microvms`. It
takes `{ client: CommandSender, region, stsClient?: CommandSender |
(() => CommandSender), now?, sleep? }` where `CommandSender = { send(command:
unknown): Promise<unknown> }` (the structural subset of `LambdaMicrovmsClient`
and `STSClient`, so tests inject a recorder without any mocking library).
`LambdaMicrovmsControlPlane.fromRegion(region?, { credentials? })` builds
`new LambdaMicrovmsClient({ region, credentials, maxAttempts: 5,
retryMode: "standard", requestHandler: new NodeHttpHandler({
connectionTimeout: 5_000, requestTimeout: 60_000 }), customUserAgent:
[["rayito", VERSION]] })` and a lazy `STSClient` with the same config
(only when a template is given by name). Requests use exactly the field
names of `AWS_API_NOTES.md` §2, §3, §5, §6: `RunMicrovmCommand({
imageIdentifier, imageVersion?, executionRoleArn?, idlePolicy?, logging,
runHookPayload, maximumDurationInSeconds, clientToken,
ingressNetworkConnectors?, egressNetworkConnectors? })`,
`GetMicrovmCommand({ microvmIdentifier })`, `paginateListMicrovms({ client,
pageSize: LIST_MAX_RESULTS }, { imageIdentifier?, imageVersion? })` filtered
client-side (`states` given → keep those; omitted → drop
`TERMINATING|TERMINATED`), `SuspendMicrovmCommand`/`ResumeMicrovmCommand`/
`TerminateMicrovmCommand({ microvmIdentifier })`,
`CreateMicrovmAuthTokenCommand({ microvmIdentifier, expirationInMinutes:
TOKEN_TTL_MINUTES, allowedPorts: ports.map(toApi) })` reading
`authToken["X-aws-proxy-auth"]` (case-insensitive fallback, single-entry
fallback, else `SandboxError`), never `{ allPorts: {} }` (ADR-006).
`terminateMicrovm` returns `false` only on `SandboxNotFoundError`;
`suspendMicrovm`/`resumeMicrovm` return `false` on `SandboxStateError`
(`ConflictException`). `resolveTemplateArn` returns ARNs as-is, validates
names against `^[a-zA-Z0-9_-]{1,64}$`, and builds
`arn:<partition>:lambda:<region>:<account>:microvm-image:<name>` from one
cached `GetCallerIdentityCommand`.

Token buckets: `TokenBucket(ratePerSecond, { now = performance.now / 1000,
sleep })` with the Python semantics (capacity = rate, tokens may go
negative, `acquire()` returns the wait). One bucket per operation in
`API_TPS` (`limits.ts`), applied in `invoke(operation, command)` before
`client.send`. `sharedControlPlane(region?)` keeps one plane per region
string per process (module `Map`), so N concurrent `Sandbox.create()`
without an explicit `controlPlane`/`client` share buckets and the SDK
client; passing `client` builds a private plane with its own buckets
(documented).

Error mapping **by `error.name`** (never by HTTP status), `translateAwsError`:
`ResourceNotFoundException` → `SandboxNotFoundError`, `ValidationException`
→ `InvalidArgumentError`, `AccessDeniedException` → `AuthenticationError`,
`ThrottlingException` → `RateLimitError(retryAfter = retryAfterSeconds)`,
`ConflictException` → `SandboxStateError`, `ServiceQuotaExceededException`
→ `QuotaExceededError(quotaCode)`, `InsufficientCapacityException` →
`CapacityError`, anything else with a `name`/`$metadata` → `SandboxError`
with `awsCode = name`, `statusCode = $metadata.httpStatusCode`.

Model mappers: `sandboxInfoFromResponse` (normalises `endpoint` to the
bare hostname; `startedAt`/`terminatedAt` are `Date`s already),
`sandboxListItemFromResponse`, `idlePolicyFromResponse`, `idlePolicyToApi`
(requires the resolved `suspendedDurationSeconds`).

### D7. Limits: one JSON source for both SDKs

`limits.json` (repo root) holds every constant of `_limits.py` under
camelCase keys with the same values: `maxDurationSeconds 28800`,
`minDurationSeconds 1`, `idleMaxIdleMinSeconds 60`,
`idleSuspendedMinSeconds 0`, `tokenTtlMinutes 60`, `tokenTtlMinMinutes 1`,
`tokenRefreshAfterMinutes 45`, `tokenRefreshRetrySeconds 60`,
`runHookPayloadMaxChars 4096`, `clientTokenMax 128`, `microvmIdMinLength
1`, `microvmIdMaxLength 256`, `listMaxResults 50`, `networkConnectorsMax
10`, `defaultPort 8080`, `hooksPort 9000`, `portMin 1`, `portMax 65535`,
`endpointTlsPort 443`, `hookPathPrefix`, `maxConcurrentConnections1Vcpu
8`, `microvmStates [6]`, `terminalStates [2]`, `suspendedStates [2]`,
`managedNetworkConnectors [4]`, `supportedRegions [10]`, `apiTps {7}`.
Every value is a property of the API (`AWS_API_NOTES.md` §2, §3, §6, §11),
never user-configurable.

`scripts/gen_limits.py` (stdlib only, `ruff` clean): reads the JSON and
renders (a) `clients/python/src/rayito/_limits.py` with the existing
constant names, `Final` annotations, `tuple` for lists, `frozenset` for
the state/connector sets, `dict[str, int]` for `apiTps`, and the module
docstring stating it is generated; (b) `clients/typescript/src/limits.ts`
with the same `UPPER_SNAKE` names as `export const X = … as const`,
`ReadonlySet<string>` for sets (`new Set([...])`), `Readonly<Record<string,
number>>` for `API_TPS`, a header comment stating it is generated.
`--check` renders in memory and exits 1 with a diff when either file
differs; `make limits` regenerates, `make lint` runs `--check`. Rendering
is deterministic (sorted keys where the JSON is an object, insertion
order where it is an array).

Drift tests: `clients/python/tests/unit/test_limits.py` loads
`limits.json` (path relative to the repo, skipped if absent when the
package is installed from a wheel) and asserts every constant of
`rayito._limits` equals the JSON value (name mapping camelCase →
UPPER_SNAKE); `tests/unit/limits.test.ts` does the same for `limits.ts`
importing the JSON with `import limits from "../../../../limits.json"
with { type: "json" }`. Both fail when someone edits a rendered file by
hand or the JSON without regenerating.

### D8. Public surface — naming rules

- Async-only: every network method returns a `Promise`; iteration is
  `for await`. There is no sync tree (Node has no blocking gRPC client;
  E2B's JS SDK is async-only).
- camelCase mirror of Python: `run_code` → `runCode`, `get_host` →
  `getHost`, `send_stdin` → `sendStdin`, `write_files` → `writeFiles`,
  `make_dir` → `makeDir`, `watch_dir` → `watchDir`, `get_new_events` →
  `getNewEvents`, `last_seq` → `lastSeq`, `resume_generation` →
  `resumeGeneration`, `create_code_context` → `createCodeContext`, …
- Durations that the caller chooses are milliseconds with the `Ms` suffix
  (E2B JS): `timeoutMs` (sandbox lifetime in `create`, per-operation server
  timeout in `commands.run`/`pty.create`/`runCode`/`watchDir`),
  `requestTimeoutMs`, `readyTimeoutMs`, `reconnectTimeoutMs`. Fields that
  mirror an AWS or proto value keep that unit in their name:
  `IdlePolicy.maxIdleSeconds`, `suspendedDurationSeconds`,
  `SandboxInfo.maximumDurationSeconds`, `SandboxHealth.uptimeMs`,
  `clockOffsetMs`. Conversions: `maximumDurationInSeconds =
  Math.ceil(timeoutMs / 1000)` after validating `1 000 ≤ timeoutMs ≤
  28 800 000` (`SandboxLifetimeError` above, `InvalidArgumentError` below
  or non-integer); `timeout_ms` on the wire = `Math.round(timeoutMs)`.
- Keyword arguments become a trailing options object; positional
  arguments stay positional (`commands.run(cmd, opts)`, `files.write(path,
  data, opts)`, `files.rename(oldPath, newPath, opts)`, `pty.sendInput(pid,
  data, opts)`).
- Bytes are `Uint8Array`; text is `string`; `envs` are
  `Record<string, string>`; enums that are strings in Python
  (`FileType`, `FilesystemEventType`, `ChartType`, `ScaleType`) are string
  literal unions plus a frozen object of the same name (`FileType.FILE ===
  "file"`), the E2B JS pattern.
- Data models are plain readonly interfaces built by mapper functions
  (`Object.freeze`d), except `Execution`, `Result` and `HostAccess` which
  are classes because they carry behaviour (`text` getter, `formats()`,
  `toJSON()`, `toString()`).
- Static-and-instance pairs (Python's `class_method_variant`): `kill`,
  `getInfo`, `pause`, `resume` exist as `static Sandbox.kill(sandboxId,
  opts)` and `sandbox.kill()`; TypeScript allows both names on one class.

### D9. `Sandbox` lifecycle surface (`sandbox/sandbox.ts`)

```ts
type LoggingOption = "disabled" | "cloudwatch" | { disabled: {} } | { cloudWatch: { logGroup: string } };
type PortLike = number | readonly [number, number];
interface Logger { debug?(msg: string, fields?: object): void; info?(…): void; warn?(…): void; error?(…): void }

interface SandboxCreateOptions {
  template?: string;                 // or RAYITO_TEMPLATE
  templateVersion?: string;
  timeoutMs?: number;                // default 3_600_000; max 28_800_000 (ADR-007)
  idle?: IdlePolicy | null;          // default IdlePolicy(); null disables auto-suspend
  envs?: Record<string, string>;
  executionRoleArn?: string;
  allowedPorts?: PortLike[];
  ingress?: string[]; egress?: string[];
  logging?: LoggingOption;           // default "disabled"
  region?: string;
  accessToken?: string;              // or RAYITO_ACCESS_TOKEN, else generated
  readyTimeoutMs?: number;           // 90_000
  requestTimeoutMs?: number;         // 60_000
  reconnectTimeoutMs?: number;       // 60_000
  keepOnFailure?: boolean;
  controlPlane?: ControlPlane; client?: CommandSender; transport?: Partial<TransportSettings>;
  logger?: Logger;
}
interface SandboxConnectOptions { accessToken?; region?; readyTimeoutMs?; requestTimeoutMs?; reconnectTimeoutMs?; controlPlane?; client?; transport?; logger? }
interface SandboxListOptions { template?; templateVersion?; states?: string[]; region?; controlPlane?; client? }
interface ControlPlaneOptions { region?; controlPlane?; client? }

class Sandbox {
  static create(opts?: SandboxCreateOptions): Promise<Sandbox>;
  static connect(sandboxId: string, opts?: SandboxConnectOptions): Promise<Sandbox>;
  static list(opts?: SandboxListOptions): AsyncIterable<SandboxListItem>;
  static kill(sandboxId: string, opts?: ControlPlaneOptions): Promise<boolean>;
  static getInfo(sandboxId: string, opts?: ControlPlaneOptions): Promise<SandboxInfo>;
  static pause(sandboxId: string, opts?: ControlPlaneOptions & { wait?: boolean; readyTimeoutMs?: number }): Promise<boolean>;
  static resume(sandboxId: string, opts?: ControlPlaneOptions & { wait?: boolean; readyTimeoutMs?: number }): Promise<void>;

  readonly sandboxId: string; readonly accessToken: string; readonly endpoint: string;
  readonly endpointUrl: string; readonly region: string;
  get info(): SandboxInfo; get resumeGeneration(): number;
  readonly commands: Commands; readonly files: Filesystem; readonly pty: Pty;

  kill(): Promise<boolean>;
  getInfo(): Promise<SandboxInfo>;
  pause(opts?: { wait?: boolean }): Promise<boolean>;
  resume(opts?: { wait?: boolean }): Promise<void>;
  isRunning(): Promise<boolean>;
  getHealth(opts?: { requestTimeoutMs?: number }): Promise<SandboxHealth>;
  getHost(port: number): HostAccess;
  getMetrics(opts?: { requestTimeoutMs?: number }): Promise<SandboxMetrics>;
  runCode(code: string, opts?: RunCodeOptions): Promise<Execution>;
  createCodeContext(opts?: CreateContextOptions): Promise<CodeContext>;
  listCodeContexts(opts?: RequestOptions): Promise<CodeContext[]>;
  removeCodeContext(context: CodeContext | string, opts?: RequestOptions): Promise<void>;
  restartCodeContext(context: CodeContext | string, opts?: RequestOptions): Promise<void>;
  close(): void;
  [Symbol.asyncDispose](): Promise<void>;   // kill()
}
```

Semantics are Python's, line by line: `create()` → `resolveControlPlane`
(`controlPlane` > `client` (private plane) > `sharedControlPlane(region)`)
→ `resolveTemplateArn(resolveTemplate(template))` → `buildLaunchPlan`
(`sandbox/launch.ts`: access token resolution/validation, `timeoutMs`
validation, idle policy resolution `suspendedDurationSeconds = seconds −
maxIdleSeconds` with `maxIdleSeconds < seconds` enforced, proxy port specs
with 8080 always first and 9000 refused, connector ARNs (managed names or
`arn:`), logging config with `/rayito/<template>`, `clientToken =
randomUUID()`, `runHookPayload` from `payload.ts`) → `runMicrovm` →
`open()`: mint the JWE for the plan's ports, build the `Sandbox`, wait
for `Health` with `ReadinessPoll` (0.25 s doubling to 2 s, `get-microvm`
every 5 s, `TERMINATING|TERMINATED` fatal, `agent_ready && kernel_ready`),
any failure before readiness closes the sandbox and, unless
`keepOnFailure`, terminates the VM (`SandboxNotReadyError` excepted: the
readiness poll already decided); then `refresher.start()`. `connect()`
requires the access token, refuses terminal states, calls
`resumeMicrovm` on `SUSPENDED` without auto-resume, and never terminates
on failure. `kill()` = `terminateMicrovm` then `close()` (also on failure).
`pause({ wait })` reads `getMicrovm` first and returns `false` on
`SUSPENDING|SUSPENDED` (Q38), else marks the pending pause, calls
`suspendMicrovm`, and with `wait` polls `getMicrovm` until `SUSPENDED`
within `readyTimeoutMs`. `resume({ wait })` clears the pending pause,
`resumeMicrovm` (a `false` is not an error), `refresher.refreshAll()`,
and with `wait` waits for `Health` recording the generation. `isRunning()`
= one `Health` probe with `min(5 s, requestTimeoutMs)` answers with
`agentReady`. `getHost(port)` validates the port (9000 refused), ensures a
token covering it, returns a `HostAccess`. `close()` stops the refresher,
stops every live watch, aborts every live stream's controller, aborts both
session managers (without it the two HTTP/2 sessions would live until the
15 min idle timeout, kept alive by PINGs), wakes dormant waiters with
`SandboxError`, and is idempotent; it never touches the VM. A stream that
ends on its own (last event, `done`, error, replaced on re-subscribe or
`Reattach`) releases its controller from the live registry
(`core.releaseStream`), so the registry never grows with the call count;
`SandboxCore.liveStreamCount` exposes it to the tests. `Symbol.asyncDispose` calls `kill()`; `index.ts` installs the E2B
polyfill `Symbol.asyncDispose ??= Symbol.for("Symbol.asyncDispose")` so
`await using` works on Node 20.

`HostAccess`: a class with `host`, `url` (`https://${host}`), `port`,
`headers` getter (`x-aws-proxy-auth` from the store on each read,
`x-aws-proxy-port`; never `force-h2`), `toString()` returning `host` so
``https://${sbx.getHost(3000)}`` works as in E2B. `SandboxInfo` carries
`expiresAt: Date`, `remainingSeconds(now?)`, `templateName`, `endpointUrl`
as methods/getters on a frozen object created by the mapper.

Logging: `logger` option (default none: the SDK is silent). Reconnection
lines carry `sandboxId`, the reason class and the generation; the
readiness line carries seconds; never bytes, paths' contents, code,
tokens, hook bodies.

### D10. Commands (`sandbox/commands.ts`)

```ts
interface CommandOptions { background?: boolean; envs?; user?; cwd?; onStdout?: (s: string) => void;
  onStderr?: (s: string) => void; stdin?: boolean; timeoutMs?: number /* 60_000; 0 = none */;
  requestTimeoutMs?: number; tag?: string }
interface ConnectOptions { fromSeq?: number; onStdout?; onStderr?; timeoutMs?: number; requestTimeoutMs?: number }
type OutputChunk = { stdout?: string; stderr?: string; pty?: Uint8Array };

class Commands {
  run(cmd: string, opts: CommandOptions & { background: true }): Promise<CommandHandle>;
  run(cmd: string, opts?: CommandOptions & { background?: false }): Promise<CommandResult>;
  connect(pid: number, opts?: ConnectOptions): Promise<CommandHandle>;
  list(opts?: RequestOptions): Promise<ProcessInfo[]>;
  kill(pid: number, opts?: RequestOptions): Promise<boolean>;
  sendStdin(pid: number, data: string | Uint8Array, opts?: RequestOptions): Promise<void>;
  closeStdin(pid: number, opts?: RequestOptions): Promise<void>;
}
class CommandHandle implements AsyncIterable<OutputChunk> {
  readonly pid: number; get lastSeq(): number; get stdout(): string; get stderr(): string;
  get exitCode(): number | undefined; get error(): string | undefined; get reconnects(): number;
  wait(): Promise<CommandResult>; kill(): Promise<boolean>; disconnect(): void;
  sendStdin(data): Promise<void>; closeStdin(): Promise<void>;
  [Symbol.asyncIterator](): AsyncIterator<OutputChunk>;
}
```

`run` builds `StartRequest{ config: { cmd: "/bin/bash", args: ["-l", "-c",
cmd], envs, cwd? }, user?, stdin, timeout_ms: round(timeoutMs), tag? }`
(pure `buildStartRequest`), opens `Start` on the unary transport when
foreground and on the stream transport when background, consumes the
leading `StartEvent` (pid) inside `openStream` (D13), and returns
`handle.wait()` or the handle. `OutputAccumulator` decodes stdout/stderr
incrementally with `TextDecoder("utf-8", { fatal: false })` streams
(`stream: true`), records `lastSeq`, invokes `onStdout`/`onStderr` with
the decoded text of each chunk. `CommandProgress` keeps M2's outcome
table: `exited` → `CommandResult` or `CommandExitError(exitCode, stdout,
stderr, error)` for a non-zero exit, `signaled` → `CommandExitError(128 +
signal…)` as Python, `timeout` → `TimeoutError`, `output_truncated` →
`SandboxError`, `suspending` → reconnect (D14). `wait()` drains the
iterator and resolves the outcome (a rejected outcome rejects `wait()`
and every later `wait()`); iteration is lazy: nobody reading the handle
means `rayd` applies backpressure and closes that subscriber with
`output_truncated` after 30 s. `disconnect()` aborts the stream and marks
the handle so it never reconnects; `kill()` is `SendSignal(pid, 9)` with
`NotFound → false`; `sendStdin` encodes strings as UTF-8; `connect(pid,
{fromSeq})` validates `pid` (`1 ≤ pid ≤ 2^32−1`) and `fromSeq ≥ 0`, opens
`Connect` on the stream transport with `timeoutMs` as the stream deadline.
`OutputChunk` is an object (`{stdout}` | `{stderr}` | `{pty}`) instead of
Python's tuple because tuples are not idiomatic in JS; the field set is the
same. `list()` maps `ProcessInfo{pid, cmd, args, envs, cwd?, tag?, kind:
"process" | "pty"}`.

### D11. Files (`sandbox/filesystem.ts`)

```ts
type ReadFormat = "text" | "bytes" | "stream";
class Filesystem {
  read(path: string, opts?: { format?: "text"; user?; requestTimeoutMs? }): Promise<string>;
  read(path: string, opts: { format: "bytes"; … }): Promise<Uint8Array>;
  read(path: string, opts: { format: "stream"; … }): Promise<ReadableStream<Uint8Array>>;
  write(path: string, data: WriteData, opts?: { user?; mode?: number; requestTimeoutMs? }): Promise<EntryInfo>;
  writeFiles(files: WriteEntry[], opts?: { user?; requestTimeoutMs? }): Promise<EntryInfo[]>;
  list(path: string, opts?: { depth?: number; user?; requestTimeoutMs? }): Promise<EntryInfo[]>;
  exists(path: string, opts?): Promise<boolean>;
  getInfo(path: string, opts?): Promise<EntryInfo>;
  remove(path: string, opts?: { recursive?: boolean /* true */; user?; requestTimeoutMs? }): Promise<void>;
  rename(oldPath: string, newPath: string, opts?): Promise<EntryInfo>;
  makeDir(path: string, opts?): Promise<boolean>;
  watchDir(path: string, opts?: { onEvent?; onExit?; recursive?; includeEntry?; user?; timeoutMs?: number /* 0 */; requestTimeoutMs? }): Promise<WatchHandle>;
}
type WriteData = string | Uint8Array | ArrayBuffer | Blob | ReadableStream<Uint8Array>;
interface WriteEntry { path: string; data: WriteData; mode?: number }
class WatchHandle { readonly path; get isRunning(); get reconnects(); getNewEvents(): FilesystemEvent[]; stop(): Promise<void>; [Symbol.asyncDispose]() }
```

`read` calls `Stat` first and refuses directories/symlinks client-side
(`InvalidArgumentError`), then opens `Read` on the unary transport with
the deadline `60 000 + 1 000 × ⌈size / 1 000 000⌉` (Python's
`file_request_deadline`) unless `requestTimeoutMs`; `"text"` decodes strict
UTF-8 (`TextDecoder("utf-8", { fatal: true })` → `InvalidArgumentError`
on failure), `"bytes"` concatenates, `"stream"` returns a
`ReadableStream<Uint8Array>` that pulls from the server stream (cancelling
it aborts the RPC). `write`/`writeFiles` materialise every `WriteData`
to `Uint8Array` (strings as UTF-8; `Blob`/`ReadableStream` via
`new Response(data).arrayBuffer()`), send one `Write` client-stream with
1 MiB chunks and `path`/`user`/`mode` only on each file's first message,
deadline `60 000 + 1 000 × ⌈total / 1 000 000⌉`, and map the returned
entries in order. `makeDir` returns `false` when `rayd` answers
`ALREADY_EXISTS` (`Code.AlreadyExists`). `NotFound` maps to
`FileNotFoundError` in this module. `WatchHandle` consumes the stream in a
detached async task (not awaited by the caller), dispatches `onEvent`
(errors inside the callback are logged and do not stop the watch),
collects for `getNewEvents()` otherwise, ignores `keepalive`, stores the
terminal error (`getNewEvents()` throws it once), calls `onExit(err)` on
end, `stop()` aborts and awaits the task (≤ 5 s), `Sandbox.close()` stops
every live handle. `timeoutMs > 0` becomes the stream deadline
(`TimeoutError`), `0`/`undefined` none. `watchDir` resolves only after
`WatchStarted`.

### D12. PTY and code (`sandbox/pty.ts`, `sandbox/code.ts`)

PTY: `Pty.create({ size?: PtySize, user?, cwd?, envs?, shell?, onData?:
(chunk: Uint8Array) => void, timeoutMs? = 60_000, requestTimeoutMs? })`
→ `PtyHandle`; `connect(pid, { fromSeq?, onData?, timeoutMs?,
requestTimeoutMs? })`; `sendInput(pid, data, opts)` (alias `sendStdin`);
`resize(pid, size, opts)`; `kill(pid, opts) → boolean`. `PtySize { cols =
80, rows = 24 }` validated `1 ≤ n ≤ 4096` (`validatePtySize`); `shell`
must be absolute. `PtyHandle extends CommandHandle` (the `PtyMessages`
stream adapter: `started` → pid, `data` → `{ pty: bytes }` with `seq`,
`exited` → end via `endEventFromPtyExited`, `keepalive` → nothing,
`exited{status:"suspending"}` → reconnect) plus `sendInput(data)`
(alias `sendStdin`), `resize(size)`; `stdout` is the terminal output
decoded with replacement, `stderr` is `""`; `kill()` uses `PtyService.Kill`;
re-subscribe uses `Pty.Connect`. Both `Create` and `Connect` use the stream
transport with deadline `timeoutMs + 5 000` (none for `0`/`undefined`).
`FailedPrecondition` ("not a PTY"/"is a PTY") → `InvalidArgumentError`.

Code: `runCode(code, { context?: CodeContext | string, onStdout?,
onStderr?: (m: OutputMessage) => void, onResult?: (r: Result) => void,
onError?: (e: ExecutionError) => void, envs?, timeoutMs? = 300_000,
requestTimeoutMs? })` validates `code` (non-empty, ≤ 1 MiB UTF-8), builds
`ExecuteRequest{ code, context_id?, envs, timeout_ms }`, opens `Execute`
on the unary transport with deadline `timeoutMs + 15 000` (none for
`0`/`undefined`; `requestTimeoutMs` replaces it), feeds every event into
an `ExecutionBuilder` (`started` → `executionId`; `stdout`/`stderr` →
`OutputMessage{ line, timestamp, error }` into `logs` and callbacks;
`result` → `Result` via `resultFromProto` (`json`/`data` parsed, raw kept,
`chart` via `parseChart`, `is_main_result`); `error` → `ExecutionError{
name, value, traceback }` (lines joined with `"\n"`); `end` →
`executionCount`; `keepalive` ignored) and returns `Execution` (`results`,
`logs: { stdout: string[], stderr: string[] }`, `error?`, `executionCount?`,
`text` getter = the `text` of the `isMainResult` result, `toJSON()` with
Python's shape). Leaving early (a callback throws, abort) cancels the
stream so `rayd` interrupts the cell. `createCodeContext({ cwd?,
language?, envs?, requestTimeoutMs? })` (90 s default deadline; validates
`language` in `{"", "python"}`, `cwd` absolute) returns the listed context
or a fallback; `listCodeContexts()`, `removeCodeContext(ctx)`,
`restartCodeContext(ctx)` (90 s) accept a `CodeContext` or its id;
`NotFound` → `NotFoundError`, `FailedPrecondition` → `InvalidArgumentError`.
`charts.ts` mirrors `_charts.py` exactly (`ChartType`, `ScaleType`,
`Chart`, `PointData`, `LineChart`, `ScatterChart`, `BarData`, `BarChart`,
`PieData`, `PieChart`, `BoxAndWhiskerData`, `BoxAndWhiskerChart`,
`SuperChart`, `parseChart` tolerant of malformed documents → `Chart` of
type `unknown`).

### D13. Stream opening, 403 re-mint and unary retry

`Sandbox.openStream(start, { transport, filesystem?, reconnect = true })`
runs the `start` thunk against the chosen transport's client and consumes
the **first** message (`StartEvent`, `PtyStarted`, `WatchStarted`, the
first `ReadResponse`, `ExecutionStarted`) before returning `{ stream,
first, controller }`: a proxy 403 at that point (`isProxyForbidden`:
`Code.PermissionDenied && rawMessage === "HTTP 403"`) re-mints via
`refresher.refreshAll()` and retries **once**; a reconnectable failure
(D14) before the first message runs `reconnect(reason, seenGeneration,
{ wake: true })` and retries once when `reconnect=true` (`Execute` passes
`reconnect: false` so a cell never runs twice); anything else is
translated (`translateRpcError`). `callUnary(call, { filesystem? })`
applies the same two rules to unaries: one 403 re-mint retry, and after a
reconnectable failure one retry when the reconnect resumed (documented
caveat: a `sendStdin`/`sendInput` cut after `rayd` applied it may apply
twice, as in E2B and Python). `Write` re-creates its request iterable for
the retry.

### D14. Reconnection contract (Python D14 transposed to one event loop)

Constants (`sandbox/readiness.ts`): `DEFAULT_RECONNECT_TIMEOUT_MS =
60_000`, `CLOCK_OFFSET_WARN_MS = 5000`, `ReconnectPoll` = `ReadinessPoll`
with `initialDelayMs 500`, `maxDelayMs 4000`, `jitter 0.25`,
`stateCheckIntervalMs 5000`, `rpcTimeoutMs = clamp(remaining, 500,
5000)`; injectable `now` and `random` for tests. `reconnectFailure(reason,
{ info?, timeoutMs?, wake })` → `SandboxNotFoundError` on
`TERMINATING|TERMINATED`, `SandboxStateError("… llama a resume()")` on
`SUSPENDED` without auto-resume for a `wake` caller, `SandboxStateError`
(suspending reason) or `SandboxError` on the deadline, `undefined` to keep
polling. `healthReconnected(response, { seenGeneration, suspending })`
requires `agentReady && kernelReady` and, after a `suspending` reason, a
new generation. `ReconnectOutcome { resumed, generationChanged,
resumeGeneration, error? }`; `ReconnectBudget` (3 futile reconnects
without a new generation, then the M2 classification).

Classification (`transport/errors.ts`), on `ConnectError` only:
- `isPhaseGate`: `Code.Unavailable` and `rawMessage ∈ {"suspending",
  "terminating"}`.
- `isKernelGate`: `Code.Unavailable` and `rawMessage.startsWith("kernel not
  ready")`.
- `isProxyForbidden`: `Code.PermissionDenied` and `rawMessage === "HTTP
  403"`.
- `isStreamReset`: `Code.Unavailable` and neither gate (covers proxy
  `HTTP 429/502/503/504`, `REFUSED_STREAM`, `ECONNREFUSED`/`ETIMEDOUT`);
  or `Code.Aborted` (`ECONNRESET`, destroyed stream); or `Code.Canceled`
  whose `rawMessage` starts with `http/2 stream closed` (an `RST_STREAM
  CANCEL` from the proxy, never our own `AbortSignal`); or `Code.Internal`
  whose `rawMessage` starts with `http/2 stream closed`, equals `protocol
  error: missing status`, or whose cause chain carries a Node error `code`
  starting with `ERR_HTTP2_` or equal to `ECONNRESET`/`EPIPE`.
- `isReconnectable = isStreamReset || isPhaseGate`; never
  `DeadlineExceeded`, never the kernel gate, never a proxy 403.
- The in-stream forms: `EndEvent{status:"suspending"}` and
  `PtyExited{status:"suspending"}` are `Suspending` in the stream adapter.

`Sandbox.reconnect(reason, seenGeneration, { wake }) →
Promise<ReconnectOutcome>`: if `resumeGeneration > seenGeneration` someone
already reconnected → resumed immediately; if closed → `SandboxError`.
`wake: true` callers share one in-flight poll (`pendingReconnect: Promise
| undefined` is the lock; a second caller awaits it and re-checks the
generation): poll `Health` on the unary transport with
`ReconnectPoll(reconnectTimeoutMs)`, treating `Unavailable`/
`DeadlineExceeded`/resets as "not yet" (`isNotYetReachable = Unavailable ||
DeadlineExceeded || isStreamReset`: Connect-ES spreads a dropped session
over `Aborted`/`Canceled`/`Internal`, which grpc-core folds into
`UNAVAILABLE`; the boot poll of `create()` uses the same predicate), `getMicrovm` every 5 s, stop early
on a terminal state or on `SUSPENDED` without auto-resume, and on success
`recordHealth` (generation, `kernelStateLost` info line, warning when
`|clockOffsetMs| > 5000`), `generationChanged = response.resumeGeneration
!== seenGeneration`. `wake: false` callers are dormant: after
`initialDelayMs` they read `getMicrovm` every 5 s while the state is
`SUSPENDING|SUSPENDED` without touching `Health` or the budget, wake early
when `recordHealth` sees a new generation (a `resumedSignal` promise
re-created after each fire) or `close()` runs, and once the state leaves
the suspended pair they take the `wake` path with a fresh budget,
re-checking the state after every failed probe and going back to sleep
if suspended again; a terminal state ends the wait with
`SandboxNotFoundError`. Who wakes: a unary retry, the first message of a
stream being opened, and an in-flight foreground `run`/`runCode` **unless
this `Sandbox` has a pending `pause()`**; a background handle, a
`PtyHandle`, a `WatchHandle` and a foreground stream cut by this
instance's `pause()` never wake (reading a handle never wakes a sandbox;
documented in README and JSDoc). `pause()` sets `paused` before
`suspendMicrovm`; `resume()` and any `recordHealth` with a new generation
clear it; `pause()` returning `false` leaves it untouched.

Handles: `CommandHandle`/`PtyHandle` iteration, on `Suspending` or a
reconnectable `ConnectError` while not disconnected → `reconnect(reason,
seen, { wake: this.wakes })`; on `resumed` re-subscribe with
`Connect(pid, fromSeq = lastSeq + 1)` (or `Pty.Connect`) on the stream
transport reusing callbacks and the remaining deadline, consume the
leading `StartEvent`/`started`, `reconnects += 1`, continue;
`OutOfRange` → one more attempt with `fromSeq = 0` and a warning
("se perdió salida entre …"); `NotFound` → `NotFoundError` stored as the
outcome; not resumed → the outcome fails with `outcome.error`; a
`ReconnectBudget` caps futile reconnects. A handle nobody reads
reconnects lazily on its next read. `WatchHandle`: re-issue `WatchDir`
with the same request and the remaining deadline, await `WatchStarted`,
keep the state, `onExit` not called, `isRunning` stays `true`. `runCode`:
after `started`, `Reattach(context_id, execution_id, fromSeq = lastSeq +
1)` on the unary transport feeding the same builder (`reattached += 1`);
`OutOfRange`/`NotFound` → `SandboxError("se perdió salida de la ejecución
…")`; before `started` no retry (`SandboxStateError` for the phase gate).

### D15. Fakes and unit tests (`tests/unit`)

Fake `rayd` (`tests/unit/fake/`): `startFakeRayd({ accessToken })` creates
`http2.createServer(connectNodeAdapter({ routes }))` listening on
`127.0.0.1:0` and returns `{ port, health, process, filesystem, code, pty,
suspend(), suspendResume({ unavailableCalls }), close() }`. Every handler
asserts the four proxy headers (`x-aws-proxy-port === "8080"`,
`x-aws-proxy-force-h2 === "true"`, a non-empty `x-aws-proxy-auth`) and
compares `sha256(base64url-decode(x-access-token))` with the installed
hash, answering `Unauthenticated` otherwise (`Health` exempt from the token
check, not from the headers). Behaviours mirror the Python fakes:

- `FakeHealth`: `Health` scripted (`agentReady`, `kernelReady`,
  `resumeGeneration`, `clockOffsetMs`, `kernelStateLost`), `unavailableCalls`
  countdown answering `Code.Unavailable`, `Metrics` fixed values; call
  counter.
- `FakeProcess`: `Start` → `start{pid}` then a scripted program (`echo x`,
  `exit N`, `sleep N`, `cat` with stdin echo, `big N` chunks, `slow`),
  ring per pid with `seq`, `Connect(from_seq)` replay + `OutOfRange`,
  `SendInput`/`CloseStdin`/`SendSignal`/`List`, `connectCalls: [pid,
  fromSeq][]`, `suspend()` ends live streams with `EndEvent{status:
  "suspending"}` and gates new `Start`/`Connect` with `Unavailable
  suspending` until `resume()`.
- `FakeFilesystem`: in-memory tree with `Read` (256 KiB chunks), `Write`
  (client stream, per-file entries, denied path → `PermissionDenied` after
  draining), `Stat`, `ListDir(depth)`, `MakeDir` (`AlreadyExists`), `Move`,
  `Remove`, `WatchDir` (`WatchStarted`, events pushed by `emit()`,
  `watchCalls`, `suspend()` aborts with `Unavailable suspending`).
- `FakeCode`: contexts (`default` first), `Execute` scripted cells (`x =
  42`, `x`, `print(x)`, a `png`+`chart` result, `1/0`, `slow N`), execution
  ring, `Reattach` (`reattachCalls`, replay, `NotFound`/`OutOfRange`),
  `suspend()` aborts `Execute` with `Unavailable suspending` and keeps the
  cell running, kernel-gate scripting.
- `FakePty`: echo shell (`echo x` → `x\r\n`, `stty size`, `exit N`),
  `Resize` recorded, `Kill` → `exited{signaled 137}`, `Connect(from_seq)`
  ring, `suspend()` ends with `exited{suspending}`.
- `suspendResume({ unavailableCalls })` orchestrates the five: streams end
  with their forms, `Health` answers `Unavailable` N times, then
  `resumeGeneration + 1`.

Fake control plane: `FakeControlPlane implements ControlPlane` with
scripted `getMicrovm` states (a queue), recorded calls, `createAuthToken`
counter; used by every `Sandbox` test through the `controlPlane` option.
The SDK adapter itself is tested in `aws.test.ts` with a recording
`CommandSender` (`send` returns scripted responses or throws error-shaped
objects with `name`), asserting the exact command inputs, the pagination,
the error table, the token buckets (injected clock/sleep), the ARN
resolution and the `authToken` key handling.

Test files (vitest, `tests/unit/*.test.ts`): `gen.test.ts` (D2),
`limits.test.ts` (D7), `payload.test.ts` (token base64url canonical form,
sha256 of the decoded bytes, payload ≤ 4096, envs validation),
`launch.test.ts` (template/env var, `timeoutMs` bounds and ceil, idle
resolution, port specs with 9000 refused, connectors, logging),
`tokens.test.ts` (store, refresher schedule with fake timers, retry on
failure, alive across pause, `unref`), `transport-errors.test.ts` (the
classification truth table built from hand-made `ConnectError`s and the
translation table, proxy 403 form), `aws.test.ts`, `readiness.test.ts`
(both polls with fixed random, deadlines, state-check cadence,
`reconnectFailure` matrix), `sandbox.test.ts` (create → run-microvm →
token → readiness → refresher; terminate-on-failure vs `keepOnFailure`;
connect on `SUSPENDED` with and without auto-resume; `kill`, `getInfo`,
`pause` false when suspended, `pause` marks `paused`, `resume` re-mints and
records the generation, `isRunning`, `getHealth`, `getHost` headers and
9000 refused, `getMetrics`, `close` idempotent, `Symbol.asyncDispose`
kills, static variants, `list` filtering), `commands.test.ts` (foreground
and background parity, callbacks, stdin `cat`, `exit 3` →
`CommandExitError`, timeout → `TimeoutError`, `list`, `kill` true/false,
`connect(fromSeq)` replay, `disconnect` keeps the process, 403 re-mint
once, transports used), `filesystem.test.ts` (read text/bytes/stream, strict
UTF-8, write materialisation for every `WriteData`, `writeFiles` one
stream with 1 MiB chunks, deadline arithmetic, `makeDir` false,
`FileNotFoundError`, `watchDir` events/callbacks/stop/asyncDispose),
`pty.test.ts` (create → `echo hola`, `onData`, `stdout` decoded, `resize`
→ `stty size`, `kill` → 137 then false, `connect`, wrong-kind →
`InvalidArgumentError`, `sendStdin` alias, deadline none for 0),
`code.test.ts` (acceptance sequence against the fake, callbacks typed,
contexts CRUD, `toJSON`, charts parsing table), `reconnect.test.ts` (every
scenario of the `suspend-resume` "Reconnection contract" requirement
transposed: background handle survives, unread handle reconnects on first
read, `OutOfRange` fallback, PTY via `Pty.Connect`, watch re-issued,
`runCode` reattaches, cut before `started` not retried, unary retried
once, terminated during the poll, suspended without auto-resume,
background handle sleeps through a suspension, `resume()` wakes dormant
handles, dormant handle does not block a foreground call, explicit pause
keeps foreground streams dormant, two concurrent reconnects share one
poll, budget of futile reconnects, `disconnect()`ed handles never
reconnect, `close()` during a reconnect). Target: ≥ 250 unit tests, all
green in < 60 s.

### D16. Error hierarchy (`errors.ts`)

```ts
class SandboxError extends Error { statusCode?: number; grpcCode?: Code; awsCode?: string }
class TimeoutError extends SandboxError {}
class InvalidArgumentError extends SandboxError {}
class NotFoundError extends SandboxError {}
class FileNotFoundError extends NotFoundError {}
class SandboxNotFoundError extends NotFoundError {}
class SandboxNotReadyError extends SandboxError { state?: string; stateReason?: string }
class SandboxStateError extends SandboxError {}
class SandboxLifetimeError extends SandboxError {}
class CommandExitError extends SandboxError { exitCode: number; stdout: string; stderr: string; error?: string }
class RateLimitError extends SandboxError { retryAfter?: number }
class AuthenticationError extends Error { proxyRejected: boolean; grpcCode?: Code; awsCode?: string }
class QuotaExceededError extends Error { quotaCode?: string }
class CapacityError extends Error {}
```

Every class sets `this.name` to its class name and fixes the prototype
(`Object.setPrototypeOf`) so `instanceof` works after the CJS build.
`translateRpcError(err, { filesystem })` and `translateStreamError(code,
message, { filesystem })` reproduce Python's tables (phase gate →
`SandboxStateError`, kernel gate → `SandboxError`,
`InvalidArgument`/`FailedPrecondition`/`Unimplemented` →
`InvalidArgumentError`, `Unauthenticated`/`PermissionDenied` →
`AuthenticationError` (`proxyRejected` from `isProxyForbidden`), `NotFound`
→ `FileNotFoundError`|`NotFoundError`, `OutOfRange` → `NotFoundError`,
`ResourceExhausted` → `RateLimitError`, `DeadlineExceeded` → `TimeoutError`,
`Canceled` (own abort) → `SandboxError("llamada cancelada por el
cliente")`, else `SandboxError` with `grpcCode`; stream codes `not_found`,
`permission_denied`, `deadline_exceeded`, `unimplemented`,
`invalid_argument`, `suspending`, `output_truncated`). Messages stay in
Spanish like Python's (user-facing strings).

### D17. e2e (`tests/e2e/m6.e2e.test.ts`)

Guard: `RAYITO_E2E === "1"` and `RAYITO_TEMPLATE` set, else
`describe.skipIf`; `AWS_REGION`/`AWS_PROFILE` through the SDK's default
chain; `RAYITO_EXECUTION_ROLE_ARN` turns on `logging: "cloudwatch"`. The
same guardrails as `clients/python/tests/e2e/conftest.py`: every sandbox
is created with `timeoutMs = 900_000` (the M5-style test uses
`1_800_000` and its own `IdlePolicy`), a pre-flight fails with more than
10 live MicroVMs of the template, `afterAll` terminates everything created
(idempotent) and prints the `run-microvm → Health` seconds per sandbox.
Cost ≈ $0.03 per sandbox; the suite uses two sandboxes (`main`, `idle`).

The acceptance test list is at the end of this document. It reproduces
`SPEC.md` §6 (points 1–6 with the image already published: point 1 is
"the template resolves and a MicroVM boots") through the TypeScript SDK,
adds the M1–M5 parity checks that the Python suite makes per milestone
(commands, files, watch, code, PTY) in compact form, and the auto-resume
case. `pnpm test:e2e` runs it; `make test-e2e-typescript` wraps it with
the same variable check as `test-e2e`.

### D18. Public exports (`index.ts`)

`Sandbox`, `Commands`, `CommandHandle`, `Filesystem`, `WatchHandle`,
`Pty`, `PtyHandle`; models `SandboxInfo`, `SandboxListItem`, `IdlePolicy`
(+ `defaultIdlePolicy()`), `HostAccess`, `CommandResult`, `ProcessInfo`,
`PtySize`, `SandboxHealth`, `SandboxMetrics`, `EntryInfo`, `FileType`,
`FilesystemEvent`, `FilesystemEventType`, `WriteEntry`, `CodeContext`,
`OutputMessage`, `Logs`, `ExecutionError`, `Result`, `Execution`,
`OutputChunk`; charts `Chart`, `ChartType`, `ScaleType`, `PointData`,
`LineChart`, `ScatterChart`, `BarData`, `BarChart`, `PieData`, `PieChart`,
`BoxAndWhiskerData`, `BoxAndWhiskerChart`, `SuperChart`; every error class;
`TransportSettings`; `ControlPlane`, `LambdaMicrovmsControlPlane`;
option types (`SandboxCreateOptions`, …, `Logger`); `VERSION`. Generated
types are not re-exported (the wire types are an implementation detail,
as `rayito.v1` is in Python).

### D19. Makefile, CI, ignores, docs

`Makefile`: `limits` (`python scripts/gen_limits.py`), `test-typescript`
(`cd clients/typescript && pnpm install --frozen-lockfile && pnpm typecheck
&& pnpm test`), `lint-typescript` (`pnpm lint`), `test-e2e-typescript`
(guarded like `test-e2e`, `pnpm test:e2e`); `test` and `lint` call the new
targets when `clients/typescript/package.json` exists; `lint` also runs
`python scripts/gen_limits.py --check`. `.github/workflows/ci.yml`: a
`typescript` job (`ubuntu-24.04`, `pnpm/action-setup@v4` with the pinned
pnpm, `actions/setup-node@v4` node 20 with pnpm cache, `pnpm install
--frozen-lockfile`, `pnpm lint`, `pnpm typecheck`, `pnpm build`, `pnpm
test`, `pnpm pack --dry-run`) and the limits check in the `check` job.
`.gitignore`: `node_modules/`, `clients/typescript/dist/`,
`clients/typescript/coverage/`.

Docs: `clients/typescript/README.md` (Spanish, the Python README's
sections and the same example program translated to TypeScript, the
"reading a handle never wakes a sandbox" paragraph, `await using`,
development commands, the local `protoc-gen-es` fallback);
`ARCHITECTURE.md` "TypeScript (`clients/typescript`)" rewritten with the
layout (D3), the transport facts (no `httpVersion`, one session manager
per transport, ping options), the Connect error classification, the
limits pipeline, and the "Plano de control" note that the TypeScript
adapter uses `@aws-sdk/client-lambda-microvms`; `MILESTONES.md` M6: the
track B bullet gains its acceptance state; root `README.md` lists the
TypeScript client; `AWS_API_NOTES.md` only if the e2e measures something
new (then §16).

### D20. Logging allowlist and secrecy

The SDK logs (through the optional `logger`) only: sandbox ids, states,
generations, seconds/ms, byte counts, reason class names, pids, paths of
watch requests, context ids. Never: command output, PTY bytes, file
contents, code, envs, tokens (JWE or access token), `runHookPayload`,
headers. Error messages never embed a token; `AuthenticationError`
messages name the port, not the JWE. A unit test greps every `logger.*`
call site's arguments in the fake-driven flows for the JWE and access
token strings and fails if either appears.

## Risks / Trade-offs

- [Connect-ES surfaces a proxy cut with several `Code`s (`Unavailable`,
  `Aborted`, `Canceled`, `Internal`) depending on where the HTTP/2 session
  dies] → the classifier (D14) is a truth table with a unit test per row,
  and the e2e's pause/resume blocks exercise the real forms (`suspending`
  end, then the session dropped by the freeze); any unclassified form seen
  in the e2e is added to the table with its `rawMessage`.
- [A `Canceled` from our own `AbortController` must not be mistaken for an
  `RST_STREAM CANCEL`] → the classifier requires the `http/2 stream closed`
  prefix and handles check their own `disconnected` flag first; a test
  aborts a stream and asserts no reconnect.
- [`createGrpcTransport` has no reconnect backoff; a dead session is
  retried on the next request] → that is faster than grpc-core's default,
  and the `ReconnectPoll` cadence already bounds probe frequency; no
  channel option to set.
- [HTTP/2 upload window of 64 KiB through the proxy (Q32) applies to
  Node too] → same deadline arithmetic as Python (1 s per MB); the e2e
  writes 1 MB, not 50 MB; the benchmark stays Python-only.
- [`Symbol.asyncDispose` on Node 20.0–20.3] → the E2B polyfill in
  `index.ts` (`Symbol.asyncDispose ??= Symbol.for(...)`), typed through
  `lib: ESNext.Disposable`.
- [Two SDKs, one contract: the TypeScript port can drift from Python in
  a corner (e.g. wake rules)] → the `typescript-sdk` spec restates every
  reconnect scenario and the e2e runs the same §6 flow; `limits.json`
  removes the constants drift; the README example is the same program in
  both languages.
- [`@aws-sdk/client-lambda-microvms` is young (published 2026-06)] → the
  adapter depends only on the seven commands and the paginator, all
  verified in 3.1133.0; a version bump is a one-line change; `aws.test.ts`
  pins the exact command inputs so a shape change fails at unit level.
- [TypeScript 7 (native) is out; 5.9 chosen] → conservative: the toolchain
  (tsdown dts, vitest, biome) is validated on 5.x; revisit in a later
  change, no API impact.
- [`vitest` major (5.x) vs the pinned `^3.2`] → the design permits the
  newest that installs cleanly; the config uses only `projects`,
  `testTimeout`, `hookTimeout`, `fileParallelism`, `vi.useFakeTimers`, all
  stable across 3–5.
- [Windows development box] → `tsdown`, `biome`, `vitest` and the fake
  `http2` server all run on Windows; the e2e is OS-independent; CI runs
  Linux.

## Migration Plan

Additive: a new package, a generated `limits.ts`, and `_limits.py`
regenerated with identical values. No `rayd`, image, proto or Python
behaviour change; the Python version stays 0.0.5. Rollback = delete
`clients/typescript`, `limits.json`, `scripts/gen_limits.py`, restore the
hand-written `_limits.py` header (values identical), remove the Makefile/CI
targets.

## Open Questions

None. Every decision above is closed with the facts verified on
2026-09-16 (BSR reachable; `@aws-sdk/client-lambda-microvms` 3.1133.0
shapes; `@connectrpc/connect(-node)` 2.2.0 option and error forms).

## Acceptance test list (`tests/e2e/m6.e2e.test.ts`)

`describe("m6 typescript sdk", ...)` with one `main` sandbox
(`timeoutMs: 1_800_000`, `idle: { maxIdleSeconds: 600,
suspendedDurationSeconds: 1200, autoResume: true }`, logging per env) and,
in a second test, an `idle` sandbox (`timeoutMs: 900_000`, `idle: {
maxIdleSeconds: 60, suspendedDurationSeconds: 600, autoResume: true }`).
Every block records its seconds with `report(label, s)`.

`test("spec §6 flow through the TypeScript SDK")`:

1. **boot** — `Sandbox.create(...)` resolves; `kernel_ready_s` printed;
   `await sbx.isRunning()` is `true`; `sbx.getHealth()` has `agentReady`,
   `kernelReady`, `resumeGeneration === 0`; `sbx.info.state` is `RUNNING`
   or `PENDING` (never read as readiness); `Sandbox.list({ template })`
   contains `sbx.sandboxId`.
2. **commands parity** — `commands.run("echo hola")` → `stdout === "hola\n"`,
   `exitCode 0`; `commands.run("exit 3")` rejects with `CommandExitError`
   (`exitCode 3`); `commands.run("cat", { stdin: true, background: true })`
   + `sendStdin("ping\n")` + `closeStdin()` → `wait()` stdout `ping\n`;
   `commands.run("sleep 30", { timeoutMs: 1000 })` rejects with
   `TimeoutError` between 1 and 4 s; `commands.run("id -u")` → `1000`;
   `getMetrics()` has `memTotalBytes > 0`; 30 sequential `echo i` succeed
   with no `RateLimitError`.
3. **files parity** — `files.write("/home/user/data.csv", csv)` →
   `EntryInfo.size === csv.length`; `read` text/bytes/stream agree;
   `writeFiles` of 3 files in one call; `list("/home/user", { depth: 2 })`
   contains them; `exists`/`getInfo`/`rename`/`remove`/`makeDir` (`true`
   then `false`); a 1 MB `Uint8Array` round-trips byte-exact;
   `read("/home/user/nope")` rejects with `FileNotFoundError`;
   `watchDir("/home/user/w")` receives a `create` event for a touched file
   within 10 s.
4. **code** (§6.3) — `runCode("import pandas as pd; df =
   pd.read_csv('/home/user/data.csv'); len(df)").text === "2"`;
   `runCode("x = 42")`; `runCode("print(x)")` → `"42"` in
   `logs.stdout.join("")` and `text === undefined`; the matplotlib cell →
   `results[0].png` is a base64 string starting with `iVBOR` and
   `results[0].chart` is defined; `runCode("1/0").error?.name ===
   "ZeroDivisionError"`; a context created with `cwd: "/tmp"` runs
   `import os; os.getcwd()` → `"/tmp"` and is removed.
5. **pty** (§6.4) — `pty.create({ size: { cols: 100, rows: 30 }, onData,
   timeoutMs: 0 })`, `sendInput("echo hola\n")`, read with a bounded wait
   until the regex `[\r\n]hola\r\n` matches (bracketed paste, M5 note);
   `stty size` → `30 100`; `resize({ cols: 120, rows: 40 })` → `40 120`;
   `commands.list()` shows the pid with `kind === "pty"`;
   `commands.sendStdin(pid, ...)` on the PTY rejects with
   `InvalidArgumentError`.
6. **state before pause** — `runCode("y = 7")`; `sleeper =
   commands.run("sleep 4000", { background: true, timeoutMs: 0 })`;
   `timed = commands.run("sleep 60", { background: true, timeoutMs:
   25_000 })`; a `WatchHandle` on `/home/user/w` open; `generationBefore =
   (await sbx.getHealth()).resumeGeneration`.
7. **pause** (§6.5) — `await sbx.pause()` is `true` and `getInfo().state
   === "SUSPENDED"` within 30 s (`pause_s` reported); a second `pause()`
   is `false`; the PTY iterator, `sleeper`, `timed` and the watch are still
   pending (no rejection) 5 s later; a `pause()` issued 30 s before the
   `resume()` means the paused `timed` still has its budget.
8. **resume** — `await sbx.resume()` returns with `getHealth()
   .resumeGeneration === generationBefore + 1` (`resume_s`),
   `kernelStateLost === false`, `|clockOffsetMs| < 5000`.
9. **kernel alive** — `runCode("x").text === "42"`, `runCode("y").text
   === "7"` (`kernel_alive_s`).
10. **processes alive and timeout re-armed** — `commands.connect(sleeper.
    pid)` resolves (`resubscribe_s`), `commands.kill(sleeper.pid)` is
    `true` and `sleeper.wait()` rejects with `CommandExitError` whose
    `exitCode === 137`; `timed.wait()` rejects with `TimeoutError` no
    sooner than 15 s after the resume (the 25 s budget excluded the ~30 s
    pause) (`rearm_timeout_s`).
11. **PTY re-attached** — `sendInput("echo resumed-42\n")` on the original
    handle → `resumed-42` observed through the same iterator, `reconnects
    === 1` (`pty_reattach_s`); `pty.kill(pid)` is `true` then `false`.
12. **watch re-issued** — touching a file in `/home/user/w` produces an
    event on the original `WatchHandle` within 10 s, `isRunning === true`,
    `reconnects === 1`.
13. **runCode across a pause** — start `runCode("import time;
    time.sleep(20); 'slept'")` without awaiting, after 3 s `pause()`, wait
    until `SUSPENDED`, sleep 5 s, `resume()`; the promise resolves with
    `text === "'slept'"` and no `error`; a following `runCode("1+1").text
    === "2"`; generation `+2`.
14. **connect from a second `Sandbox`** — `Sandbox.connect(sbx.sandboxId,
    { accessToken: sbx.accessToken })` → `runCode("x").text === "42"`,
    `commands.run("echo other")`; `close()` on it does not affect `sbx`.
15. **teardown** (§6.6) — `await sbx.kill()` is `true`; `Sandbox.getInfo(id)`
    reports `TERMINATING|TERMINATED` within 30 s and `TERMINATED` within
    120 s; `Sandbox.list({ template })` no longer contains the id; a
    second `Sandbox.kill(id)` is `true` (idempotent).

`test("auto-resume through the TypeScript SDK")`: with the `idle`
sandbox, `runCode("z = 9")`, wait until `getInfo().state === "SUSPENDED"`
(≤ 240 s, polling every 5 s without touching the endpoint), then
`commands.run("echo back")` → `back` within 30 s (`auto_resume_s`),
`runCode("z").text === "9"`, `getHealth().resumeGeneration === 1`,
`getInfo().state === "RUNNING"`; teardown kills it.

Numbers to paste into `MILESTONES.md` M6 track B: `kernel_ready_s` per
sandbox, `pause_s`, `resume_s`, `kernel_alive_s`, `resubscribe_s`,
`rearm_timeout_s`, `pty_reattach_s`, `auto_resume_s`, unit test count,
`pnpm build` output sizes (`dist/index.mjs`, `dist/index.cjs`), and the
`pnpm pack --dry-run` file list.

## ADDED Requirements

### Requirement: The rayito/e2b subpath entry point
The npm package `rayito` SHALL publish a second entry point, `rayito/e2b`, as follows:

- **`package.json`:** `exports["./e2b"]` = `{ "types": "./dist/e2b.d.mts", "import": "./dist/e2b.mjs", "require": { "types": "./dist/e2b.d.cts", "default": "./dist/e2b.cjs" } }`.
- **Build:** `tsdown.config.ts` SHALL build it from `src/e2b/index.ts` next to the main entry.
- **Tarball check:** `scripts/pack-check.mjs` SHALL require `package/dist/e2b.mjs`, `package/dist/e2b.cjs`, `package/dist/e2b.d.mts` and `package/dist/e2b.d.cts` in the tarball.
- **Contract:** the entry SHALL mirror E2B's JS SDK 2.51 so that replacing `from "e2b"` or `from "@e2b/code-interpreter"` with `from "rayito/e2b"` is the only source change an E2B JS program needs.
- **Default export:** `Sandbox`.
- **Named value exports:** `Sandbox`, `E2B`, `ConnectionConfig`, `ALL_TRAFFIC`, `FileType`, `FilesystemEventType`, `Git`, `getSignature`, `Template`, `Volume`, `Secret`.
- **Named error exports:** `SandboxError`, `TimeoutError`, `InvalidArgumentError`, `NotEnoughSpaceError`, `NotFoundError`, `FileNotFoundError`, `SandboxNotFoundError`, `AuthenticationError`, `GitAuthError`, `GitUpstreamError`, `TemplateError`, `RateLimitError`, `ServiceBusyError`, `BuildError`, `FileUploadError`, `CommandExitError`, `UnimplementedError`.
- **Type exports:** `ConnectionOpts`, `SandboxOpts`, `SandboxInfo`, `SandboxMetrics`, `SandboxPaginator`, `Execution`, `Result`, `Logs`, `ExecutionError`, `Context`, `CommandResult`, `CommandHandle`, `EntryInfo`, `WriteInfo`, `FilesystemEvent`, `GitStatus`, `GitBranches`, `GitFileStatus`, `GitResetMode`, `Logger`, `Username`.
- **Aliases:** `NotEnoughSpaceError` SHALL be the native `DiskFullError` binding and `ServiceBusyError` the native `CapacityError` binding. `TemplateError` (extending `SandboxError`) and `BuildError` (extending `Error`) SHALL exist and never be thrown.

#### Scenario: the tarball carries the subpath
- **WHEN** `pnpm build && pnpm pack:check` runs
- **THEN** it exits 0, and the printed listing contains the four `package/dist/e2b.*` entries

#### Scenario: both module systems load the subpath
- **WHEN** a Node 20 script does `import Sandbox, { E2B, NotEnoughSpaceError } from "rayito/e2b"` and another does `const { Sandbox } = require("rayito/e2b")` against the packed tarball
- **THEN** both resolve a class with static `create`, `connect`, `kill`, `getInfo` and `list`, and `NotEnoughSpaceError === DiskFullError` imported from `"rayito"`

### Requirement: E2B JS Sandbox surface in rayito/e2b
`rayito/e2b`'s `Sandbox` SHALL wrap the native `Sandbox` by composition, as `readonly native`.

**Creation and connection options.** `Sandbox.create(opts?)` and `Sandbox.create(template, opts?)` SHALL accept E2B's `SandboxOpts`: `metadata`, `envs`, `timeoutMs` (300 000 when omitted), `secure`, `allowInternetAccess`, `network`, `lifecycle`, `mcp`, `iam` and `volumeMounts`, plus the Rayito keys `maxLifetimeMs`, `templateVersion`, `executionRoleArn`, `allowedPorts`, `ingress`, `logging`, `readyTimeoutMs`, `reconnectTimeoutMs` and `keepOnFailure`. They SHALL also accept E2B's `ConnectionOpts`: `requestTimeoutMs`, `retries`, `logger`, `headers`, `proxy` and `signal`, plus the Rayito binding keys `region`, `controlPlane`, `accessToken` and `transport`. The mapping SHALL be the camelCase mirror of the Python shim:

- `apiKey`, `domain`, `debug`, `apiUrl`, `sandboxUrl`, `apiHeaders` and `secure: false` SHALL each emit one `process.emitWarning(..., { type: "RayitoCompatWarning" })` naming the option and never its value.
- `network.rules`, `network.maskRequestHost`, `network.allowPublicTraffic: true`, `mcp`, `iam`, `volumeMounts` and `lifecycle.onTimeout.keepMemory: false` SHALL reject with `UnimplementedError`.
- An unknown `network` key SHALL reject with `InvalidArgumentError`.

**Static methods:**

- `kill`, `getInfo` and `getFullInfo` (identical to `getInfo`).
- `isRunning(sandboxId)`, true when `get-microvm` answers `RUNNING`.
- `connect(sandboxId, { timeoutMs?, onResume? })`.
- `pause(sandboxId, { keepMemory? })` returning a boolean, and `betaPause` as the same function.
- `setTimeout(sandboxId, timeoutMs)`, `getMetrics(sandboxId, { start?, end? })` returning an array, `list(opts)` returning a `SandboxPaginator`, and `updateNetwork(sandboxId, network)`.

**Instance members:**

- `sandboxId`, `sandboxDomain`, and `trafficAccessToken` (the proxy JWE held for port 8080).
- `files`, `commands` (the native `Commands`), `pty` and `git`.
- `kill`, `getInfo` and `isRunning({ requestTimeoutMs })`, the last as a bounded `Health` probe.
- `setTimeout(timeoutMs)`, `pause`, `betaPause`, `connect({ timeoutMs?, onResume? })` returning `this`, and `getMetrics({ start?, end? })`.
- `uploadUrl(path, opts)` and `downloadUrl(path, opts)`. `uploadUrl()` without a path SHALL throw `InvalidArgumentError`.
- `updateNetwork(network)`.
- `getHost(port): string`, which SHALL return the endpoint hostname synchronously after validating the port with the native rules.
- `getHostHeaders(port): Promise<Record<string, string>>`, the native proxy headers.
- `runCode(code, { signal?, ... })`, `createCodeContext`, `listCodeContexts`, `removeCodeContext` and `restartCodeContext`.
- `[Symbol.asyncDispose]`, which SHALL kill the sandbox.

**Wrappers:**

- `files.watchDir(path, onEvent, { onExit?, recursive?, includeEntry?, allowNetworkMounts?, user?, timeoutMs? })`, where `timeoutMs` is 60 000 when omitted and `allowNetworkMounts` is a no-op.
- `pty.create({ cols, rows, onData, user?, cwd?, envs?, timeoutMs? })` and `pty.connect(pid, { onData, timeoutMs? })`.

**Unimplemented members.** `fork`, `createSnapshot`, `listSnapshots`, `Sandbox.deleteSnapshot`, `connect({ onResume: "reboot" })`, `pause({ keepMemory: false })`, `getMcpUrl`, `getMcpToken`, `getSignature`, every static of `Template`/`Volume`/`Secret`, and the `Template`, `Volume` and `Secret` getters of `E2B` SHALL reject or throw `UnimplementedError`. The reason strings SHALL be identical to the Python ones. Asynchronous members SHALL reject; synchronous members and getters SHALL throw.

**Bound client and integration.** `new E2B(opts).Sandbox` SHALL be a subclass bound to `opts`, with E2B's merge rule. `ConnectionConfig.setIntegration(name)` SHALL feed the control-plane `customUserAgent` of sandboxes created afterwards.

#### Scenario: create with a positional template
- **WHEN** the unit test calls `Sandbox.create("tpl", { metadata: { a: "1" }, apiKey: "e2b_x" })` against the fake control plane and the fake `rayd`
- **THEN** the launch uses template `tpl` and metadata `{ a: "1" }`, and exactly one `RayitoCompatWarning` is emitted, naming `apiKey` and not containing `e2b_x`

#### Scenario: every unimplemented member
- **WHEN** the parametrised unit test exercises each unimplemented member listed above
- **THEN** each throws or rejects with `UnimplementedError`, whose `reason` cites `AWS_API_NOTES.md §` or `SPEC.md §4`, and the fakes record zero requests

#### Scenario: synchronous getHost and E2B-shaped wrappers
- **WHEN** the unit test calls `sbx.getHost(3000)`, `sbx.getHost(9000)`, `sbx.files.watchDir("/w", onEvent)` followed by a write, and `sbx.pty.create({ cols: 80, rows: 24, onData })`
- **THEN** the first returns the endpoint string without awaiting; the second throws `InvalidArgumentError`; `onEvent` receives the write event; and the fake `PtyService` records size `cols=80, rows=24`

#### Scenario: pause returns a boolean
- **WHEN** the unit test calls `sbx.pause()`, then `Sandbox.pause(sbx.sandboxId)` while the stub answers `SUSPENDED`
- **THEN** the first resolves `true` and the second `false`

### Requirement: Connection options in the native TypeScript SDK
The native TypeScript SDK SHALL gain the following options.

**`extraHeaders`:** `TransportSettings.extraHeaders?: Readonly<Record<string, string>>`, set by the interceptor after the four reserved headers on every unary and streaming request. The validation rules SHALL be the Python shim's: the reserved keys and prefixes, `-bin` keys and non-printable-ASCII values SHALL be refused with `InvalidArgumentError` before any request.

**`proxy`:** `TransportSettings.proxy?: string` (an `http://[user:pass@]host:port` URL).

- `openTransport` SHALL open each HTTP/2 session through an HTTP CONNECT tunnel (`ProxyTunnelSocket`, sending `Proxy-Authorization: Basic` when credentials are present), with unchanged TLS verification.
- The control plane SHALL use a `ProxyTunnelAgent` in its `NodeHttpHandler`.
- A non-200 CONNECT answer SHALL fail with `SandboxError` whose message holds the status and never the URL or the credentials.

**`retries` and `integration`:** `ControlPlaneOptions.retries?: number` SHALL set the AWS SDK client option `maxAttempts: retries + 1`, and `ControlPlaneOptions.integration?: string` SHALL set the client option `customUserAgent`. A dedicated control plane SHALL be built when `retries`, `proxy` or `integration` is set.

**`signal`:** `RequestOptions.signal?: AbortSignal`.

- It SHALL be honoured by every unary call (Connect `CallOptions.signal`) and every stream, including `runCode`, `commands.run`, `files` and `pty`. The stream's `AbortController` SHALL be linked to it, so aborting cancels the stream; cancelling an `Execute` interrupts the execution.
- It SHALL be honoured by `SandboxCreateOptions`, for the readiness poll and every control-plane `send` (`abortSignal`).
- An abort SHALL reject with `signal.reason`.

**Accessors:** `Sandbox.currentProxyToken(port = 8080)` SHALL return the JWE held in the token store synchronously, or `undefined`. `Sandbox.isRunning({ requestTimeoutMs })` SHALL bound its `Health` probe.

#### Scenario: extra headers on the wire and reserved keys refused
- **WHEN** a sandbox created with `transport: { extraHeaders: { "X-Trace": "1" } }` calls `commands.list()` and `commands.run("sleep 1", { background: true })`, and another create passes `extraHeaders: { "x-access-token": "x" }`
- **THEN** the fake `rayd` sees `x-trace: 1` on both requests, and the second create rejects with `InvalidArgumentError` before any control-plane call

#### Scenario: CONNECT tunnel
- **WHEN** the unit test opens a `ProxyTunnelSocket` through a local `net` server acting as proxy with `http://u:p@127.0.0.1:<port>`, and the server first answers `HTTP/1.1 200` and then, on a second tunnel, `HTTP/1.1 407`
- **THEN** the server records `CONNECT <host>:443 HTTP/1.1` with a `Proxy-Authorization` header; bytes written after the 200 reach the server unchanged; and the second tunnel fails with a message containing `407` and not containing `p`

#### Scenario: AbortSignal cancels runCode
- **WHEN** the unit test aborts an `AbortController` while the fake `CodeService` streams a long `Execute`
- **THEN** `runCode` rejects with the controller's reason, the fake records the stream cancellation, and a signal aborted before the call rejects without any request

#### Scenario: retries land in the client configuration
- **WHEN** a control plane is built with `retries: 2` and `integration: "acme/1.0"`
- **THEN** the AWS SDK client configuration has `maxAttempts === 3` and a custom user agent containing `acme/1.0`

### Requirement: DiskFullError and UnimplementedError in the native TypeScript SDK
The native `src/errors.ts` SHALL add the following classes, all exported from `"rayito"` and re-exported by `"rayito/e2b"`:

- `DiskFullError extends SandboxError`. The gRPC error translation SHALL return it for `ResourceExhausted` whose details contain `disk_reserve` or `disk_full`. Any other `ResourceExhausted` SHALL stay `RateLimitError`.
- `UnimplementedError extends Error`, with `feature` and `reason` and a message in the Python format.
- `GitAuthError extends AuthenticationError` and `GitUpstreamError extends SandboxError`.
- `TransferError extends SandboxError` with `code` and `reason`, and `FileUploadError extends TransferError`.

`m9-file-transfer` defines `DiskFullError`, `UnimplementedError`, `TransferError` and `FileUploadError`. This requirement holds whichever change lands first, and it adds the `ResourceExhausted` translation rule for `Write`.

#### Scenario: disk full is distinguished
- **WHEN** the fake `rayd` rejects a `files.write` with `RESOURCE_EXHAUSTED` and details `disk_reserve`, and another call with `RESOURCE_EXHAUSTED` and details `rate`
- **THEN** the first rejects with `DiskFullError`, an instance of `SandboxError`, and the second with `RateLimitError`

### Requirement: E2B JS surface accepted against real AWS
The e2e `tests/e2e/m9-e2b.e2e.test.ts` SHALL build the package and then run, with `node` against a real MicroVM of the M9 `rayito-base` image, the corpus under `tests/e2e/e2b-corpus/*.mjs`. Each program SHALL import from `"rayito/e2b"` (resolved by package self-reference) and SHALL differ from an E2B JS program only in that import. The corpus SHALL cover:

- `Sandbox.create(template, opts)` and `runCode`
- the static `getInfo`, `kill` and `list`
- a synchronous `getHost` hostname
- `files.watchDir(path, onEvent)` receiving an event
- `pty.create({ cols, rows, onData })` with `sendInput`
- `git.clone` + `status`
- `pause()` returning `true` then `false`
- `new E2B().Sandbox.create()`
- an `AbortController` cancelling a long `runCode`

#### Scenario: JS corpus on AWS
- **WHEN** the e2e runs with `RAYITO_E2E=1`, `RAYITO_TEMPLATE` pointing at the M9 image and `RAYITO_ACCESS_TOKEN` set
- **THEN** every program exits 0; the aborted `runCode("import time; time.sleep(60)")` rejects within 10 s with the abort reason; a following `runCode("1+1")` on the same sandbox answers `2`; and the output prints the per-program wall time

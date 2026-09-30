## MODIFIED Requirements

### Requirement: Code execution surface
`sandbox.runCode(code, { language, context, onStdout, onStderr, onResult, onError, envs, timeoutMs = 300 000, requestTimeoutMs })` SHALL normalise `language` (case-insensitive; `js` → `javascript`, `ts` → `typescript`; `undefined`/`""` → not sent; anything outside `python`, `bash`, `javascript`, `typescript` → `InvalidArgumentError` before any call), throw `InvalidArgumentError` when both `language` and `context` are given, set `ExecuteRequest.language` only when a language was given, open `Execute` on the unary transport with `timeout_ms = round(timeoutMs)` and the stream deadline `timeoutMs + 15 000` (none for `0`/`undefined`; `requestTimeoutMs` replaces it), feed every event into `Execution{ results, logs{ stdout[], stderr[] }, error?, executionCount? }` (`keepalive` ignored; `onStdout`/`onStderr` receive `OutputMessage{ line, timestamp, error }`; `onResult` a `Result`; `onError` an `ExecutionError`), and resolve it; `Execution.text` SHALL be the `text` of the result with `isMainResult`, else `undefined`; kernel errors SHALL be data in `error`, never rejections; `toJSON()` SHALL produce the Python shape. `Result` SHALL expose `text, html, markdown, svg, png, jpeg, pdf, latex, json, javascript, data, chart, isMainResult, extra, raw` and `formats()`; `json` and `data` SHALL be parsed JSON (raw string kept on failure); `chart` SHALL parse into `LineChart`, `ScatterChart`, `BarChart`, `PieChart`, `BoxAndWhiskerChart`, `SuperChart` or a `Chart` of type `"unknown"`. Leaving before `end` (callback throw, abort) SHALL cancel the stream. `createCodeContext({ cwd, language, envs })` SHALL accept the same language names (normalised the same way, `undefined` → `python`), and with `listCodeContexts()`, `removeCodeContext(ctx)` and `restartCodeContext(ctx)` SHALL accept a `CodeContext` or its id, use 90 s default deadlines for create/restart, and map `NotFound` → `NotFoundError`, `FailedPrecondition` → `InvalidArgumentError`, `Unimplemented` → `UnimplementedError` whose `feature` is `runCode({ language: <language> })` or `createCodeContext({ language: <language> })` (just the bare method name when the caller gave no `language`, even though `createCodeContext` sends `python` by default, mirroring the Python SDK's `code_feature`) and whose `reason` is the agent's raw message alone, without the shared image-update hint (a language the image does not ship: the message already names `rayito-base-poly`, and the image's `rayd` is current).

#### Scenario: acceptance sequence against the fake
- **WHEN** the SDK runs `x = 42`, `x`, `print(x)`, the plot cell, `1/0` and `slow 10` with `timeoutMs 2000`
- **THEN** `runCode("x").text === "42"`, `"42"` is in `logs.stdout.join("")` of `print(x)`, the plot's `results[0].png` and `chart` are defined and `formats()` equals `["png", "chart"]`, `error.name === "ZeroDivisionError"` with `executionCount` set, and the last `error.name === "ExecutionTimeout"`

#### Scenario: callbacks receive typed messages
- **WHEN** `print('a'); print('b')` runs with `onStdout: seen.push`
- **THEN** every element of `seen` has `error === false`, a positive `timestamp`, and their `line`s joined contain `a` and `b`

#### Scenario: contexts
- **WHEN** `ctx = await createCodeContext({ cwd: "/tmp" })`, `runCode("2*2", { context: ctx })`, `listCodeContexts()`, `removeCodeContext(ctx.id)`, then `removeCodeContext("default")`
- **THEN** the result text is `"4"`, the list had `default` first and `ctx` after, removal resolved, and removing `default` rejects with `InvalidArgumentError`

#### Scenario: language routing against the fake
- **WHEN** `runCode("echo hi", { language: "Bash" })`, `runCode("1 + 1", { language: "js" })`, `runCode("x")` and `createCodeContext({ language: "bash" })` run against the fake `rayd`
- **THEN** the first two requests carried `language` `"bash"` and `"javascript"`, the third carried no `language`, the fake's `ListContexts` shows `default-bash` and `default-javascript` with their languages after the cells, and the created context has `language === "bash"`

#### Scenario: invalid language combinations
- **WHEN** `runCode("1", { language: "r" })` and `runCode("1", { language: "bash", context: "default" })` are called
- **THEN** both reject with `InvalidArgumentError` and the fake received no `Execute`

#### Scenario: language not shipped
- **WHEN** the fake `rayd` answers `Execute{language: "javascript"}` with `Unimplemented` and a message containing `rayito-base-poly`
- **THEN** `runCode` rejects with `UnimplementedError` (not `InvalidArgumentError`, not `SandboxError`) whose `feature` is `runCode({ language: "javascript" })` and whose message contains `rayito-base-poly`

#### Scenario: typescript routing against the fake
- **WHEN** `runCode("1", { language: "ts" })`, `runCode("1", { language: "TypeScript" })` and `createCodeContext({ language: "ts" })` run against the fake `rayd`, and then `runCode("1", { language: "tsx" })`
- **THEN** the first three requests carried `language` `"typescript"`, the fake's `ListContexts` shows `default-typescript`, the created context has `language === "typescript"`, and the last rejects with `InvalidArgumentError` with no `Execute` reaching the fake

#### Scenario: Deno language not shipped
- **WHEN** the fake `rayd` answers `Execute{language: "typescript"}` with `Unimplemented` and a message containing `rayito-base-poly`
- **THEN** `runCode` rejects with `UnimplementedError` whose `feature` is `runCode({ language: "typescript" })` and whose message contains `rayito-base-poly`

### Requirement: Error hierarchy under E2B's JavaScript names
The package SHALL export `SandboxError` (root; `statusCode?`, `grpcCode?`, `awsCode?`) with subclasses `TimeoutError`, `InvalidArgumentError`, `NotFoundError` (← `FileNotFoundError`, `SandboxNotFoundError`), `SandboxNotReadyError` (`state?`, `stateReason?`), `SandboxStateError`, `SandboxLifetimeError`, `CommandExitError` (`exitCode`, `stdout`, `stderr`, `error?`), `RateLimitError` (`retryAfter?`), and outside the hierarchy `AuthenticationError` (`proxyRejected`), `QuotaExceededError` (`quotaCode?`), `CapacityError` and `UnimplementedError` (`feature`, `reason`, `doc?`, `cause?`) with its own subclass `LifecycleUnsupportedError` (`sandbox-timeout`); `instanceof` SHALL work in both ESM and CJS builds and `name` SHALL equal the class name. gRPC translation SHALL follow: phase gate → `SandboxStateError`; kernel gate → `SandboxError`; `InvalidArgument`, `FailedPrecondition` → `InvalidArgumentError`; `Unimplemented` → `UnimplementedError` through one shared helper (`unimplementedRpcError`), with `feature` defaulting to `GENERIC_RPC_FEATURE` ("esta llamada", the same generic feature as the Python SDK's `unimplemented_rpc_error`), `reason` the raw gRPC message followed by `"; " + UNIMPLEMENTED_IMAGE_HINT` ("publica una imagen con una versión actual de rayd", identical text and position to Python's `UNIMPLEMENTED_IMAGE_HINT`) and `cause` the `ConnectError` (a caller that knows which RPC failed, such as `runCode`/`createCodeContext`'s missing-kernel case, SHALL pass its own `feature` to the same helper instead of the generic one; the image-update hint applies only to the generic feature, so the missing-kernel case passes no hint and its `reason` is the raw gRPC message alone, as in Python); `Unauthenticated` → `AuthenticationError`; `PermissionDenied` → `AuthenticationError` with `proxyRejected` when `rawMessage === "HTTP 403"`; `NotFound` → `FileNotFoundError` (filesystem) or `NotFoundError`; `OutOfRange` → `NotFoundError`; `ResourceExhausted` → `RateLimitError`; `DeadlineExceeded` → `TimeoutError`; `Canceled` from the client's own abort → `SandboxError`; else `SandboxError` with `grpcCode`. `UnimplementedError` and its subclasses SHALL never carry `grpcCode` (it is not a `SandboxError`); the failing RPC's code SHALL only be reachable through `cause`. In-stream `StreamError.code` SHALL map `not_found`, `permission_denied`, `deadline_exceeded`, `unimplemented`/`invalid_argument`, `suspending`, `output_truncated` as in Python (unchanged: the in-stream `unimplemented` code still maps to `InvalidArgumentError`, a closed protocol vocabulary distinct from the gRPC status table above).

#### Scenario: translation table
- **WHEN** `translateRpcError` receives a hand-made `ConnectError` for each code in the table
- **THEN** every code except `Unimplemented` yields the listed class with `grpcCode` equal to the input code; `Unimplemented` yields `UnimplementedError` (never `SandboxError` or `InvalidArgumentError`) whose `feature` is `GENERIC_RPC_FEATURE`, whose `reason` is the `ConnectError`'s raw message followed by `UNIMPLEMENTED_IMAGE_HINT`, and whose `cause` is that same `ConnectError`; and `new ConnectError("HTTP 403", Code.PermissionDenied)` yields `AuthenticationError` with `proxyRejected === true` while `new ConnectError("EACCES", Code.PermissionDenied)` yields `proxyRejected === false`

#### Scenario: instanceof survives the CJS build
- **WHEN** the CJS bundle is required and a `FileNotFoundError` is thrown
- **THEN** `err instanceof NotFoundError`, `err instanceof SandboxError` and `err.name === "FileNotFoundError"` hold

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
- Every static method SHALL apply the full `ConnectionOpts` of its call (it builds its own channel and plane): no connection option of a static call SHALL ever emit an instance-only unapplied-option warning.

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
- **Unapplied instance connection options.** An instance call's channel and plane are already built from the opts given at `create`/`connect` time; a `ConnectionOpts` key on the call itself (`headers`, `proxy`, `retries`, `logger`, `region`, `controlPlane`, `accessToken`, `transport`) that the given method does not forward to the native call (only `signal`, and for every method but `kill`/`pause` also `requestTimeoutMs`, reach the native layer) SHALL emit one `process.emitWarning(..., { type: "RayitoCompatWarning" })` per key, in alphabetical order (the same rule as the Python shim), naming only the key and never its value (an empty `headers` object is not "given" and SHALL NOT warn, like Python's `headers={}`), with a reason naming the method and `Sandbox.<method>(sandboxId, ...)` as the alternative that does apply it. A key already covered by the always-ignored list above (`apiKey`, `domain`, ...) SHALL NOT also emit this warning.

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

#### Scenario: an instance call warns for the connection opts it drops, never their value
- **WHEN** the unit test calls `sbx.kill({ retries: 3, proxy: "http://u:pass@h:1" })` on an existing sandbox, with `process.emitWarning` spied
- **THEN** exactly two `RayitoCompatWarning`s are emitted, naming `proxy` and `retries` and neither containing the proxy's password; `sbx.setTimeout(ms, { requestTimeoutMs })` emits none (it is applicable); and the equivalent static calls (`Sandbox.kill(sandboxId, { headers, region, controlPlane })`) emit none

#### Scenario: an empty headers object is not "given"
- **WHEN** the unit test asks which of `{ headers: {}, retries: 3 }` an instance call with no applicable keys drops
- **THEN** only `retries` is reported, never `headers`

## 0. [pre-flight] Facts and scaffolding

- [x] 0.1 Re-verify from the implementer's box: `pnpm view rayito version` is 404, `pnpm view @aws-sdk/client-lambda-microvms version` ≥ 3.1133.0, `pnpm view @connectrpc/connect-node version` ≥ 2.2.0; record the versions actually resolved in the "Notes" section at the end of this file
- [x] 0.2 `buf generate` from the repo root regenerates `clients/python/src/rayito/v1/*` unchanged and creates `clients/typescript/src/gen/rayito/v1/{code,common,filesystem,health,process,pty}_pb.ts` (BSR reachable, design D2); if the BSR is unreachable, add `pnpm add -D @bufbuild/protoc-gen-es@2.15.0` and a `buf.gen.local.yaml` with `local: ["pnpm", "--dir", "clients/typescript", "exec", "protoc-gen-es"]`, generate, and confirm the output is byte-identical to a BSR run on CI
- [x] 0.3 Scaffold `clients/typescript/`: `package.json` (D1 fields, scripts, `packageManager`, `engines`, `exports`, `files`), `tsconfig.json`, `tsdown.config.ts`, `biome.json` (excludes `src/gen/**`), `vitest.config.ts` (`unit` and `e2e` projects), `LICENSE` (MIT, same as Python), `.npmrc` empty or absent; `pnpm install` produces `pnpm-lock.yaml`; `.gitignore` gains `node_modules/`, `clients/typescript/dist/`, `clients/typescript/coverage/`
- [x] 0.4 `src/index.ts` with the `Symbol.asyncDispose` polyfill and an empty export list; `pnpm build`, `pnpm typecheck`, `pnpm lint`, `pnpm test` (0 tests) all exit 0 on the Windows box

## 1. [limits] One JSON source for both SDKs (design D7)

- [x] 1.1 Create `limits.json` at the repo root with every constant of `clients/python/src/rayito/_limits.py` under the camelCase keys listed in `specs/sdk-limits/spec.md`, same values
- [x] 1.2 `scripts/gen_limits.py` (stdlib only, `ruff check scripts` clean): render `_limits.py` (existing constant names, `Final`, tuples, `frozenset`s, `dict[str, int]`, generated header + the API sections docstring) and `clients/typescript/src/limits.ts` (`export const UPPER_SNAKE = … as const`, `ReadonlySet<string>`, `Readonly<Record<string, number>>`, generated header); `--check` diffs in memory and exits 1 on drift; deterministic output
- [x] 1.3 Run the generator; `git diff`-equivalent check that `_limits.py` changed only in its header/formatting and every value is identical; `uv run pytest tests/unit` (429) still green; `uv run ruff check .` and `ruff format --check .` clean on the rendered file (the generator formats to ruff's style)
- [x] 1.4 `clients/python/tests/unit/test_limits.py`: load `limits.json` (skip if absent), assert every `rayito._limits` constant equals the JSON (camelCase → UPPER_SNAKE mapping in the test); `clients/typescript/tests/unit/limits.test.ts`: the same for `limits.ts` importing the JSON with an import attribute
- [x] 1.5 `Makefile`: `limits` target and `python scripts/gen_limits.py --check` in `lint`; CI `check` job runs the check

## 2. [sdk] Pure modules (no I/O)

- [x] 2.1 `src/errors.ts`: the hierarchy of design D16 with `name` and prototype fixes; `tests/unit/errors.test.ts` (`instanceof` chain, fields, `name`)
- [x] 2.2 `src/models.ts`: `IdlePolicy` + `defaultIdlePolicy()` + `validateIdlePolicy`, `SandboxInfo` mapper with `expiresAt`/`remainingSeconds`/`templateName`/`endpointUrl`, `SandboxListItem`, `HostAccess` class, `PtySize` + `validatePtySize`, `validatePort`, `CommandResult`, `ProcessInfo`, `SandboxHealth`, `SandboxMetrics`, `FileType`, `EntryInfo`, `FilesystemEventType`, `FilesystemEvent`, `WriteEntry`/`WriteData`, `CodeContext`, `OutputMessage`, `Logs`, `ExecutionError`, `Result` (fields, `formats()`, `RESULT_FORMAT_ORDER`), `Execution` (`text`, `toJSON()`), `OutputChunk`; `tests/unit/models.test.ts`
- [x] 2.3 `src/charts.ts`: port `_charts.py` (`ChartType`, `ScaleType`, `Chart`, `PointData`, `LineChart`, `ScatterChart`, `BarData`, `BarChart`, `PieData`, `PieChart`, `BoxAndWhiskerData`, `BoxAndWhiskerChart`, `SuperChart`, `parseChart` tolerant of malformed input); `tests/unit/charts.test.ts` with the same fixtures as `clients/python/tests/unit/test_charts.py` (copy the JSON documents)
- [x] 2.4 `src/payload.ts`: `generateAccessToken`, `encodeAccessToken`, `decodeAccessToken` (canonical base64url, `InvalidArgumentError`), `validateAccessToken`, `accessTokenSha256`, `buildRunHookPayload` (sorted compact JSON, `envs` only when non-empty, 4096 cap with the Spanish message), `validatedEnvs`; `tests/unit/payload.test.ts` including a vector shared with the Python test (`ACCESS_TOKEN_SECRET` → known hash)
- [x] 2.5 `src/sandbox/launch.ts`: `resolveTemplate` (`RAYITO_TEMPLATE`), `resolveAccessToken`/`requireAccessToken` (`RAYITO_ACCESS_TOKEN`), `validateSandboxId` (length 1–256), `validateTimeoutMs` (ceil to seconds; `SandboxLifetimeError` above 28 800 000, `InvalidArgumentError` below 1000/non-integer), `resolveIdlePolicy`, `proxyPortSpecs` (8080 first, 9000 refused, ranges), `validateHostPort`, `connectorArns` (managed names → ARN, `arn:` pass-through, ≤ 10), `loggingConfig`, `buildLaunchPlan` → `{ accessToken, request: LaunchRequest, proxyPorts }`; `tests/unit/launch.test.ts`
- [x] 2.6 `src/sandbox/readiness.ts`: `ReadinessPoll` (0.25→2 s, `stateCheckIntervalMs 5000`, `rpcTimeoutMs` clamp 0.5–5 s), `ReconnectPoll` (0.5→4 s, ±25 % with injectable `random`), `ReconnectOutcome`, `ReconnectBudget` (3), `alreadySuspended`, `isSuspendingReason`, `reconnectFailure(reason, { info, timeoutMs, wake })`, `healthFromProto`, `healthReconnected`, `notReadyError`, `terminalStateError`, `terminatedDuringBootError`; `tests/unit/readiness.test.ts` with a fixed `random` (delays 0.5, 1, 2, 4, 4 within ±25 %) and the failure matrix

## 3. [sdk] Transport and control plane

- [x] 3.1 `src/transport/headers.ts`: header constants and `proxyAuthInterceptor(store, { port, accessToken })` (throws `AuthenticationError` without a token); `src/transport/tokens.ts`: `ProxyToken`, `TokenStore`, `TokenRefresher` (unref'd timer chain, 45 min / 60 s retry, warning without the JWE, `stop()` idempotent); `tests/unit/tokens.test.ts` with `vi.useFakeTimers()` (schedule, retry, `refreshAll`, `ensure` reuse)
- [x] 3.2 `src/transport/transport.ts`: `TransportSettings` defaults (`https`, 443, ping options, 64 MiB) and `openTransport(host, settings, interceptor)` → `createGrpcTransport` (no `httpVersion`; design D4); `src/transport/errors.ts`: `isProxyForbidden`, `isPhaseGate`, `isKernelGate`, `isStreamReset`, `isReconnectable`, `isOwnAbort`, `translateRpcError({ filesystem })`, `translateStreamError`; `tests/unit/transport-errors.test.ts` with the full truth table of the reconnection requirement and the translation table (hand-made `ConnectError`s with `rawMessage` and cause chains)
- [x] 3.3 `src/aws/control-plane.ts`: `PortSpec` (`single`, `range`, `covers`, `toApi`; no `allPorts`), `LaunchRequest.toApi()` with exactly the `AWS_API_NOTES.md` field names, `ControlPlane` interface, `TokenBucket` (injectable `now`/`sleep`), `LambdaMicrovmsControlPlane` (`CommandSender` injection, `fromRegion`, lazy STS, `invoke` through the buckets, `paginateListMicrovms` with `pageSize 50` and client-side state filter, `authToken` key handling), `sharedControlPlane(region)`, `translateAwsError` by `name`, `sandboxInfoFromResponse` (endpoint normalised), `sandboxListItemFromResponse`, `idlePolicyToApi`/`FromResponse`; `tests/unit/aws.test.ts` with a recording `CommandSender` covering the five scenarios of the control-plane requirement (exact `RunMicrovmCommand` input, error names, 2 TPS sleeps `0, 0, 0.5, 1.0, 1.5`, pagination + filters, ARN resolution cached once)
- [x] 3.4 `pnpm typecheck && pnpm lint && pnpm test` green after sections 2–3

## 4. [sdk] Fake rayd and fake control plane (design D15)

- [x] 4.1 `tests/unit/fake/server.ts`: `startFakeRayd({ accessToken })` → `http2.createServer(connectNodeAdapter({ routes }))` on `127.0.0.1:0`, header assertions on every request (`x-aws-proxy-port "8080"`, `force-h2 "true"`, non-empty JWE), token hash check on every RPC except `Health`, session counter (for the "two transports" scenario), `close()`
- [x] 4.2 `tests/unit/fake/health.ts` (scripted readiness/generation/`unavailableCalls`, `Metrics`), `fake/process.ts` (programs `echo`, `exit N`, `sleep N`, `cat`, `big N`, `slow`; rings with `seq`; `Connect(from_seq)` + `OutOfRange`; `SendInput`/`CloseStdin`/`SendSignal`/`List`; `connectCalls`; `suspend()`/`resume()` with the `suspending` end and the phase gate), `fake/filesystem.ts` (in-memory tree; `Read` 256 KiB chunks; `Write` client stream with per-file entries and a denied path; `Stat`, `ListDir(depth)`, `MakeDir` `AlreadyExists`, `Move`, `Remove`; `WatchDir` with `emit()`, `watchCalls`, `suspend()`), `fake/code.ts` (contexts with `default` first; cells `x = 42`, `x`, `print(x)`, plot → `png` + `chart`, `1/0`, `slow N` with `ExecutionTimeout`; execution ring; `Reattach` with `reattachCalls`, `NotFound`/`OutOfRange`; `suspend()`; kernel gate scripting), `fake/pty.ts` (echo shell, `stty size`, `exit N`, `Resize` recorded, `Kill` → 137, `Connect(from_seq)` ring, `suspend()`), `fake/index.ts` `suspendResume({ unavailableCalls })`
- [x] 4.3 `tests/unit/fake/control-plane.ts`: `FakeControlPlane implements ControlPlane` with a state queue for `getMicrovm`, recorded calls (`runMicrovm`, `terminateMicrovm`, `suspendMicrovm`, `resumeMicrovm`, `createAuthToken` with ports), scripted `ConflictException` behaviour, `templateArn`; `tests/unit/helpers.ts`: `createTestSandbox()` wiring the fake plane, the fake `rayd` (`transport: { scheme: "http", port }`), a recording `logger`, `readUntil(handle, needle, timeoutMs)`, `nextChunk(iterator, timeoutMs)`
- [x] 4.4 `tests/unit/gen.test.ts` (generated-surface guard) and a smoke test that a `Sandbox` created through the helpers answers `isRunning()` and that a wrong access token yields `AuthenticationError`

## 5. [sdk] Sandbox, commands, files, PTY, code (designs D9–D13)

- [x] 5.1 `src/sandbox/sandbox.ts`: constructor (transports, clients per service memoised, store/refresher, `resumeGeneration`, `paused`, `pendingReconnect`, `resumedSignal`, live stream registry), `create`/`connect`/`open` (readiness poll, terminate-on-failure rules), static `list`/`kill`/`getInfo`/`pause`/`resume`, instance `kill`/`getInfo`/`pause`/`resume`/`isRunning`/`getHealth`/`getHost`/`getMetrics`/`close`/`[Symbol.asyncDispose]`, `callUnary` (403 re-mint once, reconnect retry once), `openStream` (first message consumed, 403 re-mint, reconnect retry unless `reconnect: false`), `recordHealth` (generation, `kernelStateLost` info, clock-offset warning, wake dormant waiters, clear `paused`), `reconnect(reason, seenGeneration, { wake })` per design D14 (shared in-flight poll, dormant path with 5 s state checks and fresh budget), `Commands`/`Filesystem`/`Pty`/`CodeClient` wiring
- [x] 5.2 `src/sandbox/commands.ts`: `buildStartRequest`, `timeoutToMs`, `streamDeadlineMs`, `validatePid`, `validateFromSeq`, `encodeStdin`, `OutputAccumulator` (streaming `TextDecoder`), `CommandProgress` + `outcomeFromEnd` (M2 table), `StreamAdapter`/`ProcessEvents`, `Commands` (`run` overloads, `connect`, `list`, `kill`, `sendStdin`, `closeStdin`), `CommandHandle` (lazy async iterator, `wait`, `kill`, `disconnect`, `sendStdin`, `closeStdin`, reconnect path with `Connect(fromSeq = lastSeq + 1)`, `OutOfRange` → `fromSeq 0` + warning, `NotFound` stored, `ReconnectBudget`, `wakes` rule)
- [x] 5.3 `src/sandbox/pty.ts`: `buildPtyStartRequest`, `validateShell`, `PtyMessages` adapter (`endEventFromPtyExited`), `Pty` (`create`, `connect`, `sendInput` + `sendStdin` alias, `resize`, `kill`), `PtyHandle extends CommandHandle` (`sendInput`, `resize`, `Pty.Connect` re-subscribe, `{ pty }` chunks, `onData`)
- [x] 5.4 `src/sandbox/filesystem.ts`: validators (`validatePath`, `validateMode`, `validateDepth`, `validateReadFormat`), `fileRequestDeadlineMs`, `materialiseWriteData` (string/`Uint8Array`/`ArrayBuffer`/`Blob`/`ReadableStream`), `buildWriteRequests` (1 MiB chunks, header only on the first message), proto → `EntryInfo`/`FilesystemEvent` mappers (`modifiedTime` from ms), `Filesystem` (`read` overloads with `Stat` first and strict UTF-8, `write`, `writeFiles`, `list`, `exists`, `getInfo`, `remove`, `rename`, `makeDir` with `AlreadyExists` → `false`, `watchDir` resolving after `WatchStarted`), `WatchHandle` (detached consumer, `WatchState`, `getNewEvents`, `stop` ≤ 5 s, `[Symbol.asyncDispose]`, re-issue on reconnect with `wake: false`, `onExit` semantics), `Sandbox.close()` stops every handle
- [x] 5.5 `src/sandbox/code.ts`: `validateCode` (≤ 1 MiB), `resolveContextId`/`requireContextId`, `validateLanguage`, `validateCwd`, `buildExecuteRequest`, `buildCreateContextRequest`, `executeDeadlineMs`, `buildReattachRequest`, `resultFromProto` (mime fields, `json`/`data` parsed, `chart` via `parseChart`, `extra`, `raw`), `errorFromProto`, `contextFromProto`, `ExecutionBuilder` (callbacks, `executionId`, `lastSeq`, `reattached`), `CodeClient` (`runCode` with cancel-on-early-exit and the `Reattach` path after `started`, `createContext` 90 s, `listContexts`, `removeContext`, `restartContext` 90 s)
- [x] 5.6 `src/index.ts`: the export list of design D18 plus `VERSION` read from `package.json` at build time (tsdown `define`) or a `version.ts` constant kept equal to `package.json` by a unit test
- [x] 5.7 `tests/unit/sandbox.test.ts`, `commands.test.ts`, `filesystem.test.ts`, `pty.test.ts`, `code.test.ts`: every scenario of the lifecycle, commands, filesystem, PTY, code, error-hierarchy, 403-re-mint and transport requirements in `specs/typescript-sdk/spec.md` (including "two transports at most" through the fake's session counter, deadline arithmetic through the fake's observed `grpc-timeout`, `await using`, `getHost` headers, static variants, `Sandbox.list` filters)
- [x] 5.8 `tests/unit/reconnect.test.ts`: every scenario of the reconnection requirement (classification table already in 3.2; background handle survives, unread handle, `OutOfRange` fallback, PTY + watch re-attach, `runCode` reattach, cut before `started`, unary retried once, terminated during the poll, suspended without auto-resume, dormant handle sleeps, `resume()` wakes dormant handles, dormant handle does not block a foreground call, explicit pause keeps foreground streams dormant, concurrent reconnects share one poll, `disconnect()` never reconnects, `close()` ends waits, futile-reconnect budget) with fake timers or short injected delays so the file runs in < 20 s
- [x] 5.9 `tests/unit/logging.test.ts`: recording `logger` across the fake-driven flows; assert no logged string contains the JWE, the access token or the payload; `tests/unit/readme-example.test.ts`: the README example compiled (type-only test)
- [x] 5.10 `pnpm lint`, `pnpm typecheck`, `pnpm build`, `pnpm test` green (≥ 250 tests, < 60 s); `pnpm pack --dry-run` lists `dist/**`, `README.md`, `package.json`, `LICENSE` only; a scratch script `require("./dist/index.cjs")` and `import("./dist/index.mjs")` both expose `Sandbox`

## 6. [docs + build] README, Makefile, CI, architecture

- [x] 6.1 `clients/typescript/README.md` (Spanish): status paragraph, the Python README example translated (same program, `await using`, `for await` on the PTY, `pause()`/`resume()`, `getHealth().resumeGeneration`), the "reading a handle never wakes a sandbox" paragraph, `Desarrollo` (pnpm commands, `make proto`, the local `protoc-gen-es` fallback, e2e variables)
- [x] 6.2 `Makefile`: `test-typescript`, `lint-typescript`, `test-e2e-typescript` (same variable guard as `test-e2e`), `test`/`lint` call them when `clients/typescript/package.json` exists; `.github/workflows/ci.yml`: `typescript` job (pnpm/action-setup pinned to `packageManager`, Node 20 with pnpm cache, `pnpm install --frozen-lockfile`, `pnpm lint`, `pnpm typecheck`, `pnpm build`, `pnpm test`, `pnpm pack --dry-run`) and the limits `--check` in `check`
- [x] 6.3 `ARCHITECTURE.md` "TypeScript (`clients/typescript`)": layout (D3), transport facts (HTTP/2-only `createGrpcTransport`, one session manager per transport, ping options, no reconnect backoff to cap), the Connect error forms and the classification table, the limits pipeline, a line in "Plano de control" naming `@aws-sdk/client-lambda-microvms`; root `README.md`: the TypeScript client exists; `SECURITY.md` unchanged (verify no new row is needed)

## 7. [e2e] Acceptance against real AWS (design D17)

- [x] 7.1 `clients/typescript/tests/e2e/helpers.ts`: guard (`RAYITO_E2E`, `RAYITO_TEMPLATE`, `RAYITO_EXECUTION_ROLE_ARN` → `logging "cloudwatch"`), pre-flight (≤ 10 live MicroVMs of the template via `Sandbox.list`), `report(label, s)`, `waitUntil(predicate, budgetMs)`, `readUntil(handle, regex, timeoutMs)` with a bounded wait per chunk (bracketed-paste regex `[\r\n]hola\r\n`, M5 note), `afterAll` sweeper terminating every created id (idempotent)
- [x] 7.2 `clients/typescript/tests/e2e/m6.e2e.test.ts`: `test("spec §6 flow through the TypeScript SDK")` with blocks 1–15 and `test("auto-resume through the TypeScript SDK")` exactly as in design D17 "Acceptance test list"; `vitest.config.ts` `e2e` project timeouts (`testTimeout 900_000`, `hookTimeout 300_000`, `fileParallelism false`)
- [x] 7.3 `RAYITO_E2E=1 RAYITO_TEMPLATE=<arn> pnpm test:e2e` against the M5 image (10.0 or newer): both tests green; paste the timings (`kernel_ready_s` per sandbox, `pause_s`, `resume_s`, `kernel_alive_s`, `resubscribe_s`, `rearm_timeout_s`, `pty_reattach_s`, `auto_resume_s`), the unit test count, the `dist/` sizes and the `pnpm pack --dry-run` list into `MILESTONES.md` M6 (track B "Estado de aceptación"); if a stream cut appears with a `ConnectError` form the classifier did not expect, add the row to `transport/errors.ts` and its test before re-running
- [x] 7.4 `AWS_API_NOTES.md` §16: add a row only if the e2e measured a new platform fact (e.g. the exact `ConnectError` forms a Node client sees when the VM freezes mid-stream); otherwise state "no new facts" in the Notes below
- [x] 7.5 `python scripts/gen_limits.py --check`, `cd clients/python && uv run pytest tests/unit && uv run ruff check . && uv run ruff format --check . && uv run mypy src` green (430+ tests with `test_limits.py`); `cargo test --workspace` untouched and green; `openspec validate m6-typescript-sdk --strict` passes; acceptance agent runs `openspec archive m6-typescript-sdk --yes`

## 8. [review] M6 review findings (TypeScript track)

- [x] 8.1 `src/transport/transport.ts` + `src/sandbox/core.ts`: build an explicit `Http2SessionManager` per transport, pass it as `sessionManager` to `createGrpcTransport`, keep both managers in `SandboxCore` and `abort()` them in `close()` after the streams (Python parity with `channel.close()`); unit test: `rayd.server.getConnections()` reaches 0 shortly after `close()`; README/JSDoc claim ("cierra los transportes") made true
- [x] 8.2 `src/transport/errors.ts`: `isNotYetReachable = isStreamReset || Unavailable || DeadlineExceeded` so a dropped HTTP/2 session (`Aborted`, `Canceled`/`Internal` `http/2 stream closed`, `ERR_HTTP2_*`) during the reconnect poll or the boot poll counts as "not yet" (design D14); update the truth-table row and add a reconnect test whose first `Health` after `suspendResume()` fails with an `Aborted` and an `Internal` reset form
- [x] 8.3 `src/sandbox/commands.ts`, `src/sandbox/code.ts`, `src/sandbox/core.ts`: release a stream's `AbortController` from the live registry when the stream ends normally (`result.done`, `isFinished()`, replaced on re-subscribe, `runCode` finished); expose `SandboxCore.liveStreamCount` and assert it returns to 0 after a foreground `run`, `pty.wait()` and `runCode`
- [x] 8.4 `src/transport/transport.ts`: refuse `scheme: "http"` towards any non-loopback host with `InvalidArgumentError` (the fake stays on `127.0.0.1`); two unit tests (loopback allowed, non-loopback refused) and the rule in the `TransportSettings` JSDoc
- [x] 8.5 (M6 acceptance, 2026-09-16) `src/transport/transport.ts`: `DEFAULT_TRANSPORT_SETTINGS.pingIdleConnection = false` with the rationale in the JSDoc (connect-node 2.2.0 re-arms the idle PING timer from a stream's `close` handler after `abort()` detached the session listeners; the unref'd timer then calls `ping()` on the destroyed session 30 s after `close()` and kills the process with an uncaught `ERR_HTTP2_INVALID_SESSION`); design D4 amendment 8.5; `tests/unit/transport-errors.test.ts` defaults updated; regression test `close with a live stream leaves no PING timer on the destroyed session` in `tests/unit/sandbox.test.ts` (default transport, fake `setTimeout`, advance 2 × `pingIntervalMs`): red with the old default (`ERR_HTTP2_INVALID_SESSION`), green with the fix; `pnpm lint`, `pnpm typecheck`, `pnpm test` (292 passed), `pnpm build`, `pnpm pack:check` green; `pnpm test:e2e` re-run on `rayito-base` 16.0 without unhandled errors (numbers in `MILESTONES.md` M6 Track B)

### Notes

- **Versions resolved on the implementer's box (2026-09-16):** `pnpm view rayito
  version` → 404 (name free); `@aws-sdk/client-lambda-microvms` 3.1133.0;
  `@aws-sdk/client-sts` 3.1133.0; `@connectrpc/connect` 2.2.0;
  `@connectrpc/connect-node` 2.2.0; `@bufbuild/protobuf` 2.15.0 (=
  `buf.build/bufbuild/es:v2.15.0`, BSR reachable, no local plugin needed);
  `@smithy/node-http-handler` 4.12.1; `typescript` 5.9.3; `tsdown` 0.23.0;
  `vitest` 3.2.7; `@biomejs/biome` 2.5.14; `@types/node` 20.19.43; `pnpm`
  9.15.4 (`packageManager`); Node 22.21.1 locally, Node 20 on CI.
- **`pnpm pack --dry-run` does not exist in pnpm 9** ("Unknown option:
  'dry-run'"). Replaced everywhere (5.10, 6.2, 7.3) by `pnpm pack:check` =
  `scripts/pack-check.mjs`: packs into `.pack/`, lists the tarball with `tar`
  (relative paths: GNU tar on Windows reads `D:\…` as a remote host) and
  asserts `package.json`, `README.md`, `LICENSE`, `dist/index.{mjs,cjs,d.mts,
  d.cts}` are present. Verified list: `LICENSE`, `README.md`, `package.json`,
  `dist/index.cjs`, `dist/index.cjs.map`, `dist/index.d.cts`,
  `dist/index.d.mts`, `dist/index.mjs`, `dist/index.mjs.map`.
- **`paginateListMicrovms` replaced by manual paging** (`ListMicrovmsCommand`
  with `maxResults 50` + `nextToken`): the SDK paginator throws "Invalid
  client, expected instance of LambdaMicrovmsClient" for the injected
  `CommandSender`; behaviour (page size 50, client-side state filter) is the
  one specified.
- Naming deltas vs. the task text: the fake server is `FakeRayd.start()`
  (`tests/unit/fake/server.ts`) instead of `startFakeRayd`, the shared helpers
  live in `fake/common.ts` (no `fake/index.ts`), and `suspendResume()` is a
  method of `FakeRayd`. `VERSION` is a `src/version.ts` constant guarded by
  `tests/unit/version.test.ts` against `package.json` (the second option of 5.6).
- **Client deadline vs. pause in the e2e:** a live `CommandHandle` started with
  `timeoutMs` carries a client-side stream deadline (`timeoutMs + 5 s`) that
  runs on the client clock, so a 30 s pause exhausts it (same as Python). The
  e2e therefore disconnects the `timed` handle before `pause()` and observes the
  re-armed server timeout through `commands.connect(pid)` after `resume()`
  (`rearm_timeout_s` 24.52 s from `resumedAt`). Two e2e runs failed on that and
  on a buffered PTY read before the third passed; no SDK change was needed.
- `Sandbox.close()` is synchronous (aborts streams via `abortNow()`, stops the
  refresher); `[Symbol.asyncDispose]` kills the sandbox. `Symbol.asyncDispose`
  polyfill in `src/index.ts` for Node 20.
- The proto changed concurrently (other tracks added `HealthResponse`
  `imds_blocked` 9, `hook_anomalies` 10, `metadata` 11); `buf generate` was
  re-run at the end and `src/gen/rayito/v1/health_pb.ts` committed with them.
- Gates (final run): `pnpm lint` (Biome, 65 files, no errors), `pnpm
  typecheck`, `pnpm build` (`dist/index.mjs` 167.63 kB, `dist/index.cjs`
  171.61 kB, `.d.mts`/`.d.cts` 102.18 kB), `pnpm test` 282 passed / 20 files
  in ~21 s, `pnpm pack:check`; `python scripts/gen_limits.py --check` OK;
  Python 610 passed + ruff + mypy clean; `cargo test --workspace` 335 passed;
  `openspec validate m6-typescript-sdk --strict` valid.
- **E2E (7.3):** three runs, six MicroVMs, all `TERMINATED` (`list-microvms`
  → 0 live at the end); third run 2 passed in 193 s; timings in
  `MILESTONES.md` M6 track B.
- **7.4:** no new platform facts measured; `AWS_API_NOTES.md` §16 unchanged.
- 7.5 archive (`openspec archive m6-typescript-sdk --yes`) is left to the
  acceptance agent.
- **8.x (review fixes, 2026-09-16):** `Http2SessionManager.abort()` is not a
  permanent shutdown in connect-node 2.2.0: a request issued after `close()`
  transparently opens a fresh session (the manager's documented behaviour),
  so the test asserts the count reaches 0 and stays there (no spontaneous
  reopen, `rayd.sessions` still 2), not that later RPCs reject. Each new test
  was run once against the reverted fix and failed (4 for 8.2, 1 each for
  8.1 and 8.3). Gates after the fixes: `pnpm lint`, `pnpm typecheck`,
  `pnpm build`, `pnpm test` (291 passed in 26 s), `pnpm pack:check`,
  `openspec validate m6-typescript-sdk --strict` all green.

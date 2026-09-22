## Why

The Python SDK (`rayito` 0.0.5) is the only client of `rayd`, and every
M1–M5 behaviour — lifecycle, commands, files, PTY, `run_code`, pause/resume
with the reconnection contract — is accepted only through it. `SPEC.md` §5
and `openspec/project.md` commit to a second client, TypeScript over
Connect-ES v2, generated from the same `.proto` and validating the same
AWS limits; `MILESTONES.md` M6 lists "Cliente TypeScript con paridad de API
(Connect-ES v2, mismo JSON de límites)" as one of its tracks. Agent
frameworks in the Node ecosystem (the E2B JS surface is what they code
against) cannot use Rayito today, and the limits the two SDKs must agree on
(`_limits.py`) have no single source, so the second SDK would drift from
the first the day it is written.

## What Changes

M6 track B, decided in full by `design.md`:

- A new package `clients/typescript` published as `rayito` on npm (name
  verified free 2026-09-16), pnpm, TypeScript strict, ESM + CJS built with
  `tsdown`, Node ≥ 20, Biome for lint and format, vitest for tests. The
  Python package and `rayd` are not changed in behaviour.
- Codegen from the existing `buf.gen.yaml` target
  (`buf.build/bufbuild/es:v2.15.0`, `target=ts`, `import_extension=js`) into
  `clients/typescript/src/gen`, committed. The BSR is reachable from the
  development box (verified 2026-09-16: `buf generate` produced the six
  `*_pb.ts` files); a local `protoc-gen-es` fallback is documented, not
  wired.
- Transport: `@connectrpc/connect` + `@connectrpc/connect-node`
  `createGrpcTransport({ baseUrl: "https://<endpoint>" })` (HTTP/2 only in
  v2; there is no `httpVersion` option) with an interceptor that injects
  lowercase `x-aws-proxy-auth`, `x-aws-proxy-port`, `x-aws-proxy-force-h2`
  and `x-access-token` on every request from a `TokenStore`, so JWE rotation
  never touches the transport; at most two transports (unary + lazy stream)
  per sandbox; error classification on `ConnectError.code` + `rawMessage`
  (a proxy 403 is `PermissionDenied` with `rawMessage === "HTTP 403"`).
- Control plane: `@aws-sdk/client-lambda-microvms` (verified on npm,
  3.1133.0: `RunMicrovmCommand`, `GetMicrovmCommand`, `ListMicrovmsCommand`
  + `paginateListMicrovms`, `SuspendMicrovmCommand`, `ResumeMicrovmCommand`,
  `TerminateMicrovmCommand`, `CreateMicrovmAuthTokenCommand`, the seven
  error classes) behind the same `ControlPlane` port as Python, with the
  same per-operation token buckets, the same error mapping by exception
  name, and `@aws-sdk/client-sts` only to resolve a template name to an ARN.
  No AWS parameter outside `AWS_API_NOTES.md` §2, §3, §5, §6 is used.
- One limits source: `limits.json` at the repo root; `scripts/gen_limits.py`
  renders `clients/python/src/rayito/_limits.py` and
  `clients/typescript/src/limits.ts`; a Python unit test and a vitest test
  fail whenever either rendering drifts from the JSON.
- Surface (async-only, camelCase, milliseconds with the `Ms` suffix as in
  E2B's JS SDK): `Sandbox.create/connect/kill/list/getInfo/pause/resume`
  (static and instance forms), `isRunning`, `getHealth`, `getHost`,
  `getMetrics`, `close`, `Symbol.asyncDispose`; `sandbox.commands`
  (`run` foreground/background with `onStdout`/`onStderr`/`stdin`/`user`/
  `cwd`/`envs`/`timeoutMs` → `CommandResult | CommandHandle`, `list`,
  `kill`, `sendStdin`, `closeStdin`, `connect`); `sandbox.files` (`read`
  text/bytes/stream, `write`, `writeFiles`, `list`, `exists`, `getInfo`,
  `remove`, `rename`, `makeDir`, `watchDir` → `WatchHandle`); `sandbox.pty`
  (`create`, `connect`, `sendInput`, `resize`, `kill`; `PtyHandle` is a
  `CommandHandle`); `sandbox.runCode` → `Execution` (`results`, `logs`,
  `error`, `executionCount`, `text`) and `createCodeContext`/
  `listCodeContexts`/`removeCodeContext`/`restartCodeContext`; the M5
  reconnection contract (`resumeGeneration`, `Connect(fromSeq)`,
  `Pty.Connect`, `WatchDir` re-issue, `Reattach`, wake rules, dormant
  handles) and the same error hierarchy under E2B's JS names
  (`SandboxError`, `TimeoutError`, `InvalidArgumentError`, `NotFoundError`,
  `FileNotFoundError`, `SandboxNotFoundError`, `SandboxNotReadyError`,
  `SandboxStateError`, `SandboxLifetimeError`, `CommandExitError`,
  `RateLimitError`; outside the hierarchy `AuthenticationError`,
  `QuotaExceededError`, `CapacityError`).
- Tests: vitest unit tests against an in-process fake `rayd` served by
  `connectNodeAdapter` on a plaintext `http2` server (the same scripted
  behaviours as the Python fakes, including `suspend()`/`suspendResume()`)
  and a fake control plane; a real-AWS e2e `tests/e2e/m6.e2e.test.ts`
  guarded by `RAYITO_E2E=1` + `RAYITO_TEMPLATE` covering the `SPEC.md` §6
  flow end to end through the TypeScript SDK.
- Gates: `pnpm install`, `pnpm build`, `pnpm test`, `pnpm lint`,
  `pnpm typecheck` green; `Makefile` and CI gain the TypeScript steps;
  `README.md` for the package with the Python README's examples.

Out of this change (other M6 tracks, `SPEC.md` §4 non-goals): cgroup
slices, IMDS blocking, egress allowlist, the `rayito.e2b` shim, the cold
start benchmark, sidecar consolidation, per-sandbox metadata, filesystem
persistence, publishing to npm (only `pnpm pack` is verified), a sync
TypeScript tree, browser support.

## Capabilities

### New Capabilities
- `typescript-sdk`: the `rayito` npm package — codegen, transport and
  proxy headers, control plane adapter with token buckets, the `Sandbox`
  surface (lifecycle, commands, files, PTY, code), the reconnection
  contract, the error hierarchy, unit tests against the fake `rayd`, the
  real-AWS e2e, packaging and gates.
- `sdk-limits`: one `limits.json` at the repo root rendered into the
  Python and TypeScript limits modules by `scripts/gen_limits.py`, with a
  drift test in each SDK and a `--check` mode wired into `make lint`.

### Modified Capabilities

None. The Python SDK's requirements in `process-lifecycle`, `filesystem`,
`code-execution`, `pty` and `suspend-resume` are unchanged; the TypeScript
SDK mirrors them and the mirror is specified in `typescript-sdk`. `rayd`
and the image are untouched (the e2e runs against the M5 image 10.0).

## Impact

- New: `clients/typescript/` (`package.json`, `pnpm-lock.yaml`,
  `tsconfig.json`, `tsdown.config.ts`, `biome.json`, `vitest.config.ts`,
  `src/**`, `src/gen/rayito/v1/*_pb.ts`, `tests/unit/**`,
  `tests/e2e/m6.e2e.test.ts`, `README.md`), `limits.json`,
  `scripts/gen_limits.py`, `clients/python/tests/unit/test_limits.py`.
- Regenerated: `clients/python/src/rayito/_limits.py` (same values, now
  rendered from `limits.json` with a generated-file header).
- Edited: `Makefile` (`limits`, `test-typescript`, `lint-typescript`,
  `test-e2e-typescript`), `.github/workflows/ci.yml` (Node 20 + pnpm job),
  `.gitignore` (`node_modules/`, `clients/typescript/dist/`),
  `ARCHITECTURE.md` ("TypeScript" section: layout, transport facts, error
  table for Connect codes), `MILESTONES.md` (M6 track B state),
  `README.md` (root: the TypeScript client exists), `AWS_API_NOTES.md` §16
  only if the e2e measures a new platform fact.
- Dependencies (runtime): `@bufbuild/protobuf ^2.15.0` (floor = plugin
  version, the Python rule), `@connectrpc/connect ^2.2.0`,
  `@connectrpc/connect-node ^2.2.0`, `@aws-sdk/client-lambda-microvms
  ^3.1133.0`, `@aws-sdk/client-sts ^3.1133.0`, `@smithy/node-http-handler
  ^4.12`. Dev: `typescript ^5.9`, `tsdown`, `vitest`, `@biomejs/biome`,
  `@types/node`.
- Governed by `AWS_API_NOTES.md` facts: proxy headers and their lowercase
  form (§7), token TTL 60 min and the single `X-aws-proxy-auth` key (§3),
  suspend/resume idempotence (§5, Q38), `get-microvm` eventual consistency
  (§6), `runHookPayload` 4096 chars (§2), the 8-connection cap (§7, §11),
  the API TPS quotas (§11). No new AWS parameter and no `rayd` change.

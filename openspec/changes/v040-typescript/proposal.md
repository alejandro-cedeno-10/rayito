## Why

Three findings from `docs/research/2026-09-m9-architecture-review.md` land in
`clients/typescript` for 0.4.0:

- **`:8`/`:21` (major, item 1 of the review's ts-exceptions/py-exceptions
  pair)** — the same gRPC status `UNIMPLEMENTED` produces three different
  error shapes depending on which code path translates it: the generic RPC
  table gives `InvalidArgumentError` with `grpcCode: Code.Unimplemented`
  (kernel absent from the image, any other RPC an old agent does not know),
  `historyErrorTranslator` gives `UnimplementedError` for `MetricsHistory`,
  and `LifecycleUnsupportedError` (a plazo lógico against a pre-M9 agent)
  extends `InvalidArgumentError`/`SandboxError` instead of either. A caller
  cannot write one `catch` clause for "this sandbox cannot do that"; it has
  to check `grpcCode` on some paths and the class on others, and
  `isHistoryUnavailable` distinguishes its own error from a lookalike by
  comparing `reason` text instead of `instanceof`.
- **`:10` (major, item 3, ts-apiparams)** — the E2B shim's instance
  connection variants (`sbx.kill(opts)`, `sbx.setTimeout(ms, opts)`, ...)
  validate and merge `ConnectionOpts` but only ever forward `signal` (and,
  on most, `requestTimeoutMs`) to the already-built channel and plane;
  `headers`, `proxy`, `retries`, `logger`, `region`, `controlPlane`,
  `accessToken` and `transport` passed to one of those calls are silently
  dropped, while the equivalent static variants (`Sandbox.kill(sandboxId,
  opts)`) apply all of them. E2B's own signatures accept the same options on
  both variants, so the instance ones silently diverge from what a caller
  reasonably expects.
- **`:30`/`:35` (minor, item 1, ts-probedinfo)** — `NativeSandbox.probedInfo`
  is a public `static` of the native `rayito` `Sandbox` (and therefore part
  of the published `.d.ts`), used only by `rayito/e2b`'s `Sandbox.getInfo`;
  its own docblock already says it is internal to the shim. The dependency
  is inverted: the native core carries API shaped for one specific
  compatibility shim.

## What Changes

- **BREAKING**: the generic gRPC `Unimplemented` translation
  (`transport/errors.ts`) stops producing `InvalidArgumentError` and
  produces `UnimplementedError` (outside the `SandboxError` hierarchy)
  through one shared helper, `unimplementedRpcError(connect, feature?)`; a
  caller that knows which RPC failed (`runCode`/`createCodeContext`'s
  missing-kernel case) passes its own `feature` to the same helper instead
  of a copy of the table. The default `feature` (`GENERIC_RPC_FEATURE`,
  `"esta llamada"`) and the `"; " + UNIMPLEMENTED_IMAGE_HINT` suffix on
  `reason` (`"publica una imagen con una versión actual de rayd"`) match the
  Python SDK's `unimplemented_rpc_error` byte-for-byte, so the two SDKs give
  the same generic `UnimplementedError` message for the same old-rayd RPC.
  `LifecycleUnsupportedError` becomes a subclass of
  `UnimplementedError`, not of `InvalidArgumentError`/`SandboxError`.
  `historyErrorTranslator`'s own `MetricsHistoryUnavailableError` (a private
  subclass of `UnimplementedError`) replaces the reason-text comparison in
  `isHistoryUnavailable` with `instanceof`.
- The E2B shim's instance connection variants now warn, through the
  existing `process.emitWarning(..., { type: "RayitoCompatWarning" })`
  channel (naming the option, never its value), for every `ConnectionOpts`
  key on the call that the method does not forward to the native layer —
  the same divergence the always-ignored options (`apiKey`, `domain`, ...)
  already warn about, now covering `headers`, `proxy`, `retries`, `logger`,
  `region`, `controlPlane`, `accessToken` and `transport` where they are
  silently dropped today. No behaviour changes beyond the new warnings; the
  static variants are unaffected (they already apply everything).
- `NativeSandbox.probedInfo`/`ProbedInfoOptions` move, body unchanged, out
  of `sandbox/sandbox.ts` into a new internal module,
  `sandbox/info-probe.ts` (`probeSandboxInfo`), imported only by
  `e2b/sandbox.ts`. The native `Sandbox` class no longer has a
  `probedInfo` static, and neither name appears in `rayito`'s or
  `rayito/e2b`'s published exports or `.d.ts`.
- Tests, `CHANGELOG.md` and this OpenSpec change only; no `.proto`, no
  `rayd`, no `clients/python`.

## Migración

- Un `catch` alrededor de `runCode`/`createCodeContext` (kernel ausente) o
  cualquier otro RPC que un `rayd` anterior no implemente, que comprobaba
  `error instanceof InvalidArgumentError && error.grpcCode ===
  Code.Unimplemented` o `error instanceof SandboxError`, pasa a `error
  instanceof UnimplementedError`.
- `LifecycleUnsupportedError` (un plazo lógico contra un agente anterior a
  M9) ya no es `InvalidArgumentError` ni `SandboxError`; es `UnimplementedError`.
- `getMetricsHistory` contra una imagen anterior a M9 ya lanzaba
  `UnimplementedError` (sin cambios ahí); sólo cambia su `error.cause`, de
  `InvalidArgumentError`/`SandboxError` con `grpcCode: Code.Unimplemented` a
  `UnimplementedError`. Ninguna migración salvo para quien inspeccionara ese
  `cause`.
- `Sandbox.probedInfo`/`ProbedInfoOptions` eran internos de `rayito/e2b`
  (documentado así en su propio docblock); quien los llamara directamente
  pasa a `Sandbox.getInfo(sandboxId)` de `rayito/e2b`, que hace exactamente
  lo mismo.
- Ninguna migración para los avisos nuevos de opciones de instancia: sólo
  avisan donde antes la opción se perdía en silencio.

## Capabilities

### New Capabilities

(none)

### Modified Capabilities

- `typescript-sdk`: "Code execution surface" (kernel-missing `Unimplemented`
  is `UnimplementedError`, not `InvalidArgumentError`), "Error hierarchy
  under E2B's JavaScript names" (the generic `Unimplemented` translation and
  the error hierarchy's outside-members), and "E2B JS Sandbox surface in
  rayito/e2b" (the new instance unapplied-connection-option warnings).

## Impact

- Affected code: `clients/typescript/src/errors.ts`,
  `clients/typescript/src/transport/errors.ts`,
  `clients/typescript/src/sandbox/lifecycle.ts`,
  `clients/typescript/src/sandbox/metrics.ts`,
  `clients/typescript/src/sandbox/code.ts`,
  `clients/typescript/src/sandbox/sandbox.ts`,
  `clients/typescript/src/sandbox/info-probe.ts` (new),
  `clients/typescript/src/e2b/compat.ts`,
  `clients/typescript/src/e2b/sandbox.ts`, `clients/typescript/tests/**`,
  `clients/typescript/README.md`, `clients/typescript/CHANGELOG.md`.
- Not touched: `docs/site/**` (the shared `lifecycle.md`/`images.md`/
  `kernels.md`/`e2b-compat.md` pages and the `sandbox-timeout`/`e2b-compat`
  OpenSpec requirements that name both SDKs are `v040-python`'s, to avoid an
  archive conflict), `clients/python/**`, `crates/**`, `proto/**`,
  `src/gen/**`.
- Breaking change for 0.4.0 (`feat(typescript)!:`, `BREAKING CHANGE:`
  footer); release-please bumps `0.3.3` → `0.4.0`.

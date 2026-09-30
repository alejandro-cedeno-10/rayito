## 1. `UnimplementedError` is the single type for gRPC `Unimplemented` (ts-exceptions)

- [x] 1.1 `errors.ts`: move `LifecycleUnsupportedError` after `UnimplementedError`
      and change it to `extends UnimplementedError`, inheriting its
      `(feature, reason, doc?, options?)` constructor
- [x] 1.2 `sandbox/lifecycle.ts`: add `LIFECYCLE_FEATURE_CONNECT`,
      `LIFECYCLE_FEATURE_CREATE`, `LIFECYCLE_FEATURE_SET_TIMEOUT`; update
      `connectExtension`, `setTimeoutUnsupportedError` and `olderAgentError`
      to build `LifecycleUnsupportedError(feature, reason, undefined, {
      cause })`
- [x] 1.3 `transport/errors.ts`: add `GENERIC_RPC_FEATURE` and
      `unimplementedRpcError(connect, feature = GENERIC_RPC_FEATURE)`; the
      `translateRpcError` switch routes `Code.Unimplemented` through it
      instead of the shared `InvalidArgument`/`FailedPrecondition` case
- [x] 1.4 `sandbox/code.ts`: `runCode`'s stream open and `createContext`'s
      unary both catch the generic `UnimplementedError`, extract the
      `ConnectError` from its `cause`, and rebuild it with
      `unimplementedRpcError(connect, kernelFeature(name, language))`;
      update the `RunCodeOptions.language` docblock
- [x] 1.5 `sandbox/metrics.ts`: add private `MetricsHistoryUnavailableError
      extends UnimplementedError` (exported for tests only, not from
      `index.ts`); `historyErrorTranslator` builds it when
      `translateRpcError` already returned `UnimplementedError`;
      `isHistoryUnavailable` becomes `instanceof MetricsHistoryUnavailableError`
- [x] 1.6 `e2b/compat.ts`: `unimplementedLanguage` checks `error instanceof
      UnimplementedError` instead of `SandboxError`/`grpcCode`
- [x] 1.7 Tests: `errors.test.ts` (`LifecycleUnsupportedError instanceof
      UnimplementedError`, not `SandboxError`/`InvalidArgumentError`),
      `transport-errors.test.ts` (Unimplemented → `UnimplementedError` with
      `reason`/`cause`, the `SetTimeout` table unchanged),
      `lifecycle.test.ts`/`sandbox-timeout.test.ts` (older-agent and
      `SetTimeout` paths), `code.test.ts`/`e2b-shim.test.ts` (kernel-missing
      is `UnimplementedError` naming `rayito-base-poly`),
      `metrics.test.ts` (`isHistoryUnavailable` by `instanceof`, a lookalike
      `UnimplementedError` no longer counts), `e2b-compat.test.ts`
      (`unimplementedLanguage`'s table) — `pnpm test` green
- [x] 1.8 Cosmetic, no SDK behaviour change: `tests/unit/fake/lifecycle.ts:116`
      adopts rayd's new message "el timeout debe ser de al menos 1 s"
- [x] 1.9 `tests/e2e/poly.e2e.test.ts`: the `rayito-base` kernel-missing
      scenario expects `UnimplementedError`, not `InvalidArgumentError`

## 2. Instance connection options that reach the native layer warn when dropped (ts-apiparams)

- [x] 2.1 `e2b/compat.ts`: `instanceUnappliedReason(call)` and
      `unappliedInstanceOpts(call, applicable)` (pure; only
      `ConnectionOpts` keys, alphabetical, excluding
      `IGNORED_CONNECTION_OPTS`)
- [x] 2.2 `e2b/sandbox.ts`: `#connection(opts, call, applicable)` calls
      `nativeConnection` and the new warnings in one place; every instance
      method passes its own `call` name and the exact applicable-key set
      audited against its body (`kill`/`pause`: `{signal}`;
      `getInfo`/`isRunning`/`setTimeout`/`connect`/`getMetrics`/
      `updateNetwork`: `{signal, requestTimeoutMs}`)
- [x] 2.3 Tests: `e2b-compat.test.ts` (`unappliedInstanceOpts` pure table),
      `e2b-shim.test.ts` (`sbx.kill({ retries, proxy })` emits two
      `RayitoCompatWarning`s naming only the keys; `sbx.setTimeout(ms, {
      requestTimeoutMs })` emits none; static variants emit none) — `pnpm
      test` green

## 3. `probedInfo` moves out of the native `Sandbox` surface (ts-probedinfo)

- [x] 3.1 New `sandbox/info-probe.ts`: `probeSandboxInfo`/`ProbedInfoOptions`,
      body moved verbatim from `sandbox.ts`'s `static probedInfo`
- [x] 3.2 `sandbox/sandbox.ts`: delete `static probedInfo` and
      `ProbedInfoOptions`, and the imports left unused by the removal
- [x] 3.3 `e2b/sandbox.ts`: `infoFor` calls `probeSandboxInfo` from
      `./info-probe.js` instead of `NativeSandbox.probedInfo`
- [x] 3.4 Tests: `e2b-exports.test.ts` — `'probedInfo' in NativeSandbox` and
      in `e2b.Sandbox` are both `false`, and `probedInfo`/`probeSandboxInfo`/
      `ProbedInfoOptions` are absent from both packages' exports;
      `e2b-shim.test.ts`'s existing `getInfo(sandboxId)` scenarios stay
      green unchanged
- [x] 3.5 `pnpm build`: diff `dist/index.d.mts`/`dist/e2b.d.mts` before and
      after — the only change is the loss of `probedInfo`/`ProbedInfoOptions`

## 4. OpenSpec and docs

- [x] 4.1 `specs/typescript-sdk/spec.md` delta: "Code execution surface",
      "Error hierarchy under E2B's JavaScript names" and "E2B JS Sandbox
      surface in rayito/e2b" (items 1 and 2's requirement text changes)
- [x] 4.2 `openspec validate v040-typescript --strict` passes
- [x] 4.3 `CHANGELOG.md` `[Unreleased]`: "Cambios que rompen" for item 1's
      hierarchy change and the `probedInfo` removal, "Changed" for item 2's
      new warnings
- [x] 4.4 `README.md`: the error-hierarchy paragraph gains `UnimplementedError`
      and its `LifecycleUnsupportedError` subclass; the `rayito/e2b` section
      gains one line on the new instance-option warnings

## 5. Gates

- [x] 5.1 `npx pnpm@9.15.4 install --frozen-lockfile && pnpm lint && pnpm
      typecheck && pnpm build && pnpm test && pnpm pack:check`, all green
      (896 tests)
- [x] 5.2 `cd clients/python && uv run pytest ../../scripts/tests -q`,
      `python3 scripts/check_pins.py`, `python3 scripts/check_hygiene.py`,
      all green (unaffected by this change, run for completeness)
- [ ] 5.3 CI green on the PR (GitHub Actions; not runnable locally)

## 1. Share one `TransferClient` per sandbox (ts-e2b-sandbox-130)

- [x] 1.1 Read `sandbox/filesystem.ts`'s native constructor, `e2b/filesystem.ts`'s
      `Filesystem extends NativeFilesystem`, and `e2b/sandbox.ts:126`'s
      `new Filesystem(native.files.core)` to confirm the duplicate
      `TransferClient`
- [x] 1.2 `sandbox/filesystem.ts`: `constructor(core: SandboxCore, sharedWith?:
      Filesystem)`, reusing `sharedWith`'s `#transfers` (documented as
      internal to rayito/e2b); nothing else in the class changes
- [x] 1.3 `e2b/sandbox.ts:126`: `new Filesystem(native.files.core,
      native.files)`
- [x] 1.4 New unit test (`e2b-shim.test.ts`): the transfer-support probe RPC
      runs exactly once when both `sbx.files` and `sbx.native.files`
      trigger it
- [x] 1.5 New unit test (`filesystem.test.ts`): `new Filesystem(core)` without
      the second argument still probes on its own
- [x] 1.6 Diff `dist/e2b.d.mts`/`dist/index.d.mts` (and their `sandbox-*.d.mts`
      chunk) before/after: only the new optional constructor parameter
      changes

## 2. `awsClientSettings` becomes an explicit `ControlPlane` port field (ts-control-plane-366)

- [x] 2.1 Add `readonly awsClientSettings?: AwsClientSettings | undefined;`
      to `ControlPlane`, documented as a field decorators must forward
- [x] 2.2 Rewrite `awsClientSettingsOf` as `plane.awsClientSettings ?? {}`,
      keeping the exported name and signature
- [x] 2.3 Delete `AwsClientSettingsSource` (confirmed unused: `grep -rn
      AwsClientSettingsSource src tests` has no other hits)
- [x] 2.4 New test (`transfer-credentials.test.ts`): a bare object literal
      implementing `ControlPlane` with `awsClientSettings: { credentials
      }` is honoured by `s3ClientOverridesFor`/`awsClientSettingsOf`; the
      same literal without the field yields `{}`

## 3. Break the `core.ts` ↔ `commands.ts` import cycle (ts-core-55)

- [x] 3.1 New `sandbox/stream-errors.ts`: move `STREAM_PROBE_TIMEOUT_MS` and
      `streamFailureError` verbatim, with only the imports they need;
      confirm by grep it imports neither `core.js` nor `commands.js`
- [x] 3.2 `core.ts` imports both names from `./stream-errors.js` instead of
      `./commands.js`
- [x] 3.3 `commands.ts` re-exports both names from `./stream-errors.js`, so
      every existing import path is unchanged
- [x] 3.4 New `tests/unit/stream-errors.test.ts`: the branches of
      `streamFailureError` (non-reset → translated, `healthOk`, terminal
      state, suspended state, unknown state) plus the commands.ts
      re-export identity check

## 4. Move IO-free `getMetrics` policy into `compat.ts` (ts-e2b-sandbox-449)

- [x] 4.1 Move `historyImageError`, `lifecycleImageError` and
      `LIFECYCLE_IMAGE_REASON` verbatim into `compat.ts`; re-export
      `LIFECYCLE_IMAGE_REASON` from `e2b/sandbox.ts` so its import path is
      unchanged
- [x] 4.2 Add `metricsHistoryOrSnapshot(history, snapshot, { ranged,
      feature })` to `compat.ts`, reproducing the current instance
      `getMetrics` fallback exactly
- [x] 4.3 Instance `getMetrics` calls the new helper with `ranged =
      !(start === undefined && end === undefined)`
- [x] 4.4 `metricsFor` (static/class variant) keeps its own try/catch
      around the moved `historyImageError`, not routed through the new
      helper (no snapshot fallback for that variant)
- [x] 4.5 New pure tests in `e2b-compat.test.ts` for
      `metricsHistoryOrSnapshot`'s branches with stub closures: snapshot
      not called when history is non-empty or ranged; a non-history error
      propagates unchanged by identity

## 5. Report-only items (no code change)

- [x] 5.1 `Sandbox.probedInfo`/`ProbedInfoOptions` (ts-sandbox-810): confirm
      it is still a public static of the native `Sandbox` and that
      removing/moving it would change the published `.d.ts`; documented as
      deferred in the proposal, not implemented
- [x] 5.2 Integration-name regex mismatch (ts-validation-integration):
      confirm TS (`validation.ts:9`) still rejects a single space that
      Python (`_connection.py:86`) accepts; documented as deferred in the
      proposal, not implemented

## 6. Gates and docs

- [x] 6.1 `pnpm install --frozen-lockfile && pnpm lint && pnpm typecheck &&
      pnpm build && pnpm test && pnpm pack:check`, all green
- [x] 6.2 `clients/typescript/CHANGELOG.md`: `[Unreleased]` entries for
      items 1-4
- [x] 6.3 `docs/research/2026-09-m9-architecture-review.md`: mark `:130`,
      `:366`, `:55`, `:449` resolved by this PR; keep `:810`/`:375` listed
      as deferred with the reason
- [x] 6.4 `openspec validate m10-typescript --strict` passes
- [ ] 6.5 CI green on the PR (GitHub Actions; not runnable locally)

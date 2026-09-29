## Why

`docs/research/2026-09-m9-architecture-review.md` (2026-09-24 hexagonal/DDD
review) deferred four TypeScript findings to the next cycle, all inside
`clients/typescript`, none requiring a `.proto` or `rayd` change:

- **`:130` (major)** — the E2B shim builds `sandbox.files` as `new
  Filesystem(native.files.core)`; the shim `Filesystem` *extends* the native
  one (`e2b/filesystem.ts:35`), so this constructs a second native
  `Filesystem` over the same `SandboxCore` and, with it, a second
  `TransferClient` (`sandbox/filesystem.ts:711-718`) with its own
  `#supported` probe cache and its own S3 clients. `sbx.files` and
  `sbx.native.files` are two different objects with separate caches: the
  transfer-support probe runs twice, two S3 clients get built, and
  `sbx.uploadUrl()`/`downloadUrl()` (which go through `native.files`,
  `e2b/sandbox.ts:126`) can disagree with `sbx.files.read/write` (which go
  through the shim copy) about what the sandbox supports.
- **`:366` (major)** — S3 credentials and proxy reach the transfer's S3
  clients through a structural downcast: `awsClientSettingsOf(plane)` reads
  `(plane as Partial<AwsClientSettingsSource>).awsClientSettings` and falls
  back to `{}` (`aws/control-plane.ts:336-345`). `awsClientSettings` is not
  part of the `ControlPlane` port (`:216-229`), so the contract is implicit:
  a decorator that does not happen to forward the field (nothing in the
  type system says it must) makes `TransferClient` quietly build S3 clients
  with the default credential chain and no proxy, with no error. The pool
  already had to add a forwarding getter by hand
  (`pool/pool.ts:126-128`) to avoid exactly this.
- **`:55` (minor)** — `sandbox/core.ts` (the lower-level core) value-imports
  `STREAM_PROBE_TIMEOUT_MS` and `streamFailureError` from
  `sandbox/commands.ts` (a higher-level feature module), which in turn
  value-imports `withTimeout` from `core.ts`: an import cycle that predates
  M9 and got no smaller as `SandboxCore` grew.
- **`:449` (minor)** — `e2b/compat.ts` is documented as the pure mapping
  module that mirrors Python's `_compat.py`, but the IO-free error mappings
  `historyImageError`/`lifecycleImageError` and the "history or snapshot"
  fallback policy inside instance `getMetrics` live in the IO class
  `e2b/sandbox.ts` instead, unlike Python where `_compat.py` keeps all
  three so they are unit-testable without a sandbox and shared by `_sync`
  and `_async`.

Two more TypeScript findings from the same review are read and reported
here, not implemented, because either fix is itself a behaviour change the
review explicitly did not ask for in this wave:

- **`:810` (minor)** — `NativeSandbox.probedInfo`/`ProbedInfoOptions` are a
  shim-only helper exposed as a public static of the native `Sandbox`
  class. Removing or moving it drops it from the published `rayito` `.d.ts`
  (a public-API change); the clean fix needs metadata added to the native
  `SandboxInfo`, a model change out of scope for this wave.
- **`:375` (minor)** — TS's integration-name validation
  (`/^[\x21-\x7e]+$/`, `validation.ts:9`) rejects a single space that
  Python's (`[\x21-\x7e]+(?: [\x21-\x7e]+)*`, `_connection.py:86`) accepts.
  Aligning either SDK changes which integration names it accepts or
  rejects, which is a behaviour change neither SDK's callers opted into
  here.

## What Changes

- **`sandbox/filesystem.ts`**: the native `Filesystem` constructor gains an
  internal, optional second parameter, `constructor(core: SandboxCore,
  sharedWith?: Filesystem)`, that reuses `sharedWith`'s `TransferClient`
  instead of building a new one. `e2b/filesystem.ts`'s `Filesystem` class
  shape is unchanged (still `extends NativeFilesystem`, still overrides
  only `watchDir`); it inherits the new parameter for free.
  `e2b/sandbox.ts` passes it: `new Filesystem(native.files.core,
  native.files)`. No composition, no `Proxy`, `.core` unchanged — the
  published `.d.ts` gains exactly one optional constructor parameter and
  nothing else (diffed byte-for-byte against the pre-change build).
- **`aws/control-plane.ts`**: `ControlPlane` gains `readonly
  awsClientSettings?: AwsClientSettings | undefined`, documented as a field
  every decorator must forward. `awsClientSettingsOf` becomes `return
  plane.awsClientSettings ?? {};` — same exported name and signature, no
  caller changes. The now-redundant `AwsClientSettingsSource` interface is
  deleted (nothing imported it outside this file).
- **`sandbox/stream-errors.ts`** (new): `STREAM_PROBE_TIMEOUT_MS` and
  `streamFailureError`, moved verbatim from `commands.ts`. `core.ts` now
  imports them from here instead of from `commands.ts`; `commands.ts`
  re-exports both names so every existing import path is unchanged.
  `stream-errors.ts` imports neither `core.ts` nor `commands.ts`, closing
  the cycle.
- **`e2b/compat.ts`**: gains `historyImageError`, `lifecycleImageError`,
  `LIFECYCLE_IMAGE_REASON` (moved verbatim from `e2b/sandbox.ts`) and a new
  pure `metricsHistoryOrSnapshot(history, snapshot, { ranged, feature })`
  that reproduces the instance `getMetrics` fallback policy exactly.
  `e2b/sandbox.ts` re-exports `LIFECYCLE_IMAGE_REASON` so its import path is
  unchanged, and its instance `getMetrics` now calls the moved helper.
  `metricsFor` (the static, class-level variant) keeps its own
  try/catch around the moved `historyImageError` — its policy has no
  snapshot fallback, so it is not routed through
  `metricsHistoryOrSnapshot`.
- **Tests**: new `tests/unit/stream-errors.test.ts` (the five branches of
  `streamFailureError`, plus the commands.ts re-export identity); new
  `metricsHistoryOrSnapshot` cases in `tests/unit/e2b-compat.test.ts`; a
  new `ControlPlane`-port test in `tests/unit/transfer-credentials.test.ts`
  (a bare object literal, not a `LambdaMicrovmsControlPlane` or
  `FakeControlPlane`, is honoured through the port field); a new shared-
  `TransferClient` test in `tests/unit/e2b-shim.test.ts` (the probe runs
  once for both `sbx.files` and `sbx.native.files`) and in
  `tests/unit/filesystem.test.ts` (a bare `new Filesystem(core)` still
  probes on its own).
- **Docs**: `clients/typescript/CHANGELOG.md` `[Unreleased]`;
  `docs/research/2026-09-m9-architecture-review.md` marks the four fixed
  findings (`:130`, `:366`, `:55`, `:449`) as resolved by this PR and keeps
  `:810`/`:375` listed as still deferred, with the reason.

## Non-goals

- No composition/`Proxy` rewrite of the E2B `Filesystem`, no removal of
  `.core`, no S3-overrides injection at the composition root (`:366`'s
  "better" alternative) — the port field alone closes the downcast.
- No change to `Sandbox.probedInfo`/`ProbedInfoOptions` (`:810`): reported,
  not implemented (public-API change).
- No change to either SDK's integration-name validation (`:375`): reported,
  not implemented (accepted-input change).
- No `.proto`, no `rayd`, no generated code (`src/gen`), no dependency
  version bump (no `madge`), no error message or warning text change.

## Impact

- Affected code: `clients/typescript/src/sandbox/filesystem.ts`,
  `clients/typescript/src/e2b/filesystem.ts` (unchanged, inherits the new
  parameter), `clients/typescript/src/e2b/sandbox.ts`,
  `clients/typescript/src/aws/control-plane.ts`,
  `clients/typescript/src/sandbox/transfer.ts` (unchanged caller),
  `clients/typescript/src/pool/pool.ts` (unchanged caller),
  `clients/typescript/src/sandbox/core.ts`,
  `clients/typescript/src/sandbox/commands.ts`,
  `clients/typescript/src/sandbox/stream-errors.ts` (new),
  `clients/typescript/src/e2b/compat.ts`, five `tests/unit/*.test.ts`
  files, `clients/typescript/CHANGELOG.md`,
  `docs/research/2026-09-m9-architecture-review.md`.
- No breaking change: every moved/re-exported name keeps its old import
  path; the published `.d.ts` only gains the documented additions
  (`Filesystem`'s optional constructor parameter, `ControlPlane`'s optional
  `awsClientSettings` field), confirmed by diffing `dist/e2b.d.mts` and
  `dist/index.d.mts` (and their `sandbox-*.d.mts` chunk) before and after.
  No version bump forced by this change alone (`skip_specs: true`: pure
  internal refactor, no requirement text changes).

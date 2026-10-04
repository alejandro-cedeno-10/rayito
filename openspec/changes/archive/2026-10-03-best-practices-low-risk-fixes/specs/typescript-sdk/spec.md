## MODIFIED Requirements

### Requirement: The rayito/e2b subpath entry point
The npm package `rayito` SHALL publish a second entry point, `rayito/e2b`, as follows:

- **`package.json`:** `exports["./e2b"]` = `{ "import": { "types": "./dist/e2b.d.mts", "default": "./dist/e2b.mjs" }, "require": { "types": "./dist/e2b.d.cts", "default": "./dist/e2b.cjs" } }` (no `types` sibling of `import`/`require`).
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

## ADDED Requirements

### Requirement: Each exports condition carries the declarations of its own module format
Every object entry of `exports` in `clients/typescript/package.json` (`"."` and `"./e2b"`) SHALL contain exactly the conditions `import` and `require`, each an object with `types` and `default`: `import.types` ending in `.d.mts` and `import.default` in `.mjs`, `require.types` ending in `.d.cts` and `require.default` in `.cjs`, with no `types` condition beside them (TypeScript matches conditions in object order and assumes the declaration file describes the format of the runtime file it pairs with). The top-level `types` field SHALL point at `./dist/index.d.cts`, the declarations of `main`, and `exports` SHALL also map `"./package.json"` to itself. `scripts/pack-check.mjs` SHALL fail, naming the subpath and the field, when an entry breaks this shape.

#### Scenario: CommonJS consumers get CommonJS declarations
- **WHEN** `@arethetypeswrong/cli --pack .` runs over the built package
- **THEN** `"rayito"` and `"rayito/e2b"` resolve to `.d.cts` under `node16` from CommonJS, to `.d.mts` under `node16` from ESM, and under `bundler`, with no "Masquerading as ESM" problem

#### Scenario: the pack check rejects a sibling types condition
- **WHEN** `pnpm pack:check` runs with an entry shaped `{ "types": …, "import": …, "require": { … } }`
- **THEN** it exits non-zero naming the subpath, the extra `types` condition and the `import` fields that do not end in `.d.mts`/`.mjs`

## MODIFIED Requirements

### Requirement: Package layout, toolchain and gates
The TypeScript client SHALL live in `clients/typescript` as the npm package `rayito` (version `0.0.5`, the same SDK generation as the Python package, requiring image ≥ 10.0), `type: "module"`, `engines.node >= 20`, `"license": "Apache-2.0"`, a `repository` object pointing at `https://github.com/alejandro-cedeno-10/rayito` with `directory: "clients/typescript"`, `files` listing `dist`, `README.md`, `LICENSE` and `NOTICE`, managed with pnpm (`packageManager` pinned, lockfile committed), built by `tsdown` into ESM (`dist/index.mjs`, `dist/index.d.mts`) and CJS (`dist/index.cjs`, `dist/index.d.cts`) with an `exports` map, type-checked with `tsc --noEmit` under `strict`, `exactOptionalPropertyTypes`, `noUncheckedIndexedAccess`, `verbatimModuleSyntax`, `module: NodeNext`, linted and formatted with Biome, tested with vitest. `clients/typescript/LICENSE` SHALL hold the Apache-2.0 text and `clients/typescript/NOTICE` the root `NOTICE`, both byte-identical to the root files. `pnpm install`, `pnpm build`, `pnpm test`, `pnpm lint`, `pnpm typecheck` and `pnpm pack:check` SHALL all exit 0 on Windows and Linux; `make test`, `make lint` and CI SHALL run them. Identifiers SHALL be English; user-facing messages SHALL be Spanish; no inline comments inside function bodies.

#### Scenario: gates are green
- **WHEN** a developer runs `pnpm install --frozen-lockfile && pnpm lint && pnpm typecheck && pnpm build && pnpm test && pnpm pack:check` in `clients/typescript`
- **THEN** every command exits 0, `dist/` contains `index.mjs`, `index.cjs`, `index.d.mts` and `index.d.cts`, and the `pnpm pack` listing printed by `pack:check` contains only `package/dist/**`, `package/README.md`, `package/package.json`, `package/LICENSE` and `package/NOTICE`

#### Scenario: both module systems load
- **WHEN** a Node 20 script does `import { Sandbox } from "rayito"` and another does `const { Sandbox } = require("rayito")` against the packed tarball
- **THEN** both resolve a class with static `create`, `connect` and `list`, and `Sandbox.create` is a function in both

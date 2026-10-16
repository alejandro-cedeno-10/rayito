## ADDED Requirements

### Requirement: Executables are additive and the library entry points do not change
The npm package SHALL add a `bin` field with `rayito` and `rayito-mcp` built as ESM-only bundler entries with a shebang, SHALL keep `exports` limited to `.`, `./e2b` and `./package.json`, SHALL keep `sideEffects: false`, and no module reachable from `src/index.ts` or `src/e2b/index.ts` SHALL import an executable entry.

#### Scenario: the library surface is unchanged
- **WHEN** `exports` and the public exports guard are compared before and after adding the executables
- **THEN** they are equal

### Requirement: TypeScript offers no synchronous API
The TypeScript SDK SHALL remain asynchronous only. It SHALL NOT offer a blocking variant of any method, built on `Atomics.wait`, a worker or `child_process.execFileSync`, and the TypeScript reference SHALL document the decision with a top-level `await` example.

#### Scenario: Async classes need a synchronous twin only in Python
- **WHEN** `check_sdk_parity.py` runs
- **THEN** every Python `Async*` class has its synchronous twin and none requires a TypeScript counterpart

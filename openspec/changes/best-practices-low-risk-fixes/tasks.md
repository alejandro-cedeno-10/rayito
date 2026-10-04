## 1. TypeScript packaging

- [x] 1.1 `exports` map with `types` nested in `import`/`require`,
  top-level `types` → `./dist/index.d.cts`, `./package.json` exported.
- [x] 1.2 `scripts/pack-check.mjs` asserts every object entry has exactly
  `import` (`.d.mts` + `.mjs`) and `require` (`.d.cts` + `.cjs`); verified
  it rejects the previous map.
- [x] 1.3 arethetypeswrong 0.18.5 over the packed tarball: `node16` from CJS
  and from ESM and `bundler` green for `rayito` and `rayito/e2b` (only the
  legacy `node10` subpath stays unresolved, as before); a packed install
  loads both entries with `require` and `import`.
- [x] 1.4 `@opentelemetry/sdk-trace-base` ^2.11.0 (dev); otel tests use
  `parentSpanContext` and `instrumentationScope`.
- [x] 1.5 `DynamoDbIndex` TSDoc carries the "Coste y activación" block;
  `scripts/cost-declarations/metadata-index.json` registers the class.

## 2. Python and Rust

- [x] 2.1 `CONNECT_TIMEOUT_SECONDS`/`READ_TIMEOUT_SECONDS` in `_aws.py`.
- [x] 2.2 `cli/template.py` defaults from `DEFAULT_MEMORY_MIB`,
  `DEFAULT_BUILD_TIMEOUT_SECONDS` and `BUILD_LOG_LINES`;
  `cli/_checks.py` reports `TOKEN_TTL_MINUTES`.
- [x] 2.3 One `REDACTED` per SDK: `_aws_sanitize.py` imports `_models.REDACTED`;
  `aws/sanitize.ts` and `pool/core.ts` import `models.ts`'s.
- [x] 2.4 `#![forbid(unsafe_code)]` in `crates/rayd-core/src/lib.rs`.

## 3. Gates and docs

- [x] 3.1 `check_hygiene.py` macOS home and temporary-path rule, with
  positive and negative cases (samples built from pieces).
- [x] 3.2 `python-versions` CI job (3.11, 3.13); `CONTRIBUTING.md` recipe.
- [x] 3.3 `docs/RELEASING.md` intro; TypeScript `CHANGELOG.md` entries
  (the package metadata URLs came with #95).
- [x] 3.4 Local gates: Rust fmt/clippy/test in the Linux VM; Python unit
  (3.11, 3.13, 3.14) + ruff + mypy; scripts tests; TypeScript
  lint/typecheck/build/test/pack:check; docs; OpenSpec validate.
- [ ] 3.5 CI green on the PR; no AWS run (no runtime change).

## Why

The October 2026 best-practices audit
(`docs/research/2026-10-best-practices-audit.md`) compared the SDKs and
`rayd` against the primary sources for open-source client libraries
(Python Packaging Guide and typing guide, the TypeScript modules reference,
Node's packages docs, the Rust API Guidelines, OpenSSF Scorecard). Most of
the repository complies; a handful of deviations are small, mechanical and
safe to fix now. The larger findings stay in the audit as a ranked list.

## What Changes

- **TypeScript `exports` map** (`clients/typescript/package.json`): each
  condition carries its own declarations (`import` → `.d.mts`,
  `require` → `.d.cts`) instead of a sibling `types` that TypeScript picks
  before both, which served ESM declarations to CommonJS consumers under
  `node16`/`nodenext` ("Masquerading as ESM" in arethetypeswrong). The
  top-level `types` points at the `.d.cts` of `main`, `./package.json` is
  exported, and `scripts/pack-check.mjs` rejects a map that regresses.
  Same runtime files.
- **Dev-only OpenTelemetry SDK** `@opentelemetry/sdk-trace-base` ^1.27 →
  ^2.11: closes GHSA-8988-4f7v-96qf (`@opentelemetry/core` < 2.8.0), the
  one vulnerability OpenSSF Scorecard reports; two test assertions follow
  the 2.x span API. Nothing published changes.
- **`DynamoDbIndex` cost block** reaches the IDE hover (it lived in the
  module comment, which tsdown drops) and joins the
  `check-dts-cost-blocks` registry.
- **No magic values, no duplication** (Python): botocore timeouts become
  `CONNECT_TIMEOUT_SECONDS`/`READ_TIMEOUT_SECONDS` (parity with
  `CONNECTION_TIMEOUT_MS`/`REQUEST_TIMEOUT_MS`), `rayito template build`
  and `rayito template logs` default to the SDK constants instead of
  repeating `2048`/`1800.0`/`500`, `rayito doctor` reports
  `TOKEN_TTL_MINUTES`, and the `REDACTED` marker has one definition per SDK
  (`_models.py` / `models.ts`).
- **`rayd-core` forbids `unsafe`**: the hexagonal domain has none; every
  FFI block lives in the `rayd` adapters.
- **Hygiene gate**: `scripts/check_hygiene.py` also reports a macOS home
  path (`/Users/<name>`) and macOS temporary paths (`tmp`/`var` under
  `/private/`), the forms a macOS workstation leaks.
- **Declared Python versions are tested**: a `python-versions` CI job runs
  the unit suite on 3.11 and 3.13 (`check` already covers 3.12).
- **Docs**: `docs/RELEASING.md` no longer says nothing is published yet
  (keeping the versioning-policy link #95 added); `CONTRIBUTING.md` lists
  the new gate. (The `Documentation`/`homepage`/`bugs` metadata the audit
  also flagged landed first in #95.)

## Capabilities

### Modified Capabilities

- `typescript-sdk`: the `rayito/e2b` entry's exports shape; new
  requirement for per-condition declarations.
- `ci-hardening`: the hygiene gate's local-path rule.
- `python-release`: the unit suite runs on every declared Python minor.

## Impact

- TypeScript consumers in CommonJS projects get the CommonJS declarations;
  ESM and bundler resolution are unchanged; runtime files are identical.
- No Python or `rayd` behaviour change; no AWS cost; no new runtime
  dependency.

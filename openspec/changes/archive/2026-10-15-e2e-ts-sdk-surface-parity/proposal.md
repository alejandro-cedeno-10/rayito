## Why

The Python e2e suite exercises the whole SDK surface against real AWS; the
TypeScript suite does not. The M1 to M4 acceptance blocks (transport auth,
processes, filesystem, code execution) exist in TypeScript only as a compact
slice inside `m6.e2e.test.ts`, and several options that the TypeScript SDK
translates by itself (`cpuTimeLimit`, `egress`, `maxBytes`, the E2B shim's
lifecycle and language gates) have no TypeScript e2e at all. A regression in
the TypeScript transport, `commands`, `files` or `runCode` could ship with a
green TypeScript e2e.

## What Changes

- Five new e2e files: `transport`, `processes`, `filesystem`, `code` (mirrors
  of M1 to M4, one shared sandbox each) and `hardening` (`cpuTimeLimit` and
  the `egress` connector).
- Extensions of five existing files: the E2B shim on a live sandbox
  (`getMetrics`, `setTimeout`, `Unimplemented` gates, metadata across pause and
  connect, `NotFoundError` after kill), bash timeout, the shim on `poly` and
  `bash` in the Unimplemented loop, upload size caps, the older-agent gate and
  the shim's fail-closed `allowInternetAccess: false` on `rayito-base`.
- Shared helpers (`HOME`, `waitForTerminalState`, `cpuTimeLimit` and `egress`
  options of `createTestSandbox`, parametrised `openRawClients`) instead of
  copies per file.
- Tests only: no runtime, no public API and no `rayd` change.

## Impact

- `clients/typescript/tests/e2e/` and the TypeScript CHANGELOG
  (`[Unreleased]`, "Añadido"; tests are not shipped).
- TypeScript e2e goes from 85 to about 111 tests; about $0.22 more per full
  run, and $0 for the cases gated on `RAYITO_EGRESS_CONNECTOR_ARN` and
  `RAYITO_E2E_PRE_M9_TEMPLATE_VERSION` when those are unset.
- Out of scope: a TypeScript CLI, MCP server, proxy and dashboard (they need
  an ADR), a synchronous TypeScript surface, and the guest or image checks that
  Python already runs once.

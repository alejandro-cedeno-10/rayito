## Context

ADR-027 holds the decision record. This file keeps the design details the
phases need. Starting point: the Python CLI has 33 leaf commands under
`clients/python/src/rayito/cli/` (`typer`, extra `cli`), the MCP server has six
tools (`rayito/mcp/`, extra `mcp`), and the dashboard core lives in
`_dashboard/`. TypeScript already has `Fleet`, `Quotas`, `UsageCost`,
`OptionalStacks`, `Template`, `AgentTemplate`, `CustomDomain`,
`LifecycleEvents` and the image gateway; it lacks the dashboard API, doctor,
publish/prune, the proxy framing and the interactive terminal.

## Decisions

- **D1 — npm executables.** `"bin": {"rayito": "./dist/cli.mjs",
  "rayito-mcp": "./dist/mcp.mjs"}`, two new ESM-only bundler entries with a
  shebang. `exports` is untouched and `sideEffects: false` stays: the
  executables are never imported from `index`. `rayito --version` reports the
  runtime (`node` or `python`) so a double global install is diagnosable.
- **D2 — Argument parser.** `node:util parseArgs` with a declarative tree
  (`defineCommand({ path, help, options, run })`). No third-party parser. The
  CLI is a shell over public SDK services; new logic is pure domain plus an
  adapter. Defaults are the SDK constants. `--json` prints a single document
  on stdout and progress on stderr. Exit codes are the Python ones (2 for
  usage and environment, 1 for a failed operation).
- **D3 — Command order.** Priority A (most used, lowest risk): `sandbox
  list|info|kill|logs|metrics|create|exec` (non-interactive), `stack *`,
  `image list|sizes`, `doctor`. B: `template`, `agent template build`,
  `events`, `domain`, `dashboard cloudwatch`. C: `sandbox connect`, `exec -it`
  and `sandbox proxy`. D: `image publish|prune|zip`.
- **D4 — One source of truth, no versioned copies.** The canonical dashboard
  SPA stays in the Python package (the wheel and the Lambda need it); the
  TypeScript build copies it to `dist/dashboard-static/` and `pack-check`
  compares tarball and source byte for byte. The `/api/v1` schema stays
  `testdata/dashboard/api-v1.schema.json`; phases 7 and 8 add
  `api-v1-cases.json`, `guard-vectors.json`, `security-headers.json` and
  `theme-vectors.json`, executed by both SDKs. Limits (`DASHBOARD_*`, `MCP_*`,
  proxy timeouts) go in `limits.json` and `gen_limits.py`.
- **D5 — MCP.** `@modelcontextprotocol/server` (v2) as an optional peer loaded
  with `loadOptionalPeer`, plus `zod` as peer. The 1.x SDK is rejected for its
  direct dependency count; a hand-written JSON-RPC stdio server is rejected
  because it would track protocol versions, negotiation, cancellation,
  progress, structured output and HTTP sessions by hand on an exposed surface.
  Same six tools, annotations, `RAYITO_MCP_*` variables and lease; logs only to
  stderr and never payloads. `--http` serves the web-standard handler
  (`createMcpHandler`) on `node:http`, verified viable in phase 6, so
  `@modelcontextprotocol/node` is not needed; `Host` and `Origin` must equal
  the listening authority exactly (the SDK helpers ignore the port). A missing
  peer error carries the exact `npx` command.
- **D6 — Dashboard security parity.** Loopback only (`--host` rejected); a
  foreign `Host` is 421 and a foreign or `null` `Origin` is 403; one-time start
  token with TTL exchanged for an `HttpOnly; SameSite=Strict` cookie and a 303
  to the clean URL; `crypto.timingSafeEqual` for secrets; `Sec-Fetch-Site` must
  be `same-origin` or `none`; `X-Rayito-CSRF` on POST and billed-cost routes;
  POST requires `application/json`; body and request limits from `limits.json`;
  same CSP without `unsafe-inline`; read-only unless `--allow-actions`; nothing
  logged. `ProxyAccess` and the HTTP framing are ported once and shared by the
  proxy and the dashboard.
- **D7 — Parity checker.** A pytest test walks the `typer` tree and writes or
  checks `testdata/cli/surface.json` (path, options, types, defaults,
  required, choices). A vitest test compares the `defineCommand` tree with that
  JSON. `scripts/check_sdk_parity.py --cli` stays standard-library only and
  reads the golden plus `CLI_PHASES`; in phase 0 it only reports. When the last
  phase lands it fails on a golden command the TypeScript tree lacks and on a
  stale `CLI_PHASES` entry. The same pattern applies to MCP with
  `testdata/mcp/tools.json`.
- **D8 — No synchronous API in TypeScript.** `Atomics.wait` with a worker
  blocks the event loop, needs structured-cloneable arguments (no callbacks,
  streams, PTY, `AbortSignal` or live handles), needs a cross-thread proxy
  registry, does not work in the browser or the Electron main thread and
  doubles surface and tests. `child_process.execFileSync` spawns one process
  per call, reuses neither gRPC nor the token cache, cannot stream and moves
  secrets through argv or the environment. E2B's JavaScript SDK is async-only
  too. The `ASYNC_PREFIX` rule of `check_sdk_parity.py` already encodes it.

## Risks

- Two global installs collide on the name `rayito`: `--version` names the
  runtime and the docs warn about it.
- The transport API of `@modelcontextprotocol/server` v2 is recent: pin the
  version and check the `node:http` adapter in phase 6.
- Duplicated security surface: shared vectors and a `SECURITY.md` review before
  phases 4 and 8.
- The dashboard Lambda stays Python and the canonical SPA stays in the Python
  package during the transition.
- The golden changes whenever a Python CLI option changes: the pytest test
  fails with the regeneration command, and the diff is reviewed as public
  surface.

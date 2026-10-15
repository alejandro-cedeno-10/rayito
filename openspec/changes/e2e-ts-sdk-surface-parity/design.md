## Context

Python has 132 real e2e tests and TypeScript 85. Most of the gap is not a
missing test but a missing block inside a test: Python's M1 to M5 acceptance
tests are six large functions of 18 to 20 `check_*` blocks each, and the
TypeScript equivalent is two functions in `m6.e2e.test.ts`.

## Decisions

### D1. Classify every Python test before porting

Each Python e2e test falls in one class, and only (a) is ported:

- (a) SDK code that TypeScript implements itself: transport auth levels,
  `commands`, `files`, `runCode`, payload options, the E2B shim.
- (b) guest, `rayd` or image behaviour (identity, rlimits, root policy, path
  denylist, kernel readiness timings, PRNG, hardening of the guest): tested
  once, in Python, because the same binary answers both SDKs.
- (b-tool) CLI, MCP, proxy and dashboard, which only exist in Python.
- (c) the synchronous/asynchronous duplicate of a block, and
  `check_async_parity`: TypeScript is asynchronous only.

### D2. Shape of the new tests

- One shared sandbox per new file (`beforeAll` + `createTestSandbox`), tests
  in the order of the Python blocks, each test named after the behaviour.
- Every duration is a named constant in milliseconds; budgets copy the
  Python ones. No environment value is written in the code: optional gates
  read `RAYITO_EGRESS_CONNECTOR_ARN` and `RAYITO_E2E_PRE_M9_TEMPLATE_VERSION`,
  the same names as Python, and `skipIf` them with the variable in the title.
- `describe.skipIf(!e2eEnabled())`, so without `RAYITO_E2E` nothing runs and
  no AWS call is made.
- Helpers are shared: `waitForTerminalState` (the eventual consistency of
  `get-microvm` after `terminate-microvm`), `HOME`, and the `cpuTimeLimit`
  and `egress` pass-through of `createTestSandbox`. `raw-rpc.ts` exposes
  `openRawClients(controlPlane, sandboxId, { accessToken, jwe })` so the
  transport test and the existing stray-request helper share one transport
  setup.

### D3. Matrix of the added tests

| Mirror of (Python) | TypeScript test |
|---|---|
| `test_m1_hello` | `transport.e2e.test.ts`: anonymous Health ok and List `UNAUTHENTICATED`; forged JWE is a proxy 403 (`isProxyForbidden`); authenticated Resize on an unknown pid is `NOT_FOUND`; reconnect; kill |
| `test_m2_processes` | `processes.e2e.test.ts` (6): envs/cwd and callbacks; background tag, list, kill 137, `sendStdin` without stdin; `connect` with `fromSeq` and unknown pid; 3 MB output and a 35 s sleep through the proxy; `getHost` 200/403; impostor token and metrics |
| `test_m3_filesystem` | `filesystem.e2e.test.ts` (6): 50-file batch and 8 MB stream in at least 30 chunks; list types, symlink, remove; watch shell events; watch callback with entries; watch errors and timeout; recursive watch |
| `test_m4_code` | `code.e2e.test.ts` (5): stderr and `onStdout`; timeout keeps state and silent cell; contexts, envs, per-run envs; restart/remove errors; `KernelDied` recovery |
| `test_m6_hardening` (cpu, egress) | `hardening.e2e.test.ts` (2) |
| `test_m6_e2b_compat` steps 10 to 12 | `m9-e2b.e2e.test.ts` (3, on one live shim sandbox) |
| `test_m7_poly_kernels`, `test_m9_deno_kernels` | `poly.e2e.test.ts` (2 tests and `bash` in the base loop) |
| `test_m9_transfer::test_size_caps` | `m9-transfer.e2e.test.ts` (1) |
| `test_m9_server_timeout::test_older_agent_gate` | `m9-timeout.e2e.test.ts` (1) |
| `test_m9_egress` shim on base | `m9-egress.e2e.test.ts` (1) |

### D4. Image prerequisites

Like their Python mirrors, the cases that need the M7 and M9 agent (bash and
Deno kernels, the shim's lifecycle, an atomic write landing as one `write`
watch event, `getInfo` metadata) require an image whose agent is 0.3.0 or
later and, for `poly` and the egress policy, the `rayito-base-poly` and
`rayito-base-caps` variants. On an older image they fail on the gate, not on
the SDK, so run them against a current image.

### D5. Not decided here

A `check_e2e_parity.py` that fails when a Python e2e test has no
`Paridad:` line is left for a follow-up: it needs the line in 35 Python
files and would dwarf this change.

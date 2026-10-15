## 1. Shared helpers

- [x] 1.1 `helpers.ts`: `HOME`, `waitForTerminalState`, `cpuTimeLimit` and
      `egress` options of `createTestSandbox`.
- [x] 1.2 `raw-rpc.ts`: exported `openRawClients` with `{ accessToken, jwe }`
      and the `PtyService` client.

## 2. New files

- [x] 2.1 `transport.e2e.test.ts` (mirror of M1).
- [x] 2.2 `processes.e2e.test.ts` (mirror of M2, 6 tests).
- [x] 2.3 `filesystem.e2e.test.ts` (mirror of M3, 6 tests).
- [x] 2.4 `code.e2e.test.ts` (mirror of M4, 5 tests).
- [x] 2.5 `hardening.e2e.test.ts` (`cpuTimeLimit` and `egress`).

## 3. Extensions

- [x] 3.1 `m9-e2b`: shim metrics, `setTimeout`, Unimplemented gates,
      metadata across pause and connect, `NotFoundError` after kill.
- [x] 3.2 `poly`: bash timeout, shim on poly, `bash` in the base loop.
- [x] 3.3 `m9-transfer`: size caps.
- [x] 3.4 `m9-timeout`: older-agent gate.
- [x] 3.5 `m9-egress`: shim fails closed on `rayito-base`.

## 4. Acceptance

- [x] 4.1 Gates: `pnpm lint`, `typecheck`, `test`, `check_hygiene.py`,
      `openspec validate --strict`.
- [x] 4.2 Run the new and changed files on real AWS, with the inventory of
      MicroVMs before and after, and clean up only what the run created.
- [x] 4.3 Run the cases that need an agent 0.3.0 or later, `rayito-base-poly`
      and `rayito-base-caps` against a current image before archiving.

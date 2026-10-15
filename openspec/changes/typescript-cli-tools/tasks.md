## 0. Phase 0: ADR, change and contract (`docs/ts-cli-adr`)

- [x] 0.1 ADR-027 in `ARCHITECTURE.md`, including D8; point 6 of ADR-026
      points to it.
- [x] 0.2 This OpenSpec change (proposal, design, tasks, specs).
- [x] 0.3 `testdata/cli/surface.json` and
      `clients/python/tests/unit/cli/test_cli_surface.py`, which keeps it in
      sync.
- [x] 0.4 `scripts/check_sdk_parity.py --cli` reports every golden command as
      pending with its phase; it does not fail CI.
- [x] 0.5 `cli.md` and `parity-checklist.md` no longer call a TypeScript CLI
      out of scope.

## 1. Phase 1: CLI core (`feat/ts-cli-core`)

- [ ] 1.1 `bin`, bundler entries, `defineCommand`, console, error translation,
      exit codes and global options.
- [ ] 1.2 Priority A commands without `doctor`.
- [ ] 1.3 `surface.test.ts` against the golden.

## 2. Phase 2: doctor (`feat/ts-cli-doctor`)

- [ ] 2.1 Port `_checks.py` and `_compat.py`; the compatibility table moves to
      shared test data.

## 3. Phase 3: stacks and templates (`feat/ts-cli-stacks`)

- [ ] 3.1 `template`, `agent template build`, `events`, `domain`,
      `dashboard cloudwatch`.

## 4. Phase 4: interactive (`feat/ts-cli-interactive`)

- [ ] 4.1 PTY with `setRawMode`, `access.ts`, `http-framing.ts`, `sandbox
      proxy` and shared header-smuggling vectors.
- [ ] 4.2 `SECURITY.md` review.

## 5. Phase 5: images (`feat/ts-cli-images`)

- [ ] 5.1 `image publish|prune|zip` as pure domain plus adapter.

## 6. Phase 6: MCP (`feat/ts-mcp`)

- [ ] 6.1 `src/mcp/` (settings, lease, results, server, main), optional peers,
      `--http`, `testdata/mcp/tools.json`.

## 7. Phase 7: dashboard core (`feat/ts-dashboard-core`)

- [ ] 7.1 Contract, router, jobs, serialisation, theme, accounts/assume, logs
      and CloudWatch board, with `api-v1-cases.json` and `theme-vectors.json`.

## 8. Phase 8: local dashboard (`feat/ts-dashboard-local`)

- [ ] 8.1 `dashboard-server.ts`, SPA copy and byte-for-byte pack check,
      `guard-vectors.json`, `security-headers.json`, `--allow-actions` and
      `--open`.
- [ ] 8.2 `SECURITY.md` review.

## 9. Phase 9: docs (`docs/ts-cli`)

- [ ] 9.1 `cli.md`, `mcp.md` and the dashboard pages with Python/TypeScript
      tabs, `optional-features.md` row, `cost-declarations/dashboard.json`,
      CHANGELOG of both packages.
- [ ] 9.2 `check_sdk_parity.py --cli` becomes a failing check.

## 10. Acceptance

- [ ] 10.1 One real-AWS pass after every phase is merged into the integration
      branch (`cli`, `cli-proxy`, `cli-templates`, `mcp` over stdio and
      `--http`, read-only dashboard); data only through `RAYITO_E2E_*`. Delete
      only the VMs and image versions of that run.

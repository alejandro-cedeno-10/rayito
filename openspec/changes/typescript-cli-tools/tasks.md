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

- [x] 1.1 `bin`, bundler entries, `defineCommand`, console, error translation,
      exit codes and global options.
- [x] 1.2 Priority A commands without `doctor`: `sandbox list|info|kill|logs|
      metrics|exec|create --detach`, `stack *` and `image list|sizes`.
      Interactive `create` (no `--detach`) waits for phase 4.
- [x] 1.3 `surface.test.ts` against the golden.
- [x] 1.4 Shared vectors and command cases (`format-vectors`, `input-vectors`,
      `document-vectors`, `command-cases`) that both CLIs run, and `rayito
      --version` naming the runtime in both.

## 2. Phase 2: doctor (`feat/ts-cli-doctor`)

- [x] 2.1 Port `_checks.py` and `_compat.py`; the compatibility table moves to
      shared test data (`testdata/cli/compatibility.json`, plus
      `doctor-contract.json` for the check names, IAM actions and thresholds).

## 3. Phase 3: stacks and templates (`feat/ts-cli-stacks`)

- [x] 3.1 `template`, `agent template build`, `events`, `domain`,
      `dashboard cloudwatch`. The CloudWatch board has shared byte-for-byte
      cases (`testdata/dashboard/cloudwatch-board-cases.json`) and the
      stack-backed orders run the shared `command-cases.json`.

## 4. Phase 4: interactive (`feat/ts-cli-interactive`)

- [x] 4.1 PTY with `setRawMode`, `access.ts`, `http-framing.ts`, `sandbox
      proxy` and shared header-smuggling vectors.
- [x] 4.2 `SECURITY.md` review.

## 5. Phase 5: images (`feat/ts-cli-images`)

- [x] 5.1 `image publish|prune|zip` as pure domain plus adapter
      (`images/publish-domain.ts`, `prune-domain.ts` and `artifact.ts` are pure;
      `publish-service.ts` and `prune-service.ts` run over ports; `aws.ts` and
      `artifact-node.ts` are the adapters). The pure contracts are shared
      with Python in `testdata/images/` (publish vectors, prune plans, zip
      entries).

## 6. Phase 6: MCP (`feat/ts-mcp`)

- [x] 6.1 `src/mcp/` (settings, lease, results, server, main), optional peers,
      `--http`, `testdata/mcp/tools.json`.

## 7. Phase 7: dashboard core (`feat/ts-dashboard-core`)

- [x] 7.1 Contract, router, jobs, serialisation, theme, accounts/assume, logs
      and CloudWatch board, with `api-v1-cases.json`, `theme-vectors.json` and
      `cloudwatch-board-vectors.json`.

## 8. Phase 8: local dashboard (`feat/ts-dashboard-local`)

- [x] 8.1 `dashboard-server.ts`, SPA copy and byte-for-byte pack check,
      `guard-vectors.json`, `security-headers.json`, `--allow-actions` and
      `--open`.
- [x] 8.2 `SECURITY.md` review.

## 9. Phase 9: docs (`docs/ts-cli`)

- [x] 9.1 `cli.md`, `mcp.md` and the dashboard pages with Python/TypeScript
      tabs, `optional-features.md` row, CHANGELOG of both packages. No
      `cost-declarations` entry: the executables export no symbol into
      `dist/*.d.mts`; their cost text lives in `--help` and the docs boxes.
- [x] 9.2 `check_sdk_parity.py --cli` becomes a failing check.

## 10. Acceptance

- [ ] 10.1 One real-AWS pass after every phase is merged into the integration
      branch (`cli`, `cli-proxy`, `cli-templates`, `mcp` over stdio and
      `--http`, read-only dashboard); data only through `RAYITO_E2E_*`. Delete
      only the VMs and image versions of that run.

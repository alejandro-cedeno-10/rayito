## ADDED Requirements

### Requirement: e2b-parity.md rows 82 and 110 reflect the M12 alternatives without overstating scope
`docs/site/docs/e2b-parity.md` row 82 (`cpu_count`/`memory_mb` por sandbox) SHALL read status "divergente" with a note naming the per-image alternative (`rayito image publish --memory-mib`, choosing the resulting image as the template), that `Template.build(cpu_count=, memory_mb=)` still raises `UnimplementedError`, that `SandboxInfo.cpu_count`/`memory_mb` report the guest's view (measured `Q68`) rather than the contracted baseline, and a link to `limits.md#tamano-cpuram`.

Row 110 (dominio propio vía proxy inverso) SHALL keep status "fuera por SPEC" as long as ADR-014 (owned by the sibling `m11-optin-adr` change) does not permit a hosted ingress; its note SHALL point to `rayito sandbox proxy <id> --port N` as the local-development alternative and SHALL state that a public domain remains an optional, not-included add-on for the customer's own account.

The status-count table (`implementado`/`divergente`/`fuera por SPEC`/`imposible en la plataforma`) SHALL be recomputed to match (18 divergente, 12 fuera por SPEC), and `docs/site/docs/e2b-compat.md`'s cpu/memory footnote row SHALL carry the same per-image alternative, separated from the unrelated "CLI de templates/snapshots/fork" row it used to share. `scripts/tests/test_m9_docs.py::test_the_parity_page_has_every_ledger_row` SHALL stay green (113 rows, numbered 1..113, every status one of the four valid ones).

#### Scenario: row 82 names the per-image alternative and stays truthful about Template.build
- **WHEN** a reader opens `e2b-parity.md` row 82
- **THEN** its status is "divergente", its note names `--memory-mib` and `Q68`, and it does not claim `Template.build(cpu_count=, memory_mb=)` works

#### Scenario: row 110 points at the local proxy without claiming a hosted domain
- **WHEN** a reader opens `e2b-parity.md` row 110
- **THEN** its note names `rayito sandbox proxy` and states that a public domain is a not-included, optional add-on, and its status is unchanged ("fuera por SPEC") unless ADR-014 says otherwise

#### Scenario: the ledger test still passes
- **WHEN** `scripts/tests/test_m9_docs.py::test_the_parity_page_has_every_ledger_row` runs after this change
- **THEN** it passes: rows numbered 1..113 and every status one of the four valid prefixes

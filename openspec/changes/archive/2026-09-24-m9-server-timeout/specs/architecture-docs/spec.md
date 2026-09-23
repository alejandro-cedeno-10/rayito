## ADDED Requirements

### Requirement: ADR-011 records the server-enforced logical deadline
`ARCHITECTURE.md` SHALL contain a section `ADR-011 — Plazo lógico impuesto por rayd; el tope de la plataforma se elige en create()` stating:

- the context: ADR-007, the Q58 measurement that `rayd` exiting terminates the VM about 15 s later, and the monotonic clock of `AWS_API_NOTES.md` §15
- the decision: the payload block, the `sandbox_timeout` domain module, the watcher thread, kill and pause behaviour, the resume grace, E2B's auto-resume rule, `SetTimeout`, `Health.lifecycle` and the SDK deadline trigger
- the honest costs: the cap counts suspended time; processes run between the pause deadline and the suspension; idle suspension happens before the deadline; about 15 s of 502 is billed after a kill-mode exit; the 60 s cap margin

The ADR-007 section SHALL keep its body and gain the line `**Sustituida por ADR-011**`. `SPEC.md` §4 SHALL NOT list `set_timeout` as a non-goal. `openspec/project.md` hard rule 3 SHALL NOT list "no `set_timeout`".

#### Scenario: docs gate
- **WHEN** `scripts/tests/test_lifecycle_docs.py` reads `ARCHITECTURE.md`, `SPEC.md` §4 and `openspec/project.md`
- **THEN** ADR-011 exists, ADR-007 carries the superseded line, and neither SPEC §4 nor hard rule 3 names `set_timeout`

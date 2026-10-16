## ADDED Requirements

### Requirement: ADR-027 records the command-line tools in both SDKs
`ARCHITECTURE.md` SHALL contain ADR-027, which states that TypeScript ships the `rayito` CLI, the `rayito-mcp` server and the local dashboard with the same surface, names the shared golden as the contract, records decision D8 (no synchronous TypeScript API) with the options studied, lists the phases, and supersedes point 6 of ADR-026. Point 6 of ADR-026 SHALL point to ADR-027. `cli.md` and the parity checklist SHALL NOT call a TypeScript CLI out of scope.

#### Scenario: the superseded statement is gone
- **WHEN** the documentation is searched for the statement that the Python CLI is the only CLI
- **THEN** the only occurrence is the pointer from ADR-026 to ADR-027

## ADDED Requirements

### Requirement: ADR-026 records the optional in-account management dashboard
`ARCHITECTURE.md` SHALL carry ADR-026 "Panel de gestión opcional en la cuenta del cliente" stating: it runs only in the customer account, with no Rayito server and no billing; it is off by default; it is read-only by default with gated actions; one versioned API and one SPA serve all phases; and the phases 0 to 3. `SPEC.md` §4 SHALL exclude only billing and a Rayito-hosted panel, and SHALL reference ADR-026.

#### Scenario: ADR and non-goal are consistent
- **WHEN** `ARCHITECTURE.md` and `SPEC.md` §4 are read
- **THEN** ADR-026 exists and the non-goal no longer excludes an in-account optional dashboard

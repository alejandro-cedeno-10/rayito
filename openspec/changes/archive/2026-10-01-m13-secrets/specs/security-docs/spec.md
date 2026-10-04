## ADDED Requirements

### Requirement: SECURITY.md documents user secret custody as T18
`SECURITY.md` SHALL carry a T18 row ("custodia de secretos del usuario") covering the channel (per-call `envs` only; never `runHookPayload`, `metadata`, logs or errors), data at rest (KMS in Secrets Manager, SDK process memory only for the cache, the VM environment and the suspend snapshot, SEC-5 open), who reads what (the caller's IAM, never the execution role; in phase 1 any uid 1000 code can read an injected secret), and the recommendation for untrusted code (short-lived, least-privilege tokens; no long-lived credentials). `docs/site/docs/security.md` SHALL have a matching section.

#### Scenario: T18 is present and complete
- **WHEN** `scripts/tests/test_security_docs.py` runs
- **THEN** the T18 row exists and names the payload exclusion, the in-memory cache, the phase-1 readability, the snapshot and the short-lived-token recommendation, and the site page has the section

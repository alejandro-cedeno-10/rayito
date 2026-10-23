## ADDED Requirements

### Requirement: TypeScript e2e tests never share a secret name
Every TypeScript e2e test SHALL build its secret names with `secretName(scope, kind)`, which returns a name that is unique per run, test and call. Each test SHALL delete only the secrets it created, so that a secret pending deletion from one test never collides with another (`ResourceExistsException`).

#### Scenario: AgentCore and MicroVM in one run
- **WHEN** the AgentCore and MicroVM tests both create an `llm-key` secret in the same run
- **THEN** the two names differ and neither creation fails

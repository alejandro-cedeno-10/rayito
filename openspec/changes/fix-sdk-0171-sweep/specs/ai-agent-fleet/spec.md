## ADDED Requirements

### Requirement: A member whose probe fails while listing is treated as terminating
When a fleet lists its members (`AgentFleet.connect()`, `status()`, `refresh_members()` / `refreshMembers()`, sync, async and TypeScript), each `RUNNING` MicroVM SHALL be probed on its own. A MicroVM whose probe fails with a `SandboxException` / `SandboxError`, or that is no longer `RUNNING` when probed, SHALL NOT be a member, SHALL NOT count for capacity and SHALL NOT make the image count as shared, and the listing SHALL NOT fail because of it; a warning SHALL name its id and the error class only. Any other error SHALL still fail the listing. The pure selection SHALL be tested in both SDKs with `testdata/agent-fleet/members.json`.

#### Scenario: one member is being terminated
- **WHEN** a fleet has two `RUNNING` members and a suspended one, and the `Health` of one running member fails
- **THEN** `connect()` and `refresh_members()` succeed, the fleet has the other running member and the suspended one, and `status()` does not list the failing one

#### Scenario: a member left RUNNING before its probe
- **WHEN** a VM listed as `RUNNING` is `TERMINATING` when probed
- **THEN** it is not a member

## ADDED Requirements

### Requirement: Quotas and UsageCost have SDK parity
`Quotas` and `UsageCost` SHALL exist in Python (sync and async) and TypeScript with the same surface and the same named constants.

#### Scenario: shared fixtures pass in both SDKs
- **WHEN** the shared fixtures for quota utilization and cost grouping run in both test suites
- **THEN** the results are equal

### Requirement: Quota usage warns near limits
The service SHALL report allocated memory (RUNNING plus SUSPENDED) against the per-region quota and the applied TPS per operation, flag usage at or above the named warning thresholds, and include a quota-increase link.

#### Scenario: warning at the threshold
- **WHEN** allocated memory reaches the warning threshold of the quota
- **THEN** the result carries a warning and the increase link

### Requirement: Billed and estimated cost are distinct
Billed cost SHALL come from Cost Explorer grouped by usage type, cached for the TTL in `limits.json`, and only fetched when cost is explicitly enabled. Per-sandbox cost SHALL be labelled estimated.

#### Scenario: cost disabled makes no Cost Explorer call
- **WHEN** the dashboard runs without enabling cost
- **THEN** no Cost Explorer call is made

#### Scenario: estimates are labelled
- **WHEN** a per-sandbox cost is returned
- **THEN** it carries the estimated label

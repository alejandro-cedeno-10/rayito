## MODIFIED Requirements

### Requirement: deploy validates parameters against the component's declared set before any AWS call
`OptionalStacks.deploy` SHALL reject (with `InvalidArgumentException`/`InvalidArgumentError`, before calling `StackProvisioner.describe`) any parameter name not declared on the resolved `StackComponent`. It SHALL reject, with the same exception and before calling `StackProvisioner.create` or `update`, any parameter declared `required` with no default that the caller did not pass and the existing stack (if any) does not already have.

#### Scenario: an unknown parameter is rejected before any call
- **WHEN** `deploy("metadata-index", parameters={"TotallyMadeUp": "x"})` is called
- **THEN** `InvalidArgumentException`/`InvalidArgumentError` is raised and the `StackProvisioner` records zero calls

#### Scenario: creating without a required parameter is rejected
- **WHEN** `deploy("s3-mounts")` is called without `BucketName` and `describe` returns no stack
- **THEN** `InvalidArgumentException`/`InvalidArgumentError` is raised and neither `create` nor `update` is called

## ADDED Requirements

### Requirement: redeploying an existing stack keeps the parameters the caller did not pass
`OptionalStacks.deploy` SHALL apply a parameter's catalog default only when it calls `StackProvisioner.create`, or on `update` for a declared parameter the existing stack does not have. On `update`, every declared parameter the caller did not pass and the existing stack already has SHALL be sent with `UsePreviousValue: true` and no `ParameterValue`. `OptionalStacks.parameter_changes`/`parameterChanges` SHALL return the parameters whose value that `deploy` would change, calling only `StackProvisioner.describe`, and `rayito stack deploy` SHALL print them before asking for confirmation.

#### Scenario: s3-mounts keeps its prefixes
- **WHEN** `s3-mounts` was deployed with `Prefixes=team7/*` and `deploy("s3-mounts", parameters={"BucketName": "b"})` is called again
- **THEN** `update` sends `Prefixes` and `ReadOnly` with `UsePreviousValue` and the stack still has `Prefixes=team7/*`

#### Scenario: metadata-index keeps its table
- **WHEN** `metadata-index` was deployed with `TableName=my-table` and `DeletionProtection=true`, and `deploy("metadata-index")` is called again
- **THEN** `TableName`, `DeletionProtection` and `PointInTimeRecovery` are sent with `UsePreviousValue` and the table is not replaced

#### Scenario: secrets-access keeps its KMS key
- **WHEN** `secrets-access` was deployed with `KmsKeyArn` and `deploy("secrets-access")` is called again
- **THEN** `KmsKeyArn` is sent with `UsePreviousValue`

#### Scenario: create still fills the defaults
- **WHEN** `deploy("s3-mounts", parameters={"BucketName": "b"})` is called and `describe` returns no stack
- **THEN** `create` receives `Prefixes='*'` and `ReadOnly='true'`

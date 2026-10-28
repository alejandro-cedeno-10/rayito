## ADDED Requirements

### Requirement: `failure_reason` reads `DescribeStackEvents` and returns the earliest failed resource of the latest operation, redacted, falling back to `StackStatusReason`
`OptionalStacks.failure_reason(component, stack_name=None)` (TypeScript: `failureReason(component, { stackName })`, and the async twin in Python) SHALL resolve the stack name like `status()` and delegate to `StackProvisioner.failure_reason`, whose real adapter SHALL call `DescribeStackEvents` with `StackName` only, read the first page, and map `LogicalResourceId`, `ResourceStatus` and `ResourceStatusReason`. The pure function `first_failed_resource` SHALL walk the events from newest to oldest, stop at the stack's own `CREATE_IN_PROGRESS` or `UPDATE_IN_PROGRESS` event, and return the oldest resource event with `CREATE_FAILED` or `UPDATE_FAILED`. The result SHALL be `<LogicalResourceId> (<ResourceStatus>): <ResourceStatusReason>` passed through `redact_aws_text` / `redactAwsText`; without such a resource it SHALL be the redacted `StackStatusReason`, and `None`/`undefined` if the stack does not exist or has none. The text SHALL never enter an exception message, a log or a `__cause__`. `StackException` / `StackError` SHALL carry the additive attribute `state`, set only by `wait()` to the terminal state of the stack.

#### Scenario: the earliest failed resource wins
- **WHEN** the events hold a `CREATE_FAILED` "Resource creation cancelled" newer than the `CREATE_FAILED` of `Distribution`
- **THEN** the result names `Distribution`

#### Scenario: an earlier operation is ignored
- **WHEN** a failure precedes the `UPDATE_IN_PROGRESS` event of the stack
- **THEN** it is not returned

#### Scenario: nothing failed
- **WHEN** only the stack's own events exist
- **THEN** the result is the redacted `StackStatusReason`, or none

#### Scenario: signatures are redacted
- **WHEN** the reason contains `X-Amz-Signature=<hex>`
- **THEN** the result does not contain the hex value

#### Scenario: the wait stamps the state
- **WHEN** `wait()` finds the stack in `UPDATE_ROLLBACK_COMPLETE`
- **THEN** the raised error has `code == "failed"` and `state == "UPDATE_ROLLBACK_COMPLETE"`, and a rejected `CreateStack`/`UpdateStack` has no `state`

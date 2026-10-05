## ADDED Requirements

### Requirement: Stack errors chain the sanitized AWS summary
The CloudFormation provisioner SHALL raise `StackException`/`StackError` whose cause is the sanitized summary of the AWS error (`raise ... from sanitize_aws_error(exc)` in Python, `cause: sanitizeAwsError(...)` in TypeScript), never the raw `ClientError` or smithy error.

#### Scenario: a signature error on describe-stacks
- **WHEN** `describe_stacks` fails with `InvalidSignatureException` carrying the canonical string
- **THEN** the `StackException` traceback (cause included) contains neither the session token nor the access key id

## ADDED Requirements

### Requirement: The `dashboard` OptionalStack runs only in the customer account
`infra/dashboard.yaml` SHALL be deployed with `rayito stack deploy|status|destroy dashboard`, create no resource unless deployed, and serve the same SPA and API from a CloudFormation-created Lambda Function URL, with no Rayito-operated server.

#### Scenario: destroy removes everything
- **WHEN** `rayito stack destroy dashboard` completes
- **THEN** no dashboard resource remains

### Requirement: The stack authenticates every request and separates roles
Requests SHALL be authenticated by IAM auth or a Cognito JWT (verified in the test account), with a read role and a separate actions role of least privilege. Each action SHALL write an audit row with caller subject, action, resource and time, using a conditional put and no delete permission.

#### Scenario: unauthenticated request rejected
- **WHEN** a request without valid credentials reaches the Function URL
- **THEN** it is rejected before any AWS call

#### Scenario: action is audited
- **WHEN** an authorised user kills a sandbox
- **THEN** one audit row records subject, action, resource and time

### Requirement: The stack is testable under restrictive organisation policies
The default variant SHALL NOT require CloudFront, S3 bucket creation or direct `lambda:CreateFunction`. The CloudFront plus S3 variant SHALL be optional and documented as untested where blocked. Required tags SHALL pass through `deploy --tag`.

#### Scenario: default variant avoids blocked services
- **WHEN** `infra/dashboard.yaml` is linted
- **THEN** it contains no CloudFront distribution or S3 bucket resource

### Requirement: Activation is documented and declared
The feature SHALL ship the "Coste y activación" box, a row in `optional-features.md` and `cost-declarations/dashboard.json`.

#### Scenario: docs gates pass
- **WHEN** `mkdocs build --strict` and the cost-declaration check run
- **THEN** both pass

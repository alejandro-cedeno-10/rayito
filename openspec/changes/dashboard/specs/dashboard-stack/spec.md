## ADDED Requirements

### Requirement: The `dashboard` OptionalStack runs only in the customer account
`infra/dashboard.yaml` SHALL be deployed with `rayito stack deploy|status|destroy dashboard`, create no resource unless deployed, and serve the same SPA and API from a CloudFormation-created Lambda Function URL, with no Rayito-operated server.

#### Scenario: destroy removes everything
- **WHEN** `rayito stack destroy dashboard` completes
- **THEN** no dashboard resource remains

### Requirement: The stack authenticates every API request and separates roles
The default variant SHALL use a Function URL with `AuthType NONE` and validate a Cognito JWT inside the handler (issuer, audience, expiry, JWKS signature, `token_use`) before any routing or AWS call, with the static shell as the only unauthenticated route. The `AWS_IAM` variant with a Cognito identity pool and SigV4 SHALL be optional. The function SHALL run with a read-only execution role and reach a separate actions role with `sts:AssumeRole` only after checking the caller's group claim; the actions role SHALL trust only the function role. Each action SHALL write an audit row with caller subject, action, resource and time, using a conditional put and no delete permission.

#### Scenario: unauthenticated API request rejected
- **WHEN** a request to `/api/v1/*` without a valid token reaches the Function URL
- **THEN** the handler rejects it before any routing or AWS call

#### Scenario: invalid token claims rejected
- **WHEN** the token has a wrong issuer, audience, `token_use`, signature or is expired
- **THEN** the handler rejects it with no AWS call

#### Scenario: action needs the actions group
- **WHEN** a caller without the actions group claim posts a kill
- **THEN** the handler refuses it and does not assume the actions role

#### Scenario: action is audited
- **WHEN** an authorised user kills a sandbox
- **THEN** one audit row records subject, action, resource and time

### Requirement: The stack defends the browser surface
The Function URL CORS policy SHALL allow only its own origin, `Origin` SHALL be checked on every `POST`, the token SHALL travel in the `Authorization` header and not in a cookie, and every response SHALL send `Content-Security-Policy`, `X-Frame-Options: DENY` with `frame-ancestors 'none'` and `Referrer-Policy: no-referrer`.

#### Scenario: foreign origin rejected
- **WHEN** a `POST` arrives with a foreign `Origin`
- **THEN** it is rejected

#### Scenario: security headers present
- **WHEN** the SPA shell or any API response is served
- **THEN** it carries the CSP, framing and referrer headers

### Requirement: Cost Explorer access is a stack parameter
Billed cost SHALL be off by default behind a stack parameter, and `ce:GetCostAndUsage` SHALL be granted only when it is on.

#### Scenario: default role lacks Cost Explorer
- **WHEN** the stack is deployed with default parameters
- **THEN** no role holds `ce:GetCostAndUsage`

### Requirement: The stack is testable under restrictive organisation policies
The default variant SHALL NOT require CloudFront, S3 bucket creation or direct `lambda:CreateFunction`. The CloudFront plus S3 variant SHALL be optional and documented as untested where blocked. Required tags SHALL pass through `deploy --tag`.

#### Scenario: default variant avoids blocked services
- **WHEN** `infra/dashboard.yaml` is linted
- **THEN** it contains no CloudFront distribution or S3 bucket resource

### Requirement: Activation is documented and declared
The feature SHALL ship the "Coste y activación" box, a row in `optional-features.md` and `clients/typescript/scripts/cost-declarations/dashboard.json`.

#### Scenario: docs gates pass
- **WHEN** `mkdocs build --strict` and the cost-declaration check run
- **THEN** both pass

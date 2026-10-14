## ADDED Requirements

### Requirement: The `dashboard` OptionalStack runs only in the customer account
`infra/dashboard.yaml` SHALL be deployed with `rayito stack deploy|status|destroy dashboard`, create no resource unless deployed, and serve the same SPA and API from a CloudFormation-created Lambda Function URL, with no Rayito-operated server.

#### Scenario: destroy removes everything
- **WHEN** `rayito stack destroy dashboard` completes
- **THEN** no dashboard resource remains

### Requirement: The stack authenticates every API request and separates roles
The default variant SHALL use a Function URL with `AuthType NONE` and validate a Cognito JWT inside the handler (issuer, audience, expiry, JWKS signature, `token_use`) before any routing or AWS call, with the static shell as the only unauthenticated route. The audience SHALL be the dashboard's own app client, found by its name, so other app clients of the pool neither break sign-in nor become valid audiences. The `AWS_IAM` variant with a Cognito identity pool and SigV4 SHALL be optional. The function SHALL run with a read-only execution role and reach a separate actions role with `sts:AssumeRole` only after checking the caller's group claim; the actions role SHALL trust only the function role. Before assuming it the handler SHALL confirm in Cognito that the caller is still enabled and still in the actions group, and SHALL refuse to act if Cognito cannot answer. Each action SHALL write an audit row with caller subject, action, resource and time, using a conditional put and no delete permission.

#### Scenario: unauthenticated API request rejected
- **WHEN** a request to `/api/v1/*` without a valid token reaches the Function URL
- **THEN** the handler rejects it before any routing or AWS call

#### Scenario: invalid token claims rejected
- **WHEN** the token has a wrong issuer, audience, `token_use`, signature or is expired
- **THEN** the handler rejects it with no AWS call

#### Scenario: action needs the actions group
- **WHEN** a caller without the actions group claim posts a kill
- **THEN** the handler refuses it and does not assume the actions role

#### Scenario: a revoked user cannot act with a valid token
- **WHEN** a user disabled or removed from the actions group posts a kill with an unexpired token
- **THEN** the handler refuses it and does not assume the actions role

#### Scenario: another app client does not break the page
- **WHEN** the pool also holds a CLI app client
- **THEN** `/config.json` still names the dashboard client and only its tokens are accepted

#### Scenario: action is audited
- **WHEN** an authorised user kills a sandbox
- **THEN** one audit row records subject, action, resource and time

#### Scenario: audit failure closes the action
- **WHEN** the audit rows cannot be written
- **THEN** the action does not run and the caller gets a 503 `audit_unavailable`

#### Scenario: the application cannot rewrite the audit log
- **WHEN** the function role is inspected
- **THEN** it allows only `dynamodb:PutItem` on the audit table and denies `UpdateItem`, `DeleteItem` and `BatchWriteItem`

### Requirement: The stack defends the browser surface
The Function URL SHALL NOT enable CORS (the SPA is served from its own origin), `Origin` SHALL be checked on every `POST`, the token SHALL travel in the `Authorization` header and not in a cookie, and every response SHALL send `Content-Security-Policy`, `X-Frame-Options: DENY` with `frame-ancestors 'none'` and `Referrer-Policy: no-referrer`.

#### Scenario: no CORS headers
- **WHEN** any response is served
- **THEN** it carries no `Access-Control-*` header

#### Scenario: foreign origin rejected
- **WHEN** a `POST` arrives with a foreign `Origin`
- **THEN** it is rejected

#### Scenario: security headers present
- **WHEN** the SPA shell or any API response is served
- **THEN** it carries the CSP, framing and referrer headers

### Requirement: A user theme enters through a stack or SSM parameter
The stack SHALL accept the theme JSON as the `ThemeJson` stack parameter (stored by the stack in an SSM parameter) or as the name of a user-managed SSM parameter (`ThemeParameterName`, taking precedence), and an optional SVG logo parameter. The function SHALL pass them through `parse_theme` and `parse_logo`, re-read them at a bounded interval, serve them in `/theme.css` and `/theme/logo`, fall back to the built-in theme when they are invalid or unreadable, and be allowed to read only those parameters.

#### Scenario: valid theme served
- **WHEN** the theme parameter holds a valid theme
- **THEN** `/theme.css` carries its tokens and `/api/v1/meta` reports its name

#### Scenario: invalid theme ignored
- **WHEN** the theme parameter holds an unknown key or a value that is not a colour, length or font list
- **THEN** the built-in theme is served and only the reason is logged

### Requirement: Cost Explorer access is a stack parameter
Billed cost SHALL be off by default behind a stack parameter, and `ce:GetCostAndUsage` SHALL be granted only when it is on. When on, the handler SHALL accept only month-aligned windows within `dashboardStackCostLookbackMonths` and supported regions, and SHALL spend at most `dashboardStackCostMissesPerDay` billed queries per UTC day for the whole stack, counted in the cache table and only on a cache miss; if the budget cannot be checked it SHALL not bill. No guide or threat model SHALL claim that the cache alone bounds the spend.

#### Scenario: default role lacks Cost Explorer
- **WHEN** the stack is deployed with default parameters
- **THEN** no role holds `ce:GetCostAndUsage`

#### Scenario: arbitrary windows cannot run up the bill
- **WHEN** an authenticated user sends billed-cost requests with arbitrary `start`, `end` or `region`
- **THEN** windows and regions outside the bounds get a 400, and once the daily budget is used up no further window reaches Cost Explorer

### Requirement: Token lifetime limits are pinned and described truthfully
The access token validity, the theme parameter byte cap and the SSM Advanced cap SHALL be named values in `limits.json`, asserted against the template, and referenced from the descriptions and docs. The docs and threat model SHALL state that an issued access token cannot be revoked (the handler validates it offline), that it stays valid until it expires, and that actions are cut immediately by the live check.

#### Scenario: template equals limits.json
- **WHEN** `scripts/tests/test_dashboard_template.py` runs
- **THEN** `MaxLength` of `ThemeJson` and `AccessTokenValidity` equal their `limits.json` values

### Requirement: The page does not loop on a persistent 401
After a 401 the page SHALL keep the refresh token and renew only the access token, SHALL redirect to the Hosted UI at most once per rejection streak, and SHALL show an error state when a 5xx answers `/config.json`, treating only a 404 as the local server.

#### Scenario: second rejection shows an error
- **WHEN** the server rejects the token again after a renewal
- **THEN** the page shows the error and does not redirect again

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

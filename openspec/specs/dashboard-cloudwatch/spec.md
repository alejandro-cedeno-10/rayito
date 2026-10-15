# dashboard-cloudwatch Specification

## Purpose
TBD - created by archiving change dashboard. Update Purpose after archive.

## Requirements

### Requirement: A generated CloudWatch dashboard is optional
`rayito dashboard cloudwatch` SHALL generate the dashboard JSON (Logs Insights queries over `/rayito/*` and OTLP metrics when enabled) without creating resources unless the user deploys it.

#### Scenario: generation makes no AWS write
- **WHEN** the command runs without a deploy option
- **THEN** it prints the JSON and makes no write call

### Requirement: Quota alerts and multi-account views are opt-in
Quota alerts to SNS and cross-account read roles SHALL be separate opt-in features, off by default, with their own cost declaration.

#### Scenario: off by default
- **WHEN** no alert or cross-account option is set
- **THEN** no SNS or cross-account call is made

### Requirement: The printed and the deployed CloudWatch dashboard are the same JSON
The `dashboard-cloudwatch` stack SHALL render its `DashboardBody` from the generator `rayito dashboard cloudwatch` prints, with Logs Insights widgets over the `/rayito/*` log groups always present and the OTLP metric charts and the quota-usage chart only when `IncludeOtlp` or `IncludeQuota` is `true`. The prefix and any explicit log group names SHALL be validated against a closed character list before they enter a query.

#### Scenario: no drift between the template and the generator
- **WHEN** the stack template's `DashboardBody` is evaluated for each combination of `IncludeOtlp` and `IncludeQuota`
- **THEN** it equals the JSON the generator builds for the same options

#### Scenario: a prefix that could break out of the query is refused
- **WHEN** `rayito dashboard cloudwatch --log-group-prefix "/a'b"` runs
- **THEN** it exits 2 and builds no query

### Requirement: Multi-account view uses read-only assumed roles
`rayito dashboard --account NAME=<role ARN>` SHALL add read-only accounts to the local panel through `sts:AssumeRole`, with an optional `--external-id`. The `dashboard-read-role` stack SHALL create, in the target account, a role that allows only `lambda:ListMicrovms`, `lambda:ListMicrovmImages`, `lambda:ListMicrovmImageVersions`, `lambda:GetMicrovmImageVersion` and `servicequotas:ListServiceQuotas` and trusts only the principal it is given (never an account root). Additional accounts SHALL never run actions, billed cost, logs or events, the API SHALL expose only their labels, and building the session SHALL make no STS call.

#### Scenario: an additional account cannot act
- **WHEN** a kill request carries `?account=<label>` of an additional account
- **THEN** the API answers `actions_disabled` and no write call reaches AWS

#### Scenario: no STS call at startup
- **WHEN** `rayito dashboard --account staging=<role ARN>` starts
- **THEN** no AWS call is made until the page asks for that account

#### Scenario: an unknown account is refused
- **WHEN** a request names an account that was not configured
- **THEN** the API answers `invalid_argument` before any AWS call

### Requirement: Quota alerts notify once per threshold crossing
The `quota-alerts` stack SHALL create a CloudFormation Lambda and an EventBridge schedule that measure the memory allocated to MicroVMs against the regional quota with the same `Quotas` service as the panel, publish the usage fraction as one custom metric, and publish to SNS only when the level rises (ok to warning, warning to critical). The level SHALL be stored after a successful publish, so a failed publish is retried by the next run. The message SHALL carry the region, the figures and the sandbox count, and no sandbox, image or account identifiers. The warning and critical percentages and the schedule SHALL be stack parameters whose defaults equal `limits.json`.

#### Scenario: one alert per crossing
- **WHEN** the usage stays above the warning threshold for several runs
- **THEN** one message is published, and a new one only if the usage reaches the critical level or drops below and crosses again

#### Scenario: a failed publish is not lost
- **WHEN** SNS rejects the publish
- **THEN** the run fails and the stored level is unchanged

#### Scenario: least privilege
- **WHEN** the function role is read as data
- **THEN** its only writes are `sns:Publish` on its topic, `GetItem` and `PutItem` on its state table, and `cloudwatch:PutMetricData` restricted by `cloudwatch:namespace`

### Requirement: The CloudWatch dashboard name is configurable and unique by default
The `dashboard-cloudwatch` stack SHALL name the dashboard after the stack when `DashboardName` is empty (the default) instead of a fixed name, SHALL use the given name when set, and a stack deployed with the previous default SHALL keep its name on redeploy.

#### Scenario: default name is the stack name
- **WHEN** the stack is deployed without `DashboardName`
- **THEN** the dashboard is named like the stack

### Requirement: The OTLP charts can be tested by following a documented recipe
The docs SHALL list the steps that make the OTLP charts show data (deploy the export policy, create a sandbox with `telemetry=`, wait two intervals, check `get_telemetry_status()`, deploy the dashboard with `IncludeOtlp=true`) with their cost, and how to turn it off.

#### Scenario: docs gates pass
- **WHEN** `mkdocs build --strict` runs
- **THEN** it passes

## ADDED Requirements

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

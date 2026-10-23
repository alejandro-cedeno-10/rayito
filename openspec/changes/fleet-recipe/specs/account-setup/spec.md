## ADDED Requirements

### Requirement: setup publishes the caps base image
The setup plan SHALL have four steps in the order `bucket`, `iamStack`, `baseImage`, `capsImage`. `capsImage` SHALL publish, reuse or wait for `rayito-base-caps` from the same downloaded release artifact as `baseImage`, with `additionalOsCapabilities=["ALL"]`, and with no second download, only when `caps_image`/`capsImage` is true (`--caps-image`). By default the step SHALL be `skip` and SHALL make no call, because it costs money. The plan and `--dry-run` SHALL show the step and its storage cost.

#### Scenario: fresh account
- **WHEN** `rayito setup --yes --caps-image` runs in an account with no `rayito-base-caps`
- **THEN** both `rayito-base` and `rayito-base-caps` are published and the release zip is downloaded once

#### Scenario: off by default
- **WHEN** `rayito setup --yes` runs without `--caps-image`
- **THEN** only `rayito-base` is published and the `capsImage` step is `skip`

#### Scenario: already published
- **WHEN** a launchable `rayito-base-caps` version exists
- **THEN** the `capsImage` step is `reuse`

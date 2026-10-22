## ADDED Requirements

### Requirement: setup publishes the caps base image
The setup plan SHALL have four steps in the order `bucket`, `iamStack`, `baseImage`, `capsImage`. `capsImage` SHALL publish, reuse or wait for `rayito-base-caps` from the same downloaded release artifact as `baseImage`, with `additionalOsCapabilities=["ALL"]`, and with no second download. With `caps_image=False` (`capsImage: false`, `--no-caps-image`), the step SHALL be `skip` and SHALL make no call. The plan and `--dry-run` SHALL show the step and its storage cost.

#### Scenario: fresh account
- **WHEN** `rayito setup --yes` runs in an account with no `rayito-base-caps`
- **THEN** both `rayito-base` and `rayito-base-caps` are published and the release zip is downloaded once

#### Scenario: already published
- **WHEN** a launchable `rayito-base-caps` version exists
- **THEN** the `capsImage` step is `reuse`

## ADDED Requirements

### Requirement: rayito image publish can scope the artifact key to one run
`rayito image publish` SHALL accept an optional `--artifact-run-id RUN_ID`. Without it the artifact key SHALL stay `rayito/images/rayd-<first 12 hex of sha256>.zip`. With it the key SHALL be `rayito/images/runs/<RUN_ID>/rayd-<first 12 hex of sha256>.zip`. `RUN_ID` SHALL match `[A-Za-z0-9][A-Za-z0-9-]{0,63}`; any other value SHALL be a usage error (exit 2) before any AWS call. The publish summary SHALL include `artifactUploaded`, `true` when this invocation uploaded the object and `false` when the key already existed.

#### Scenario: default key unchanged
- **WHEN** `rayito image publish` runs without `--artifact-run-id`
- **THEN** the zip is uploaded to `rayito/images/rayd-<sha>.zip` unless the key exists, exactly as before

#### Scenario: run-scoped key
- **WHEN** `rayito image publish --artifact-run-id acc-1` runs
- **THEN** the zip is uploaded to `rayito/images/runs/acc-1/rayd-<sha>.zip` and the image's `codeArtifact.uri` points there

#### Scenario: invalid run id
- **WHEN** `--artifact-run-id` contains `/`, starts with `-`, is empty or longer than 64 characters
- **THEN** the command exits 2 before any AWS call

#### Scenario: summary says whether the run uploaded the artifact
- **WHEN** `rayito --json image publish` finds the key already present
- **THEN** the summary has `artifactUploaded: false`, and `true` when it uploaded the object

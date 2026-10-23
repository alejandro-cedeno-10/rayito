## RENAMED Requirements

- FROM: `### Requirement: The IAM stack is created, updated only for its bucket, or waited for`
- TO: `### Requirement: The IAM stack is created, updated for its bucket or its template, or waited for`

## MODIFIED Requirements

### Requirement: The IAM stack is created, updated for its bucket or its template, or waited for
When `rayito-m0-iam` does not exist, `apply` SHALL create it through `OptionalStacks.deploy` with the packaged `infra/iam.yaml`, `Capabilities=["CAPABILITY_NAMED_IAM"]`, `ArtifactBucket=<bucket>` (the reused bucket or the `BucketName` output of `rayito-m0-artifacts`) and `TemplateFingerprint=<sha256 of the packaged template>`, and no other parameter, and SHALL wait for it, always after the bucket step. When it exists in a usable state with a different `ArtifactBucket`, or without the `TemplateFingerprint` of the running SDK (a stack deployed by an earlier SDK or by hand), `apply` SHALL update it with the packaged template, sending only `ArtifactBucket` and `TemplateFingerprint` and keeping every other parameter with `UsePreviousValue`; the plan step SHALL carry `previousBucket` only when the bucket changes. When it is in progress (other than `DELETE_IN_PROGRESS`), `apply` SHALL wait for it and SHALL NOT call `CreateStack` or `UpdateStack`, unless the waited stack has another bucket or another template, which it SHALL then update. A stack in `ROLLBACK_COMPLETE`, `DELETE_IN_PROGRESS` or a `*_FAILED` state SHALL raise `stack_blocked`, and a usable stack without `BuildRoleArn` SHALL raise `stack_incomplete`. Both SDKs SHALL compute the same fingerprint. The `iam` component SHALL NOT appear in the `rayito stack` catalog.

#### Scenario: create on a fresh account
- **WHEN** `rayito setup --yes` runs on a fake account with a CDK bootstrap bucket and no stack
- **THEN** the fake provisioner records exactly one `create` of `rayito-m0-iam`, with `CAPABILITY_NAMED_IAM` and the parameters `ArtifactBucket` and `TemplateFingerprint`, followed by a `wait` for `deployed`, and no `update`

#### Scenario: a changed bucket updates only that parameter
- **WHEN** `rayito setup --yes --bucket amzn-s3-demo-bucket1` runs and the stack's `ArtifactBucket` is `amzn-s3-demo-bucket`
- **THEN** the fake provisioner records one `update` whose values are `ArtifactBucket=amzn-s3-demo-bucket1` plus the template fingerprint that every deploy carries, and whose kept-previous list holds the six other template parameters present on the stack

#### Scenario: a stack of an earlier SDK is updated with the same bucket
- **WHEN** the plan observes a usable stack whose `ArtifactBucket` is the plan's bucket and that has no `TemplateFingerprint` parameter
- **THEN** the step is `update` with `previousBucket` null, the plan line reads `actualizar a la plantilla infra/iam.yaml de este SDK`, and `apply` sends one update with the same bucket

#### Scenario: both SDKs fingerprint the same template
- **WHEN** each SDK computes the fingerprint of its packaged `iam` template
- **THEN** it equals the sha256 of the repository's `infra/iam.yaml`

#### Scenario: the catalog is unchanged
- **WHEN** `rayito stack list` runs
- **THEN** no component named `iam` or `artifact-bucket` is listed

## ADDED Requirements

### Requirement: The JSON steps of a published image carry its version
After `apply`, every `baseImage` or `capsImage` step of the `--json` document whose action is `publish` SHALL carry in `imageVersion` the version that step published, the same value as `result.imageVersion` or `result.capsImageVersion`. In a dry run, and when the preparation fails before the result, those steps SHALL keep `imageVersion: null`.

#### Scenario: a fresh account published both images
- **WHEN** `rayito --json setup --caps-image --yes` publishes `rayito-base` and `rayito-base-caps`
- **THEN** the `baseImage` and `capsImage` steps have `imageVersion` equal to `result.imageVersion` and `result.capsImageVersion`

#### Scenario: a dry run has nothing published
- **WHEN** `rayito --json setup --caps-image --dry-run` runs on a fresh account
- **THEN** both image steps have `imageVersion: null`

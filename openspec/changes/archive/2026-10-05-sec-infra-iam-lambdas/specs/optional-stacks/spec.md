## ADDED Requirements

### Requirement: stack artifacts are never trusted on existence alone
`OptionalStacks.deploy` SHALL upload a component's Lambda artifact to `rayito/stacks/<component>/<sha256>.zip` of `artifact_bucket`, never to the bucket root. `StackProvisioner.put_artifact`/`putArtifact` SHALL send `ExpectedBucketOwner` set to the caller's account (`sts:GetCallerIdentity`) on every S3 call, SHALL read an existing object and compare its sha256 with the artifact's before reusing it, and SHALL otherwise upload with `ChecksumSHA256`; an existing object with different content SHALL be overwritten and reported through the SDK logger without naming the bucket or the key.

#### Scenario: a planted object is overwritten
- **WHEN** the artifact key already holds different bytes
- **THEN** `put_artifact` uploads the SDK's artifact with `ExpectedBucketOwner` and `ChecksumSHA256`

#### Scenario: an identical object is reused
- **WHEN** the artifact key already holds exactly the artifact's bytes
- **THEN** no `PutObject` is sent

#### Scenario: a bucket of another account is refused
- **WHEN** `GetObject` with `ExpectedBucketOwner` answers `AccessDenied`
- **THEN** `put_artifact` raises `StackException`/`StackError` and uploads nothing

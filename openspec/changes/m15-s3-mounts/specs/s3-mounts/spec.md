## ADDED Requirements

### Requirement: an S3Mounts section naming a bucket outside the image allowlist is rejected before any mount is touched
The agent's `s3_mounts` feature slot SHALL validate every `S3Mount` in a present `S3MountsConfig` against the image's `RAYITO_ALLOWED_MOUNT_BUCKETS` allowlist before attaching FUSE or spawning `mount-s3` for any of them. An empty or unset allowlist SHALL reject every bucket. A request with one invalid mount among several valid ones SHALL leave every currently-mounted entry untouched and mount none of the new ones, reporting `SECTION_CODE_INVALID` with `error_class = "not_allowed"`.

#### Scenario: an empty allowlist rejects every bucket
- **WHEN** `S3MountsConfig` names a bucket and the image's `RAYITO_ALLOWED_MOUNT_BUCKETS` is empty or unset
- **THEN** the section is rejected with `SECTION_CODE_INVALID`/`not_allowed` and neither `FuseDevice::attach` nor `FuseDaemon::spawn` is called

#### Scenario: one invalid mount blocks the whole section, not just itself
- **WHEN** a request contains one mount whose bucket is on the allowlist and one whose bucket is not
- **THEN** the section is rejected as a whole (`SECTION_CODE_INVALID`) and no mount in the request is attached

### Requirement: re-applying an unchanged S3Mounts section is a no-op
Given a mount already in the `Mounted` state with an identical `S3Mount` spec, a later `Configure` call carrying the same section SHALL NOT call `FuseDevice::attach` or `FuseDaemon::spawn` again for that mount, and SHALL report `SECTION_CODE_APPLIED`.

#### Scenario: identical re-apply mounts nothing new
- **WHEN** `Configure` is called twice with byte-identical `S3MountsConfig`
- **THEN** `FuseDevice::attach` and `FuseDaemon::spawn` are each called exactly once across both calls

### Requirement: a mount removed from a later section is unmounted
A mount present in the agent's current state but absent from a newly-applied `S3MountsConfig` SHALL be detached (`FuseDevice::detach`) and its daemon killed (`FuseDaemon::kill`) before the call returns, and SHALL NOT appear in a subsequent `ConfigureStatus` response.

#### Scenario: dropping a mount from the config unmounts it
- **WHEN** a mount is present in one `Configure` call and absent from the next
- **THEN** `FuseDevice::detach` and `FuseDaemon::kill` are called for it, and it is absent from `ConfigureStatus.s3_mounts.mounts`

### Requirement: the s3-mounts LifecycleParticipant never claims a /suspend share
The `s3_mounts` feature's `LifecycleParticipant::demand().max` SHALL be zero: the bounded per-filesystem `syncfs` already covers a mounted FUSE filesystem, so this feature never extends `/suspend`'s total wait.

#### Scenario: the participant's demand is zero
- **WHEN** `S3MountsFeature::participant()` is asked for its `LifecycleParticipant`
- **THEN** `demand().max` is `Duration::ZERO`

### Requirement: mount-s3 is launched with no credential in its argv or environment
`adapters::mount_s3`'s spawn of the `mount-s3` daemon SHALL clear the inherited environment and set only `AWS_REGION` and `PATH`, and SHALL NOT pass any AWS credential value as a command-line argument.

#### Scenario: the daemon's argv names only the mount parameters
- **WHEN** `TokioMountS3Daemon::spawn` builds the `mount-s3` command line
- **THEN** its arguments are drawn only from `--foreground`, the bucket, the FUSE fd path, `--prefix` and the read/write flags — never a credential

### Requirement: deploying the s3-mounts OptionalStack component requires BucketName and is off by default
`OptionalStacks.deploy("s3-mounts")` (and `rayito stack deploy s3-mounts`) SHALL require the `BucketName` parameter (raising `InvalidArgumentException`/`InvalidArgumentError` before any `StackProvisioner` call when it is missing) and SHALL NOT be invoked by any `Sandbox.create()`, listing, or getter path.

#### Scenario: deploying without BucketName fails before touching the provisioner
- **WHEN** `OptionalStacks().deploy("s3-mounts")` is called with no `BucketName` parameter
- **THEN** `InvalidArgumentException`/`InvalidArgumentError` is raised and the `StackProvisioner` records zero calls

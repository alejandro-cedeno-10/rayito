# s3-mounts Specification

## Purpose
TBD - created by archiving change m15-s3-mounts. Update Purpose after archive.

## Requirements

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

### Requirement: the mountpoint is never resolved through a symlink
`rayd` SHALL open every component of a mount path without following symlinks before `mount(2)`, SHALL reject a path any of whose components is a symlink or not a directory with `invalid_path`, and SHALL perform `mount(2)`/`umount2(2)` only through `/proc/self/fd` descriptors of the directories it opened, on the first attach and on every relaunch.

#### Scenario: a symlinked mount directory is rejected
- **WHEN** the mount directory (or one of its ancestors) is a symlink to a system directory at attach time
- **THEN** the attach fails with `invalid_path` and nothing is created or mounted inside the symlink's target

### Requirement: the agent only claims s3_mounts with CAP_SYS_ADMIN
`Health.features.s3_mounts` (and the slot's `supported()`) SHALL be `true` only when rayd's effective capability set contains `CAP_SYS_ADMIN` and the `mount-s3` binary, `/dev/fuse` and the `rayito-mount` user are present; `root_egress` SHALL list `S3` only while the slot is supported.

#### Scenario: an image without CAP_SYS_ADMIN reports no support
- **WHEN** the agent boots without `CAP_SYS_ADMIN` even though `mount-s3` is installed
- **THEN** `Health.features.s3_mounts` is `false` and `root_egress` does not contain `S3`

### Requirement: create() returns only once every requested mount is mounted
After a `Configure` call whose `s3_mounts` result is `SECTION_CODE_PENDING`, `Sandbox.create()` SHALL poll `ConfigureStatus` until every requested mount is `mounted`, bounded by the mount settle timeout; a mount reported `failed` SHALL raise `MountException`/`MountError` with its `last_error_class` as `code`, and one still `pending` at the deadline SHALL raise it with `code = "timeout"`, in both cases terminating the sandbox unless `keep_on_failure`.

#### Scenario: a failed mount fails create()
- **WHEN** `ConfigureStatus` reports a requested mount as `failed` with `error_class = "iam_denied"`
- **THEN** `create()` raises `MountException` with `code == "iam_denied"` instead of returning a sandbox

#### Scenario: a mount that settles is readable when create() returns
- **WHEN** `ConfigureStatus` reports the mount `pending` and then `mounted`
- **THEN** `create()` returns only after the `mounted` reading

### Requirement: the s3-mounts IAM policy is scoped to the declared prefixes
The `RayitoS3MountAccess` policy SHALL scope `s3:ListBucket` with an `s3:prefix` condition and SHALL scope every object-level action to object ARNs that carry the declared prefixes, and the `s3-mounts` component SHALL declare `CAPABILITY_IAM`.

#### Scenario: object actions never cover the whole bucket unless asked
- **WHEN** the stack is deployed with `Prefixes=runs/*`
- **THEN** `GetObject`, `PutObject` and `DeleteObject` apply only to `arn:<partition>:s3:::<bucket>/runs/*`

### Requirement: internal launchers never pass rayd's environment or descriptors to a child
The mount readiness probe SHALL run an absolute `stat` binary as the guest
user with an environment holding only `PATH`, and SHALL, like `mount-s3`,
mark every descriptor above the ones the child must inherit close-on-exec
and reset every signal disposition between `fork` and `exec`, through the
same posture the user-code launchers use.

#### Scenario: the probe's environment is only PATH
- **WHEN** the agent builds the readiness probe for a mount path
- **THEN** its program is `/usr/bin/stat`, its environment is cleared and
  `PATH` is the only variable set

#### Scenario: an inheritable descriptor never reaches a guest helper
- **WHEN** `rayd` holds a descriptor opened without `O_CLOEXEC` while it
  launches a guest helper
- **THEN** the helper's environment lists only `PATH` and the descriptor is
  not open in the helper

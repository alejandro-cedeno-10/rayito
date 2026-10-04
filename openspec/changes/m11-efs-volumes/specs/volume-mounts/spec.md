## ADDED Requirements

### Requirement: Measurements gate the implementation
Before any code of this change is written, the measurements Q79–Q98 of `docs/research/2026-10-efs-persistence.md` §9 SHALL be run against real AWS in a VPC owned or lent for the purpose and recorded as rows of `AWS_API_NOTES.md` §16, and `AWS_API_NOTES.md` SHALL gain a §19 with the EFS API contract (`CreateAccessPoint`, `DescribeAccessPoints`, `DeleteAccessPoint`, `DescribeMountTargets`, `TagResource`) copied from the `efs` 2015-02-01 service model and the mount-helper options used. If any stop question (Q79, Q80, Q81, Q85, Q86, Q89, Q91) fails, the change SHALL stop without implementation and the report and `docs/site/docs/e2b-parity.md` row 26 SHALL be amended with the measured reason. No AWS parameter absent from `AWS_API_NOTES.md` or `docs/aws-api/` SHALL be used.

#### Scenario: a stop question fails
- **WHEN** Q79 shows that `/proc/filesystems` of the `rayito-base-caps` guest lists no `nfs4`
- **THEN** no task after group 1 is started, the Q79 row records the output, and `e2b-parity.md` row 26 is re-labelled "imposible en la plataforma" citing Q79

#### Scenario: all stop questions pass
- **WHEN** Q79, Q80, Q81, Q85, Q86, Q89 and Q91 are recorded as successful
- **THEN** §19 exists in `AWS_API_NOTES.md` and implementation starts with the contract group

### Requirement: rayd validates volume specs in the domain
`rayd-core` SHALL provide a `volume` module that validates each requested volume (`file_system_id` matching `fs-[0-9a-f]{8,40}`, `access_point_id` matching `fsap-[0-9a-f]{8,40}`, optional IPv4 mount-target address, `read_only`, `mount_path`) and the plan as a whole: at most 4 volumes; each `mount_path` absolute, canonical, under `/mnt/` or `/home/user/`, never equal to `/home/user`, never under `/proc`, `/sys`, `/dev`, `/run`, `/etc`, `/usr`, `/tmp` or `/root` nor matched by the filesystem `DenyList`; no two paths equal, overlapping or nested; and the file system present in the image allowlist `RAYITO_EFS_ALLOWED_FILE_SYSTEMS`. A violation SHALL be reported as gRPC `INVALID_ARGUMENT` (or `PERMISSION_DENIED` for the allowlist) before any mount is attempted, with fixed messages that never contain ids, paths or IP addresses.

#### Scenario: invalid plans are rejected before mounting
- **WHEN** the domain test validates, separately, five volumes; `/mnt/a` and `/mnt/a/b`; `/etc/x`; `/home/user`; and a file system absent from the allowlist
- **THEN** each is rejected with its `VolumeError` variant, the fake `VolumeMounter` records no `mount`, and no error message contains the id or the path

### Requirement: rayd mounts volumes through the mount helper as root without blocking hooks
Only when `rayd` detects `CAP_SYS_ADMIN`, `nfs4` in `/proc/filesystems` and the EFS mount helper SHALL `Health.volumes_supported` be true. `VolumeService.Mount` (unary, `x-access-token` required) SHALL apply a validated plan by running the mount helper as root with a scratch environment and the options `tls`, `iam`, `accesspoint=<id>`, `mounttargetip=<ip>`, `noresvport` and, for read-only volumes, `ro`, retrying with backoff 0.5, 1, 2, 4 and 8 s within a 45 s budget, and SHALL publish each volume in `Health.volumes` as `MOUNTING`, `MOUNTED`, `DEGRADED` or `FAILED` with a fixed error class (`network`, `iam_denied`, `not_found`, `tls`, `helper_missing`, `timeout`). `rayd` SHALL supervise the mount watchdog as a child process. No mount SHALL happen before the `/run` hook, so that no build snapshot contains an NFS client session. Where `volumes_supported` is false, `Mount` SHALL answer `UNIMPLEMENTED`.

#### Scenario: mount on the caps image
- **WHEN** the e2e test calls `Mount` on a `rayito-base-caps` sandbox launched with the EFS connector and a role allowed on the access point
- **THEN** `Health.volumes` reports the path as `MOUNTED` within 60 s and a file created by uid 1000 in it is owned by `1000:1000`

#### Scenario: default image
- **WHEN** `Mount` is called on a `rayito-base` sandbox
- **THEN** it answers `UNIMPLEMENTED` and `Health.volumes_supported` is false

### Requirement: Suspend and resume never block on a network mount
The `/suspend` hook SHALL NOT unmount volumes and SHALL replace the unbounded `sync(2)` with `syncfs` on each local file system plus a `syncfs` per volume on a disposable thread bounded to 5 s, and SHALL always answer 200. The `/resume` hook SHALL probe each mounted volume with a `stat` in a child process bounded to 5 s; on a stale or hung result it SHALL restart the mount tunnel and, if still unhealthy, lazily unmount and remount in the background, latch `volume_state_lost` in `Health` until the next `/resume`, and answer 200 without waiting for the remount. `/terminate` SHALL lazily unmount every volume within 2 s.

#### Scenario: unreachable mount target during suspend
- **WHEN** the security group of the mount targets is revoked while a volume is mounted and `pause()` is called
- **THEN** `/suspend` answers 200 within its timeout, the VM reaches `SUSPENDED`, and after restoring the rule and resuming `Health.volumes` reports the path as `MOUNTED` or `DEGRADED` with `volume_state_lost` set

#### Scenario: short pause
- **WHEN** a sandbox with a mounted volume is paused for 90 s and resumed
- **THEN** a file written before the pause is readable at the same path after the resume

### Requirement: The sandbox user cannot reach NFS directly
On images with `CAP_NET_ADMIN`, the egress routes of ADR-012 SHALL, for uid 1000–65535, install a `blackhole` route for every mount-target address of the applied plan and a `prohibit` rule for the local port or ports of the mount helper's TLS proxy, installed and removed atomically with the plan; `rayd` SHALL NOT expose any credential or mount option to uid 1000.

#### Scenario: direct connection attempts
- **WHEN** a process running as uid 1000 connects to the mount-target address on TCP 2049 and to the local proxy port
- **THEN** both connections fail immediately and the mounted volume keeps working for the same process through the file system

### Requirement: Infrastructure templates for volumes
The repository SHALL provide `infra/efs-volumes.yaml` creating an encrypted EFS file system (General Purpose, Elastic throughput, lifecycle to Infrequent Access after 30 days) whose file-system policy denies access without TLS, without an access point and not via a mount target; one mount target per subnet; a mount-target security group admitting TCP 2049 only from the connector security group; a connector security group whose only egress is TCP 2049 to the mount targets; an `AWS::Lambda::NetworkConnector` with `VpcEgressConfiguration` and its operator role; and outputs for the file-system id and ARN, the connector ARN and the mount-target addresses. `infra/iam.yaml` SHALL gain parameters that grant the execution role `elasticfilesystem:ClientMount` (and `ClientWrite` only when writable) conditioned on a list of access-point ARNs and `aws:SecureTransport`, never `ClientRootAccess`, and the caller the access-point management actions and `lambda:PassNetworkConnector` on the EFS connector. Both templates SHALL pass `make infra-lint`.

#### Scenario: lint
- **WHEN** `make infra-lint` runs
- **THEN** `validate-template` and `cfn-lint` report no error for `efs-volumes.yaml` and `iam.yaml`

### Requirement: Real-AWS acceptance of volumes
`clients/python/tests/e2e/test_m11_volumes.py` SHALL be skipped unless `RAYITO_E2E=1`, `RAYITO_TEMPLATE_CAPS`, `RAYITO_EXECUTION_ROLE_ARN`, `RAYITO_EFS_FILE_SYSTEM_ID` and `RAYITO_EFS_CONNECTOR_ARN` are set, SHALL run scenarios 1–8 of the report §10 (mount and ownership, concurrent sandboxes, pause/resume, persistence across `kill()`, IAM-enforced read-only, fail-closed paths, E2B shim, teardown) and SHALL leave zero MicroVMs and zero access points tagged by the run. `clients/typescript/tests/e2e/m11.e2e.test.ts` SHALL run scenarios 1, 3 and 7.

#### Scenario: shared volume between two sandboxes
- **WHEN** sandbox A writes a 50 MB file to `/mnt/v` and sandbox B, mounting the same volume at the same time, reads it
- **THEN** both compute the same sha256 and, after both are killed, a third sandbox reads the same file

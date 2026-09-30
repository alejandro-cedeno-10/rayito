## Context

Full analysis, sources and the labels VERIFIED / ASSUMED / TO MEASURE live
in `docs/research/2026-10-efs-persistence.md` (the "report"). This design
only records the decisions. Every AWS parameter named below is either in
the `efs` 2015-02-01 service model (botocore 1.43.105), in the Lambda
MicroVMs service model, or a mount-helper option; none of them is used in
code until task group 1 has copied it into `AWS_API_NOTES.md` §19.

## Goals / Non-Goals

Goals: E2B-shaped volumes that are live, shared and persistent, mounted by
`rayd` as root through a customer VPC connector, isolated per tenant by
IAM and per volume by an EFS access point, encrypted in transit, invisible
to uid 1000 as credentials, and honest about suspend/resume.

Non-goals: volumes on `rayito-base` (no `CAP_SYS_ADMIN`); FUSE /
Mountpoint for S3 (not POSIX, `fuse` unmeasured); S3 Files (same
dependencies as EFS, a later variant of the same port); `Volume` file
operations without a sandbox (no data plane outside a VM, no control-plane
service); cross-region or cross-account file systems; changing the 8 h
ceiling.

## Decisions

- **D1 Measurements gate everything.** Q79–Q98 (report §9) run first.
  Stop questions: Q79 (NFSv4.1 in the guest kernel), Q80 (`mount` inside
  the app container), Q81 (own VPC connector reaches TCP 2049), Q85
  (`amazon-efs-utils` installs on `al2023-minimal` ARM64), Q86 (`mount -t
  efs -o tls,iam,accesspoint` works without `systemd`), Q89 (mount usable
  after resume), Q91 (`/suspend` answers 200 in time with an unreachable
  mount target). If one fails, the change stops, the report is amended
  and `e2b-parity.md` row 26 is re-labelled with the measured reason.
- **D2 Option A.** EFS Regional, General Purpose, Elastic throughput; one
  access point per volume with `RootDirectory.Path=/rayito-volumes/<name>`,
  `CreationInfo` 1000:1000 `0750`, `PosixUser` 1000:1000, tag
  `rayito:volume=<name>`. Option C (S3 checkpoint as `Volume`) is rejected
  because it is not a live shared volume.
- **D3 `rayd` mounts, the platform does not.** Lambda MicroVMs has no
  file-system configuration; only `rayito-base-caps` can mount. `rayd`
  runs `mount -t efs -o tls,iam,accesspoint=<fsap>,mounttargetip=<ip>,noresvport[,ro]`
  as root with a scratch environment and supervises the mount watchdog
  itself (no `systemd`; `rayd` is PID 1).
- **D4 Mount by RPC after readiness, not in the payload.** The suspended
  pool (ADR-008) launches VMs before the volume is known, so
  `VolumeService.Mount` is the single path for `create()` and `take()`.
  The VPC connector must be in `run-microvm` (immutable, §2), so a pool
  with volumes is a pool launched with that connector.
- **D5 Mount target by IP.** The SDK resolves `mounttargetip` with
  `DescribeMountTargets` on the caller's credentials; the guest never
  depends on resolving the VPC-private file-system name (guest resolvers
  are local, Q66; Q83 measures whether they forward).
- **D6 Domain rules in `rayd-core::volume`.** At most 4 mounts; absolute
  canonical paths under `/mnt/` or `/home/user/` (never `/home/user`
  itself, system trees or the `DenyList`); no overlap or nesting; ids
  validated with the `efs` model patterns; the file system must be in the
  image allowlist `RAYITO_EFS_ALLOWED_FILE_SYSTEMS`; errors never carry
  ids, paths or IPs.
- **D7 Port `VolumeMounter`** with `support`, `mount`, `unmount(mode)` and
  `probe(budget)`; the probe runs `stat` in a child process because a
  thread blocked on a `hard` NFS mount cannot be killed.
- **D8 Hooks never block on NFS.** `/run`: 200 immediately, mount in
  background with backoff 0.5→8 s and a 45 s budget. `/suspend`: no
  unmount (user processes hold files and `cwd`); `sync(2)` is replaced by
  `syncfs` on each local file system plus a 5 s bounded `syncfs` per
  volume on a disposable thread; always 200. `/resume`: 5 s probe; on
  `Stale`/`Hung` restart the tunnel, then lazy unmount and remount, and
  latch `volume_state_lost` in `Health` until the next `/resume`; the 200
  does not wait for the remount. `/terminate`: lazy unmount, 2 s.
- **D9 Egress.** Under caps the egress routes add, for uid 1000–65535, a
  `blackhole` for every mount-target IP of the plan and a `prohibit` rule
  on the local `efs-proxy` port(s) using the DNS-guard mechanism of M10,
  because that port is an already-authenticated NFS tunnel.
- **D10 Security model.** File-system policy denies non-TLS
  (`aws:SecureTransport`), mounts without an access point
  (`elasticfilesystem:AccessPointArn` null) and access not via a mount
  target; the execution role, one per tenant, gets `ClientMount` (and
  `ClientWrite` only for writable volumes) conditioned on the tenant's
  access-point ARNs; `ClientRootAccess` is never granted. Read-only is
  enforced by IAM, not by the `ro` option alone. Residuals go to
  `SECURITY.md` T18.
- **D11 SDK surface.** `EfsVolume` value type; `VolumeStore` (management on
  the caller's boto3 / AWS SDK v3 credentials: create, get, list,
  destroy, purge); `Sandbox.create(volumes={path: EfsVolume})` requires
  `execution_role_arn` and at least one egress connector, waits for
  `MOUNTED` with `volume_timeout` (60 s), and on failure terminates the VM
  (unless `keep_on_failure`) and raises `VolumeMountException(code)`; an
  agent without `volumes_supported` raises `UnimplementedError`.
- **D12 E2B shim.** `Volume.create/connect/list/get_info/destroy` over
  `VolumeStore`, configured by the bound `E2B(...)` client or
  `RAYITO_EFS_FILE_SYSTEM_ID`, `RAYITO_EFS_CONNECTOR_ARN`,
  `RAYITO_EFS_ROLE_ARN`; `volume_mounts` accepts a `Volume` or its name;
  `destroy` deletes the access point and documents that data stays until
  `purge`; `volume.read_file/write_file/make_dir/list/remove` stay
  `UnimplementedError`.
- **D13 Logging.** Only states, counts, durations and fixed error classes;
  never file-system ids, access-point ids, IPs or paths (as T15 with
  buckets).

## Risks / Trade-offs

- NFSv4 lease expiry during long pauses loses locks (`EIO`) and may turn
  open descriptors `ESTALE`; documented, measured in Q89/Q90.
- If Q82 shows that a VPC connector replaces `INTERNET_EGRESS`, internet
  access for volume sandboxes needs a NAT gateway in the customer VPC.
- `amazon-efs-utils` grows the image snapshot (Q85); acceptable if the
  cold-start delta stays within the Q42 resolution.
- Isolation is per execution role, not per sandbox (T18), as for S3 (T15).
- `files.write(metadata=)` cannot work on a volume (EFS has no xattrs):
  it answers `unimplemented` there.

## Migration Plan

Additive: new proto service and `Health` fields; SDKs gate on
`volumes_supported`; older agents answer `UNIMPLEMENTED` and the SDK maps
it to `UnimplementedError`. Operators opt in by deploying
`infra/efs-volumes.yaml` and republishing `rayito-base-caps`.

## Open Questions

Q79–Q98 of the report; in particular whether the platform terminates a VM
whose `/suspend` times out (Q91), how two egress connectors are routed
(Q82) and whether IMDS rotates credentials during a pause over 55 min
(Q90, also Q1).

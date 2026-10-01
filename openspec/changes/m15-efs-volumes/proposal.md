## Why

E2B's `Volume` (beta) gives several sandboxes a live-shared POSIX
directory. Rayito 0.5.x has no equivalent — only `persist=`, a
checkpoint/restore copy in S3 that never shares data between two running
sandboxes. `docs/research/2026-10-efs-persistence.md` (the M11 feasibility
study, merged into this branch from PR #59) found the design viable
*conditioned on* an AWS measurement campaign (EFS-1..EFS-20, renumbered
from the study's own Q79-Q98) that nothing in this change runs: three stop
criteria (NFSv4.1 in the guest kernel, a Rayito-owned VPC connector that
reaches a mount target, `efs-utils` with TLS+IAM+access-point and no
`systemd`) decide whether real mounting is even possible, and a real
`VolumeMounter` adapter must wait for that campaign's own OpenSpec change
or revision.

This change ships everything that does **not** depend on those
measurements: the pure domain and port (`rayd_core::volume`,
`VolumeMounter`), a real, tested `VolumeStore` CRUD over EFS access points
(credentials of the *caller*, not the execution role), the capability-gated
`Sandbox.create(volumes=)` surface that validates eagerly and always raises
`UnimplementedError` until a real mounter exists, the `efs-volumes`
`OptionalStack` component (`infra/efs-volumes.yaml`: file system, mount
targets, the NFS security groups, a dedicated egress connector), the E2B
shim's `Volume`/`AsyncVolume`, and a precise, idempotent measurement script
for the serialized AWS acceptance stage.

## What Changes

- **`rayd-core::volume`** (pure): `VolumeSpec`/`FileSystemId`/
  `AccessPointId`/`MountPath`/`MountTargetIp` (validated, no I/O),
  `VolumePlan` (dedupe, cap at 4, deterministic order), `MountState` (7
  states) with a pure transition function, a closed `VolumeError` table, and
  the `VolumeMounter` port (`support`/`mount`/`unmount`/`probe`, boxed
  futures so it stays `dyn`-safe).
- **`rayd::adapters::efs_mount::UnavailableEfsMounter`**: the only adapter
  this change ships. Always reports `MountSupport::Unsupported`; demonstrates
  the port boundary without pretending to mount anything.
  `features::efs_volumes::build()` stays `slot::Unsupported` (an
  `EfsVolumesConfig` section always answers `SECTION_CODE_UNSUPPORTED`):
  wiring a real dispatch onto an adapter that can only ever refuse would be
  dead code.
- **`proto/rayito/v1/efs_volumes.proto`**: `EfsVolumesConfig`
  (`repeated EfsVolumeMount`) / `EfsVolumesStatus` (`repeated
  EfsVolumeStatus`, `EfsVolumeState` mirroring `MountState`) — complete now
  so a future real adapter needs no wire change.
- **Python `rayito._volumes` / TypeScript `src/volumes/`**: `EfsVolume`
  (validated value), `VolumeStore`/`AsyncVolumeStore` (Python)/`VolumeStore`
  (TypeScript, always async) — real CRUD (`CreateAccessPoint`/
  `DescribeAccessPoints`/`DeleteAccessPoint`), lazy client construction (zero
  AWS calls until first use), and `require_volume_support`/
  `requireVolumeSupport`: the `volumes=` gate that validates eagerly (shape,
  mount paths, caps variant) and always raises `UnimplementedError` naming
  this change and the pending measurement campaign.
- **`infra/efs-volumes.yaml`** (`OptionalStack` component, `supported`):
  encrypted Elastic-Throughput `AWS::EFS::FileSystem`, up to 3
  `AWS::EFS::MountTarget`s, a connector security group (egress 2049 only)
  and a mount-target security group (ingress 2049 only from the connector),
  a `FileSystemPolicy` denying non-TLS/no-access-point/non-mount-target
  traffic, and a dedicated `AWS::Lambda::NetworkConnector` + operator role.
  `RetainData` (default `true`) controls whether `destroy()` also deletes
  the file system and its data.
- **E2B shim**: `Volume`/`AsyncVolume` move out of
  `e2b/_unimplemented.py` into `e2b/_volume.py` (TS: `e2b/volume.ts`), with
  real `create`/`connect`/`list`/`get_info`/`destroy` over a configured
  `VolumeStore`; content operations
  (`read_file`/`write_file`/`make_dir`/`list`/`remove`) stay
  `UnimplementedError` (no data plane outside a MicroVM).
- **`scripts/measure/efs_volumes.py`**: `plan`/`run --region --run-id`/
  `report`/`cleanup --run-id`, idempotent (resolves by tag), tags every
  resource `rayito:measurement=efs-volumes`, never writes account IDs or
  resource IDs to the repo (state in `$XDG_STATE_HOME/rayito-measure/`).
- Docs (`funciones-opcionales/volumenes-efs.md`, marked experimental),
  `AWS_API_NOTES.md` §22, ADR-018, `MILESTONES.md`, three CHANGELOGs,
  `docs-delta.md` for `m15-docs-integration`.
- Merges `docs/research/2026-10-efs-persistence.md` from
  `origin/research/m11-efs` (PR #59) onto this branch, renaming its OpenSpec
  references from `m11-efs-volumes` to `m15-efs-volumes` and its questions
  from `Q79-Q98` to `EFS-1..EFS-20` (`Q79` is already answered: `nfs4` is
  compiled into the guest kernel, confirmed by the M10 measurement
  campaign).

## Impact

- Affected specs: new capability `efs-volumes` (this change); no existing
  capability's requirements change (s3-mounts, sizes-catalog, etc. are
  untouched, separate changes).
- Affected code: `crates/rayd-core/src/volume/`, `crates/rayd/src/{adapters/efs_mount.rs,features/efs_volumes.rs,grpc/configure.rs}`
  (one unavoidable 2-line fix: the stub `EfsVolumesStatus{}`/`request.efs_volumes?`
  construction foundations wrote no longer compiles once the message gets a
  `repeated` field; see tasks.md 2.4), `proto/rayito/v1/efs_volumes.proto`,
  `clients/python/src/rayito/_volumes/`, `clients/python/src/rayito/{_feature_options.py,__init__.py,e2b/}`,
  `clients/typescript/src/volumes/`, `clients/typescript/src/{feature-options.ts,index.ts,e2b/}`,
  `infra/efs-volumes.yaml`, `scripts/measure/efs_volumes.py`.
- Off by default: no `VolumeStore(...)` and no `volumes=` means no `efs`
  client, no `ConfigureSandbox` call, and the existing zero-cost golden
  tests stay byte-for-byte unchanged (no 0.6 option touches this feature's
  code path).
- No AWS touched by this change: the measurement campaign and the full
  mounting implementation are a separate, future change once EFS-2, EFS-3
  and EFS-8 clear.

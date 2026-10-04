## Why

E2B's `Volume` API and `Sandbox.create(volume_mounts=)` give an agent a
directory that outlives every sandbox and is shared live between several of
them. Rayito declares both "out by SPEC" (`docs/site/docs/e2b-parity.md`
rows 26, 56, 90; `SPEC.md` §4: "EFS sigue fuera"), and its only analogue,
the S3 checkpoint of ADR-009 (`persist=`), is a copy taken at a point in
time, not a shared file system. Mapping `volume_mounts` onto `persist=`
would silently approximate the feature (constitution rule 7).

`docs/research/2026-10-efs-persistence.md` concludes that EFS is **viable
under conditions**: the platform documents that `additionalOsCapabilities:
["ALL"]` allows "mounting filesystems" and that a VPC egress network
connector reaches private resources of the customer's VPC; a third party
mounted EFS from a MicroVM over plain NFS. Nothing verifies the parts a
safe design needs: an NFSv4.1 client compiled into a guest kernel without
loadable modules, `amazon-efs-utils` (TLS + IAM + access points) running
without `systemd` under `rayd` as PID 1, the credentials of the execution
role read from the MicroVM's IMDSv2, Rayito's first customer-owned VPC
connector (Q46 was never measured) and its coexistence with
`INTERNET_EGRESS`, and a live NFS session across suspend/resume. The
report also found that today's `/suspend` calls `sync(2)` without a
budget, which would hang on any unreachable network mount.

This change is therefore **measurement-first**: its first phase runs the
Q79–Q98 measurements of the report and stops, per constitution rule 8, if
any stop question (Q79, Q80, Q81, Q85, Q86, Q89, Q91) fails.

## What Changes

- **Measurements (phase 1, gate)**: Q79–Q98 on real AWS in a VPC owned or
  lent for the purpose, recorded as rows of `AWS_API_NOTES.md` §16, plus a
  new §19 with the EFS API contract (`CreateAccessPoint`,
  `DescribeAccessPoints`, `DeleteAccessPoint`, `DescribeMountTargets`,
  `TagResource`) copied from the `efs` 2015-02-01 service model and the
  mount-helper options actually used. No code before this phase is green.
- **Architecture record**: ADR-014 "EFS volumes mounted by `rayd` through a
  customer VPC connector, one access point per volume"; `SPEC.md` §4 amends
  the EFS non-goal; `SECURITY.md` gains T18.
- **Contract**: new `proto/rayito/v1/volume.proto` (`VolumeService.Mount`,
  `VolumeService.List`) and `HealthResponse` fields 16
  (`volumes_supported`) and 17 (`repeated VolumeStatus volumes`), additive.
- **`rayd-core`**: module `volume` (spec validation, plan, per-mount state
  machine, retry and resume decisions, `VolumeError`) and the port
  `VolumeMounter`, introduced together with its adapter.
- **`rayd`**: adapter `EfsUtilsMounter` (`mount -t efs` with TLS, IAM,
  access point and mount-target IP; supervision of the mount watchdog),
  hook integration (`/run` background mount, `/suspend` bounded `syncfs`
  replacing unbounded `sync(2)`, `/resume` probe and remount,
  `/terminate` lazy unmount), egress rules that keep uid ≥ 1000 away from
  the mount targets and the local `efs-proxy` port.
- **Image and infra**: `rayito-base-caps` ships `amazon-efs-utils` and an
  allowlist of file systems in its environment; new
  `infra/efs-volumes.yaml` (file system, policy, mount targets, security
  groups, connector, operator role) and parameters in `infra/iam.yaml`
  (execution-role client actions per tenant, caller access-point actions,
  `lambda:PassNetworkConnector`).
- **SDKs**: `EfsVolume`, `VolumeStore`, `Sandbox.create(volumes=)`,
  `sbx.volumes`, `VolumeMountException`, pool support, in Python (sync and
  async) and TypeScript.
- **E2B shim**: `Volume`/`AsyncVolume` management on access points,
  `volume_mounts`, `SandboxInfo.volume_mounts`; the `Volume` file
  operations stay `UnimplementedError`.
- **Docs and acceptance**: `volumes.md`, parity rows, real-AWS e2e in
  Python and TypeScript.

## Impact

- Affected specs: new capabilities `volume-mounts` (agent, image, infra,
  measurements) and `sdk-volumes` (native SDKs and the E2B shim).
- Affected code: `proto/rayito/v1/{volume,health}.proto`,
  `crates/rayd-core/src/volume/`, `crates/rayd/src/{adapters,hooks,grpc,network}/`,
  `image/Dockerfile`, `infra/`, `clients/python/src/rayito/`,
  `clients/typescript/src/`, `docs/site/docs/`, `AWS_API_NOTES.md`,
  `ARCHITECTURE.md`, `SPEC.md`, `SECURITY.md`, `MILESTONES.md`.
- Only `rayito-base-caps` gains volumes; `rayito-base` answers
  `volumes_supported=false` and the SDK fails closed.
- Operators need a VPC with one subnet per AZ, an EFS file system and a
  connector; EFS storage and throughput are billed to them (report §6:
  $0.30/GB-month Standard, $0.03/GB read, $0.06/GB written, us-east-1).
- The `/suspend` change (bounded `syncfs`) ships with M11 but also
  protects any future network mount.
- Measurement campaign ≈ $1–2 of AWS usage; effort ≈ 22–23 person-days,
  of which ≈ 4 are spent even if a stop question fails.

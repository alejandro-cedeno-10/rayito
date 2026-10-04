## 1. Measurements on real AWS (gate; no code before this group is green)

- [ ] 1.1 Borrow or create a VPC for the purpose (one subnet per AZ, no
  other workload); record the deploy gate decision of Q46.
- [ ] 1.2 Copy the `efs` 2015-02-01 operations `CreateAccessPoint`,
  `DescribeAccessPoints`, `DeleteAccessPoint`, `DescribeMountTargets` and
  `TagResource` into `docs/aws-api/efs/` and write `AWS_API_NOTES.md` §19
  (EFS contract: fields, patterns, errors, the four client condition keys).
- [ ] 1.3 Q79 and Q80 (stop): `/proc/filesystems` and a probe `mount` in
  `rayito-base` and `rayito-base-caps`.
- [ ] 1.4 Minimal `infra/efs-volumes.yaml` deployed; Q81 (stop), Q82, Q83,
  Q96 from a caps VM launched with the connector.
- [ ] 1.5 Q85 (stop): caps image version with `amazon-efs-utils`; record
  `snapshotBuild` deltas and build time.
- [ ] 1.6 Q84 and Q86 (stop): `mount -t efs -o tls,iam,accesspoint,mounttargetip`
  as root without `systemd`; who starts `efs-proxy` and the watchdog; 20
  mount latencies; error texts for closed SG, IAM denied, unknown AP.
- [ ] 1.7 Q87, Q88, Q92, Q93, Q94, Q95, Q98.
- [ ] 1.8 Q89 (stop), Q90, Q91 (stop): suspend/resume with a live mount at
  60 s, 10 min, 60 min and 70 min; `/suspend` with an unreachable mount
  target, with `sync(2)` and with bounded `syncfs`.
- [ ] 1.9 Write every result into `AWS_API_NOTES.md` §16 (rows Q79–Q98) and
  the report; if a stop question failed, stop here, amend the report and
  `e2b-parity.md` row 26, and close the change without implementation.
- [ ] 1.10 Tear down: stack, access points, data, zero MicroVMs.

## 2. Architecture record

- [ ] 2.1 ADR-014 in `ARCHITECTURE.md` (context, decision D2–D10,
  consequences, rejected options B and C).
- [ ] 2.2 `SPEC.md` §4: amend "EFS sigue fuera".
- [ ] 2.3 `SECURITY.md` T18 (per-role isolation, `efs-proxy` local tunnel,
  POSIX permissions inside a volume, deleted volume while suspended).
- [ ] 2.4 `MILESTONES.md` M11 with acceptance criteria.

## 3. Contract

- [ ] 3.1 `proto/rayito/v1/volume.proto`: `VolumeService.Mount`,
  `VolumeService.List`, `VolumeSpec`, `VolumeStatus`, `MountState`.
- [ ] 3.2 `HealthResponse` fields 16 `volumes_supported` and 17
  `repeated VolumeStatus volumes`; `buf lint` and `buf breaking` clean.
- [ ] 3.3 Regenerate Rust, Python and TypeScript clients (`make proto`).

## 4. rayd-core

- [ ] 4.1 `volume::spec` validation (paths, ids, allowlist, 4-mount cap,
  overlap and nesting) with table-driven tests.
- [ ] 4.2 `volume::state` state machine and `RetryPolicy`.
- [ ] 4.3 `volume::lifecycle` pure actions for `/run`, `/suspend`,
  `/resume`, `/terminate` and `ResumeDecision` from `ProbeOutcome`.
- [ ] 4.4 Port `VolumeMounter`; `VolumeManager` over fake ports; `VolumeError`
  without ids, paths or IPs.

## 5. rayd adapter

- [ ] 5.1 `EfsUtilsMounter`: scratch environment, option string from §19,
  stderr classified into fixed classes, never logged verbatim.
- [ ] 5.2 Watchdog supervision under `rayd` as PID 1 (per Q86).
- [ ] 5.3 Child-process `probe` with budget; reaping of abandoned probes.
- [ ] 5.4 Hooks: background mount on `/run`; bounded `syncfs` replacing
  `sync(2)` on `/suspend`; probe and remount on `/resume` with
  `volume_state_lost`; lazy unmount on `/terminate`.
- [ ] 5.5 `VolumeService` gRPC with `x-access-token`; `Health` fields.
- [ ] 5.6 Egress: `blackhole` of mount-target IPs and `prohibit` of the
  `efs-proxy` port(s) for uid 1000–65535 under caps.
- [ ] 5.7 Adapter tests in the Lima VM against a local NFSv4.1 server
  (mount, stale, hung, remount), `clippy` pedantic clean.

## 6. Image and infrastructure

- [ ] 6.1 `image/Dockerfile` caps stage with `amazon-efs-utils`;
  `RAYITO_EFS_ALLOWED_FILE_SYSTEMS` in the published environment.
- [ ] 6.2 `infra/efs-volumes.yaml` complete (file system, policy, mount
  targets, security groups, connector, operator role with the documented
  `ec2:CreateNetworkInterface`/`ec2:CreateTags` permissions); review
  `infra/egress-connector.yaml` against the same documentation.
- [ ] 6.3 `infra/iam.yaml` parameters for volumes (execution role client
  actions per tenant, caller access-point actions and
  `lambda:PassNetworkConnector`); `make infra-lint` clean.

## 7. SDKs

- [ ] 7.1 Python `EfsVolume`, `VolumeStore`, `VolumeMountException`,
  `create(volumes=, volume_timeout=)`, `sbx.volumes`, pool `take()` with
  volumes; sync/async parity; unit tests against fakes.
- [ ] 7.2 TypeScript mirror (`EfsVolume`, `VolumeStore`,
  `create({ volumes, volumeTimeoutMs })`, `VolumeMountError`).
- [ ] 7.3 `limits.json` entries (max mounts, default volume timeout) and
  regenerated `_limits.py` / `limits.ts`.

## 8. E2B shim

- [ ] 8.1 Python `Volume`/`AsyncVolume` management and `volume_mounts`;
  `SandboxInfo.volume_mounts`; `Volume*Exception` raised.
- [ ] 8.2 TypeScript `Volume` and `volumeMounts`.
- [ ] 8.3 `Volume` file operations keep `UnimplementedError` with a reason.

## 9. Documentation

- [ ] 9.1 `docs/site/docs/volumes.md` (setup, costs, suspend/resume rules,
  no SQLite/`flock`, limits).
- [ ] 9.2 `e2b-parity.md` rows 26, 34, 56, 90; `persistence.md` and
  `network.md` cross-links; `CHANGELOG.md`.

## 10. Real-AWS acceptance

- [ ] 10.1 `clients/python/tests/e2e/test_m11_volumes.py` (report §10,
  scenarios 1–8) green on the republished caps image.
- [ ] 10.2 `clients/typescript/tests/e2e/m11.e2e.test.ts` (scenarios 1, 3, 7).
- [ ] 10.3 Measured numbers in `AWS_API_NOTES.md`, ADR-014 and
  `MILESTONES.md`; zero MicroVMs and zero tagged access points left.

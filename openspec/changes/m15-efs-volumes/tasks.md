## 1. Research merge

- [x] 1.1 Merge `docs/research/2026-10-efs-persistence.md` from
      `origin/research/m11-efs` (PR #59) onto this branch.
- [x] 1.2 Rename its OpenSpec references `m11-efs-volumes` -> `m15-efs-volumes`.
- [x] 1.3 Rename its questions `Q79-Q98` -> `EFS-1..EFS-20`; `EFS-1` (ex
      `Q79`) marked answered (`nfs4` compiled into the guest kernel, per the
      M10 campaign).

## 2. rayd-core domain and rayd adapter

- [x] 2.1 `crates/rayd-core/src/volume/{mod,spec,plan,state,ports,error}.rs`:
      `VolumeSpec`/`FileSystemId`/`AccessPointId`/`MountPath`/
      `MountTargetIp` (validated), `VolumePlan` (cap 4, dedupe, sorted),
      `MountState` (7 states, pure transition function), `VolumeError`,
      `VolumeMounter` port (`BoxFuture`-returning, `dyn`-safe).
- [x] 2.2 `crates/rayd-core/src/lib.rs`: `pub mod volume;`.
- [x] 2.3 `crates/rayd/src/adapters/efs_mount.rs`: `UnavailableEfsMounter`
      (`VolumeMounter` impl, always `Unsupported`), registered in
      `adapters/mod.rs`.
- [x] 2.4 `features/efs_volumes.rs`: `EfsVolumesSlot` over the
      `VolumeMounter` port (design.md D1): `supported()` is the mounter's
      `support()`, `apply()` answers `UNSUPPORTED` while it is unsupported
      (every shipped build), and behind a supporting mounter validates the
      section into a `VolumePlan`, unmounts dropped/changed paths and mounts
      the rest in plan order (tested with a fake mounter). `proto/rayito/v1/efs_volumes.proto` gains real fields, which
      requires two mechanical fixes in foundations'
      `crates/rayd/src/grpc/configure.rs` (its own code no longer compiles
      against a message with a `repeated` field): `request.efs_volumes?`
      -> `request.efs_volumes.clone()?` (prost stops deriving `Copy`), and
      the test's `EfsVolumesStatus {}` literal -> `EfsVolumesStatus::default()`.
- [x] 2.5 Unit tests: `volume::spec`, `volume::plan`, `volume::state`
      (rayd-core, 35 tests) and `adapters::efs_mount` (rayd). `cargo test
      --workspace` green in the Lima VM; `cargo clippy --workspace
      --all-targets -- -W clippy::pedantic` adds no new warning.

## 3. Proto

- [x] 3.1 `proto/rayito/v1/efs_volumes.proto`: `EfsVolumesConfig`
      (`repeated EfsVolumeMount`), `EfsVolumesStatus` (`repeated
      EfsVolumeStatus`), `EfsVolumeState` enum mirroring `MountState`.
- [x] 3.2 `buf lint` and `buf generate` regenerate Python/TypeScript
      bindings with no change to any other feature's generated file.

## 4. SDK surface (Python)

- [x] 4.1 `rayito._volumes.{_domain,_base,_store,_store_async,_section}`:
      `EfsVolume`, `VolumeStatus`, `VolumeStore`/`AsyncVolumeStore` (real
      CRUD over `CreateAccessPoint`/`DescribeAccessPoints`/
      `DeleteAccessPoint`, lazy `LazyClient`), `require_volume_support`.
- [x] 4.2 `_feature_options.py`: `volumes=` branch replaced with
      `require_volume_support` (design.md D2).
- [x] 4.3 `__init__.py`: exports `EfsVolume`, `VolumeStore`,
      `AsyncVolumeStore`, `VolumeStatus`.
- [x] 4.4 `_stacks/components/efs_volumes.py`: real `StackComponent`
      (`supported=True`), parameters, `CostStatement`.
- [x] 4.5 `infra/efs-volumes.yaml`; `scripts/gen_stack_assets.py` renders
      the packaged template for both SDKs.
- [x] 4.6 `e2b/_volume.py`: `Volume`/`AsyncVolume` moved out of
      `e2b/_unimplemented.py`, real `create`/`connect`/`list`/`get_info`/
      `destroy` over a configured `VolumeStore`; content operations stay
      `UnimplementedError`.
- [x] 4.7 Tests: `test_m15_efs_volumes_{domain,store,section}.py`,
      `fake_efs.py`; `pytest`/`ruff check`/`ruff format --check`/`mypy` all
      green.

## 5. SDK surface (TypeScript)

- [x] 5.1 `src/volumes/{domain,efs,store,section}.ts` mirroring 4.1/4.2.
- [x] 5.2 `src/feature-options.ts`: `volumes` branch replaced with
      `requireVolumeSupport`.
- [x] 5.3 `src/index.ts`: exports `EfsVolume`, `VolumeStore`.
- [x] 5.4 `package.json`: `@aws-sdk/client-efs` as optional peer +
      devDependency (foundations had not added it); lockfile updated.
- [x] 5.5 Fixed two shared parametrized tests whose `volumes` row assumed
      the old generic-stub behaviour (design.md D4):
      `m15-create-options.test.ts`/`test_m15_create_kwargs.py` (now use a
      well-formed `EfsVolume`) and
      `m15-feature-options.test.ts`/`test_m15_feature_options.py` (volumes
      row removed, covered by this change's own test file instead).
- [x] 5.6 Fixed `m15-stacks.test.ts`/`test_m15_stacks_service.py`'s
      "destroy an unsupported component" test, which used `efs-volumes` as
      its example (now `sizes-guard`, design.md D4).
- [x] 5.7 `class VolumeStore`'s cost check lives in the drop-in
      `clients/typescript/cost-declarations/efs-volumes.json`;
      `scripts/check-dts-cost-blocks.mjs` gains the glob foundations left
      for the first feature that needed it (v06-foundations design E6),
      without touching its shared `COST_DECLARATIONS` array. The TSDoc cost
      block sits on the class declaration itself (a file-header comment is
      dropped by tsdown's `.d.mts` bundling).
- [x] 5.8 Tests: `m15-efs-volumes.test.ts`; `pnpm lint`/`typecheck`/`test`/
      `build`/`pack:check` all green.

## 6. Docs, ADR, AWS_API_NOTES, CHANGELOGs

- [x] 6.1 `docs/site/docs/funciones-opcionales/volumenes-efs.md`: filled
      in, marked experimental, "Coste y activación" box, Python/TypeScript
      tabs.
- [x] 6.2 `docs/site/docs/referencia/python/opcionales.md`: `VolumeStore`/
      `EfsVolume`/`VolumeStatus` mkdocstrings sections.
- [x] 6.3 `docs/site/docs/referencia/typescript.md`: `VolumeStore` row.
- [x] 6.4 `AWS_API_NOTES.md` §22: `CreateAccessPoint`/
      `DescribeAccessPoints`/`DeleteAccessPoint`/`DescribeMountTargets`
      parameter contract (botocore 1.43.103 verified).
- [x] 6.5 `ARCHITECTURE.md` ADR-018: filled in.
- [x] 6.6 `MILESTONES.md` M15 efs-volumes bullet, `docs/RELEASE_NOTES_0.6.0.md`
      section.
- [x] 6.7 Three CHANGELOGs' `<!-- m15-efs-volumes -->` anchors.
- [x] 6.8 `docs-delta.md`: exact row replacements for `e2b-parity.md` (row
      26), `optional-features.md`, `cost.md`, `security.md` (T21),
      `limits.md`, for `m15-docs-integration` to apply.

## 7. Measurement script (not run by this change)

- [x] 7.1 `scripts/measure/efs_volumes.py`: `plan`/`run`/`report`/
      `cleanup`; idempotent by tag (`rayito:measurement=efs-volumes`,
      `rayito:run-id`, `rayito:expires-at`); state under
      `$XDG_STATE_HOME/rayito-measure/`. `run` provisions a throwaway
      VPC/subnet and the `efs-volumes` `OptionalStack` (via
      `rayito.OptionalStacks`), tags the stack's always-retained file
      system with the run id, creates one access point and attaches
      `CallerPolicyArn` to `--execution-role-arn`.
- [x] 7.2 The ★ stop criteria are automated (`AUTOMATED`), in research doc
      §9 order, through `rayito.Sandbox` against `--caps-template`:
      EFS-2 (tmpfs + `mount -t nfs4` to TEST-NET in caps; `EPERM` in the
      default image with `--default-template`), EFS-3 (connector `ACTIVE`
      + TCP 2049 to the mount target), EFS-8 (20 × `mount -t efs -o
      tls,iam,accesspoint,mounttargetip` without systemd, p50/p95,
      `efs-proxy` count), EFS-11 (pause 60 s/10 min/60 min × 3 cycles,
      time to first correct read after resume) and EFS-13 (`pause()` with
      the mount-target ingress revoked; the rule is always restored). Each
      records pass/fail; a ★ failure marks the run stopped and calls
      `cleanup` immediately; a stopped run refuses to resume.
- [x] 7.3 `cleanup` walks `CLEANUP_ORDER` (detach client policy, stack,
      retained file system + its access points, subnet, VPC) and only
      drops a stage once its delete succeeded; results survive for
      `report`. Nothing an AWS id/IP/ARN reaches state or stdout
      (`redact`).
- [x] 7.4 `scripts/tests/test_measure_efs_volumes.py`: fakes for
      `MeasurementAwsPort` and `GuestPort` — campaign order, stop-and-clean
      on a ★ failure, EFS-13 restoring ingress on a hang, redaction,
      resume skipping answered steps, cleanup order. No AWS calls.

### 7.5 Acceptance checklist (serialized AWS stage, cap $3, in order)

This change does not run any of it. The acceptance agent follows it step
by step and stops at the first ★ failure (the script then cleans up by
itself; re-run `cleanup` until it exits 0 anyway).

1. `cd clients/python && uv run python ../../scripts/measure/efs_volumes.py plan`
   — check the estimate is under the cap.
2. **EFS-7 (★, by hand)**: publish a throwaway `rayito-base-caps` version
   with `amazon-efs-utils` (and `efs-proxy`) installed, `ALL` and the image
   environment variable `RAYITO_ALLOW_ROOT=1` (every automated step runs
   as root; without it `rayd` rejects `user="root"`); record the
   `codeInstallSizeInBytes`/`memorySnapshotSizeInBytes` delta and build
   time in `AWS_API_NOTES.md` §16. If the package cannot be installed on
   `al2023-minimal` ARM64, stop here (no infra exists yet).
3. `RAYITO_E2E_VPC_ID=<vpc> RAYITO_E2E_SUBNET_IDS=<s1,s2> ... efs_volumes.py
   run --region <r> --run-id <id> --caps-template <throwaway caps>
   --execution-role-arn <infra/iam.yaml role> [--default-template
   <rayito-base>]` — the existing VPC comes only from those variables (or
   `--vpc-id`/`--subnet-ids`) and is never created, modified, recorded or
   deleted (needed where an SCP denies `ec2:CreateVpc`, Q124); `run` first
   runs `EfsVolumes.check` and stops before creating anything on a `FAIL`
   — then measures EFS-2, 3, 8, 11, 13 and stops + cleans up on the first
   ★ failure. `--efs11-pauses 60` only for
   a quick smoke run; the go/no-go needs the default 60,600,3600.
4. Only if step 3 exited 0, by hand against the infra it left up
   (`rayito stack status efs-volumes --stack-name
   rayito-efs-volumes-measure-<id>` for the outputs): EFS-4
   (`INTERNET_EGRESS` + connector), EFS-5 (DNS of `<fs-id>.efs...`),
   EFS-9 (throughput), EFS-12 (70-min pause), EFS-15 (two sandboxes on the
   same access point), EFS-16 (policy denials: no AP, no TLS, role without
   `ClientWrite` via `AllowWrite=false`, another tenant's AP).
5. `... efs_volumes.py report --run-id <id>`; copy the redacted rows into
   `AWS_API_NOTES.md` §16 and the research doc §9.
6. `... efs_volumes.py cleanup --run-id <id>` until it exits 0; then
   confirm no file system tagged `rayito:run-id=<id>` and no stack
   `rayito-efs-volumes-measure-<id>` remain, that the VPC's route tables,
   NACLs and security groups are as before, and delete the throwaway caps
   image version (and only it).

### 7.6 Acceptance run 2026-10-02 (results in `AWS_API_NOTES.md` §16 Q122–Q125)

- [x] Step 1 `plan`: ~$0.82, under the $1.50 cap.
- [x] Step 2 EFS-7 ★ passes (Q122): `amazon-efs-utils-3.1.3` from the
      AL2023 repo, code install +197.6 MB, memory unchanged, build +10 s;
      the real image layer must re-link `/usr/bin/python3` to 3.12.
- [x] EFS-2 ★ passes (Q123), measured with `measure_efs2` without a VPC.
- [ ] Step 3 **blocked** (Q124): the test account's organization SCP denies
      `ec2:CreateVpc`; `run` stopped before creating anything. EFS-3, 8, 11
      and 13 (★) and steps 4-5 wait for a VPC borrowed with permission
      (`--vpc-id`/`--subnet-ids`, or `RAYITO_E2E_VPC_ID`/
      `RAYITO_E2E_SUBNET_IDS`; see §9).
- [x] Python + TypeScript e2e of the CRUD, the E2B shim and the
      `volumes=`/`volume_mounts` gates against a throwaway file system;
      found and fixed the `DescribeAccessPoints` lag (Q125).
- [x] Off-by-default: a plain `create`/`run`/`kill` touches only
      `lambda-microvms` (plus the `sts`/`sso` calls 0.5.1 already made).
- [x] Cleanup: only this run's resources deleted (two throwaway images,
      their zips and log groups, one file system); before/after inventory
      identical.

### 7.7 Acceptance run 2026-10-04 (results in `AWS_API_NOTES.md` §16 Q126–Q134)

An existing VPC of the test account, used with the maintainer's permission
and passed only through `RAYITO_E2E_VPC_ID`/`RAYITO_E2E_SUBNET_IDS` (two
private subnets in two AZs, default route to a transit gateway, no NAT).

- [x] Python e2e `test_m15_efs_volumes_vpc.py`: 2 passed after two test
      fixes (EC2 returns route-table associations in any order; a security
      group created meanwhile by another deploy in the same VPC is not a
      change; `DeleteFileSystem` is asynchronous). TypeScript check e2e:
      1 passed. `check()`'s egress note now also counts non-NAT default
      routes (both SDKs).
- [x] Step 2 EFS-7: throwaway caps image with `amazon-efs-utils`, built in
      227 s.
- [x] Step 3 `run --efs11-pauses 60,600`: EFS-2, EFS-3, EFS-8, EFS-11 and
      EFS-13 (★) pass (Q127–Q130). EFS-13 detail: no hang, but with
      unflushed writes and the mount target unreachable the platform
      terminates the MicroVM after `rayd`'s 5 s sync deadline.
- [x] Step 4: EFS-4 (only one egress connector per VM), EFS-5, EFS-6,
      EFS-9, EFS-10 (uid 1000 reaches the local `efs-proxy` port), EFS-15,
      EFS-16 (unscoped policy) measured; **EFS-12 fails** (70-min pause:
      `Permission denied` until a remount). Not measured: EFS-16 with
      `AccessPointArns`/`AllowWrite=false`, EFS-14, 17, 19, 20.
- [ ] Step 6 cleanup (`efs_volumes.py cleanup --run-id <id>`, the throwaway
      image, its zip and log group) and the before/after inventory diff:
      blocked by the expired SSO session; resources are tagged with the run
      id and listed in the acceptance report.

## 9. Existing VPC quick setup (`EfsVolumes`, design D5)

- [x] 9.1 Merge `origin/main` (FeatureSet/single Configure path kept;
      `AWS_API_NOTES.md` rows renumbered Q96–Q99 → Q122–Q125, Q121 left
      for PR #74's own renumbering).
- [x] 9.2 `infra/efs-volumes.yaml`: `SubnetIds` list (1–3), `AccessPointArns`,
      `ClientSecurityGroupId` output, policy scoped to this file system and
      its access points; template tests pin "no network resource" and
      "rules only on own groups"; `make infra-lint` lints it (cfn-lint
      1.56.3 clean).
- [x] 9.3 Pure check (`_volumes/_network.py`, `src/volumes/network.ts`) +
      read-only EC2 adapter (`_vpc.py`, `vpc.ts`, peer
      `@aws-sdk/client-ec2`); `EfsVolumes`/`AsyncEfsVolumes`/TS
      `EfsVolumes`: `check`, `deploy` (refuses on `FAIL`), `status`,
      `volume_store`, `destroy(delete_file_system=)`, `delete_file_system`
      (tag-guarded). `AWS_API_NOTES.md` §22 rows for every new operation.
- [x] 9.4 `rayito doctor --efs-vpc-id/--efs-subnet-ids` (`efs-network`).
- [x] 9.5 Unit tests with fakes (Python + TS), e2e
      `test_m15_efs_volumes_vpc.py` (deploy/CRUD/destroy, VPC snapshot
      before/after) and TS check-only e2e, gated on
      `RAYITO_E2E_VPC_ID`/`RAYITO_E2E_SUBNET_IDS`.
- [x] 9.6 Docs page "Volúmenes EFS en tu VPC", `cli.md`, references;
      T21 delta extended (docs-delta.md).
- [x] 9.7 Measurement script: no throwaway VPC; network only from
      args/env; `EfsVolumes` for check/deploy/destroy/delete.
- [ ] 9.8 AWS acceptance (serialized stage, cap $3): e2e on an existing
      VPC, then §7.5 steps 3–6. Measured 2026-10-04 (§7.7); cleanup and the
      inventory diff are still pending (the SSO session expired first).


## 10. Hardening from the 2026-10-04 acceptance (design D6–D10, no AWS)

- [x] 10.1 `rayd_core::volume`: `proxy` (attribute the `efs-proxy` a mount
      started by pid + start time; liveness), `resume` (`plan_resume`,
      `after_probe`, closed `DegradeReason` strings), `MountReceipt`
      (credentials expiry), `FlushOutcome`, `MountFailureClass::InvalidPath`,
      new `MountTransition`s (`FlushTimedOut`, `CredentialsExpired`,
      `ProbeFoundHealthy`).
- [x] 10.2 `adapters::mountpoint`: the symlink-proof walk, `/proc/self/fd`
      targets, bind mount, detach and the bounded `stat` probe, extracted from
      `fuse_device` and shared by both mount features; `bounded_sync::
      spawn_syncfs_thread` shared by `BoundedFlush` and the EFS flush;
      `capabilities::{binary_on_path, kernel_supports_filesystem}`.
- [x] 10.3 `adapters::efs_mount::EfsUtilsMounter` replaces
      `UnavailableEfsMounter`: helper through `ChildRegistry` on a root-only
      staging dir, bind onto the requested path, proxies stopped on unmount
      (SIGTERM, SIGKILL after a grace), lease expiry from
      `ImdsCredentialBroker`, bounded per-volume `syncfs`; support only with
      `CAP_SYS_ADMIN`, `nfs4`, `mount`, `mount.efs` and `efs-proxy`.
      Tests with a fake host, plus a real-process stop test in the Lima VM.
- [x] 10.4 `features::efs_volumes`: lifecycle participant (`/suspend` flush
      with `DEGRADED`/`flush_timeout`, `/resume` remount or probe with a
      1.5 s wait and background completion reported by `ConfigureStatus`,
      `/terminate` unmount), generations so a remount never resurrects a
      dropped volume, `root_egress_class = Efs`.
- [x] 10.5 `infra/efs-volumes.yaml`: `ReadOnlyAccessPointArns` (explicit
      `Deny` of `ClientWrite`); both SDK components and
      `EfsVolumes.deploy(read_only_access_point_arns=)`/
      `readOnlyAccessPointArns`; template tests.
- [x] 10.6 SDKs: `volumes=` requires exactly one own connector in
      `egress=` (never `INTERNET_EGRESS`) before launch, naming the VPC
      alternative; `plan_features`/`planFeatures` take `egress`;
      `has_internet_connector`/`hasInternetConnector` move to the models;
      the shim's `volume_mounts` explains the single-connector limit;
      `check()`'s `internet-egress` message says the same.
- [x] 10.7 Docs: both EFS pages (five-minute setup, internet and volumes,
      what `rayd` does with each volume, data-loss warning, real read-only),
      `SECURITY.md` T21, `AWS_API_NOTES.md` §22, ADR-018 addendum, research
      doc §9, `MILESTONES.md`, three CHANGELOGs, `docs-delta.md`.
- [ ] 10.8 AWS re-check (serialized stage, ≤ $2): EFS-12 (a remount after
      a pause past the credentials' expiry restores reads, through `rayd`'s
      own `/resume`), EFS-16 scoped (`AccessPointArns` + 
      `ReadOnlyAccessPointArns`/`AllowWrite=false`: writes through the
      mount and through the loopback tunnel denied), the adapter's own mount
      path (bind from staging, `ro`, no `mounttargetip`) and proxy cleanup
      after `umount`.

## 8. OpenSpec

- [x] 8.1 `proposal.md`, `design.md`, `tasks.md` (this file),
      `specs/efs-volumes/spec.md`.
- [x] 8.2 `npx -y @fission-ai/openspec@1.10.0 validate --strict` passes.

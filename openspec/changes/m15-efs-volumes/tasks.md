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
- [x] 2.4 `features/efs_volumes.rs` stays `slot::Unsupported` (design.md
      D1). `proto/rayito/v1/efs_volumes.proto` gains real fields, which
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
- [x] 5.7 `scripts/check-dts-cost-blocks.mjs`: added `class VolumeStore`;
      moved the TSDoc cost block onto the class declaration itself (a
      file-header comment is dropped by tsdown's `.d.mts` bundling).
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

### 7.5 Acceptance checklist (serialized AWS stage, cap $1.50, in order)

This change does not run any of it. The acceptance agent follows it step
by step and stops at the first ★ failure (the script then cleans up by
itself; re-run `cleanup` until it exits 0 anyway).

1. `cd clients/python && uv run python ../../scripts/measure/efs_volumes.py plan`
   — check the estimate is under the cap.
2. **EFS-7 (★, by hand)**: publish a throwaway `rayito-base-caps` version
   with `amazon-efs-utils` (and `efs-proxy`) installed; record the
   `codeInstallSizeInBytes`/`memorySnapshotSizeInBytes` delta and build
   time in `AWS_API_NOTES.md` §16. If the package cannot be installed on
   `al2023-minimal` ARM64, stop here (no infra exists yet).
3. `... efs_volumes.py run --region <r> --run-id <id> --caps-template
   <throwaway caps> --execution-role-arn <infra/iam.yaml role>
   [--default-template <rayito-base>]` — measures EFS-2, 3, 8, 11, 13 and
   stops + cleans up on the first ★ failure. `--efs11-pauses 60` only for
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
   confirm no file system tagged `rayito:run-id=<id>`, no stack
   `rayito-efs-volumes-measure-<id>` and no VPC with that tag remain, and
   delete the throwaway caps image version (and only it).

## 8. OpenSpec

- [x] 8.1 `proposal.md`, `design.md`, `tasks.md` (this file),
      `specs/efs-volumes/spec.md`.
- [x] 8.2 `npx -y @fission-ai/openspec@1.10.0 validate --strict` passes.

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

## 7. Measurement script (not run)

- [x] 7.1 `scripts/measure/efs_volumes.py`: `plan`/`run --region --run-id`/
      `report`/`cleanup --run-id`; idempotent by tag
      (`rayito:measurement=efs-volumes`, `rayito:run-id`,
      `rayito:expires-at`); state under `$XDG_STATE_HOME/rayito-measure/`.
      `run`/`cleanup` provision and tear down the real infra (a throwaway
      VPC/subnet plus the `efs-volumes` `OptionalStack`, via
      `rayito.OptionalStacks`) behind an injectable `MeasurementAwsPort`,
      resolved by the `rayito:run-id` tag before creating anything new;
      `cleanup` only drops a stage from local state once its delete has
      actually succeeded. **Still a scaffold, on purpose**: the EFS-2/3/8/
      9/11/12/13/15/16 measurements that need a running MicroVM against
      that file system/connector are not launched by this script — they
      are gathered by hand by the AWS acceptance stage against the infra
      `run` provisions. Only the infra lifecycle (create/discover/destroy)
      is real; the measurement-taking itself is not implemented here.
- [x] 7.2 `scripts/tests/test_measure_efs_volumes.py`: the script's
      tag-based discovery, reverse-dependency-order cleanup and
      partial-failure-keeps-state behaviour, against a fake
      `MeasurementAwsPort` — no AWS calls.

## 8. OpenSpec

- [x] 8.1 `proposal.md`, `design.md`, `tasks.md` (this file),
      `specs/efs-volumes/spec.md`.
- [x] 8.2 `npx -y @fission-ai/openspec@1.10.0 validate --strict` passes.

## 1. Proto (owned fields inside `s3_mounts.proto`)

- [x] 1.1 `S3MountsConfig{mounts}`, `S3Mount{mount_path, bucket, prefix, read_only, allow_overwrite, allow_delete}`.
- [x] 1.2 `S3MountsStatus{mounts}`, `S3MountState{mount_path, phase, error_class}`, `S3MountPhase` enum.
- [x] 1.3 Regenerated Python (`s3_mounts_pb2*`) and TypeScript (`s3_mounts_pb.ts`) gencode via `buf generate`; no other `.proto` file touched.

## 2. rayd-core domain (`crates/rayd-core/src/s3_mount/`)

- [x] 2.1 `error.rs`: `MountErrorClass` (`network`/`iam_denied`/`not_found`/`not_allowed`/`helper_missing`/`timeout`), unit-tested.
- [x] 2.2 `spec.rs`: `S3Mount`, `parse_allowed_buckets`, `validate_mounts` (allowlist + duplicate-path rejection, both validated before any mount is touched), unit-tested.
- [x] 2.3 `state.rs`: `MountPhase`, `MountState`, unit-tested.
- [x] 2.4 `ports.rs`: `FuseDevice`, `FuseDaemon`.
- [x] 2.5 `lib.rs` registers `pub mod s3_mount;`.

## 3. rayd adapters and feature slot

- [x] 3.1 `adapters/fuse_device.rs` (`LinuxFuseDevice`): raw `libc::mount(2)`/`umount2`, no new Cargo dependency.
- [x] 3.2 `adapters/mount_s3.rs` (`TokioMountS3Daemon`): spawns `mount-s3` as uid/gid 990 with a from-scratch environment, reaps its own child via a dedicated `tokio::spawn(... .wait() ...)` task (design.md D3).
- [x] 3.3 `features/s3_mounts.rs`: real `S3MountsFeature` (`supported()==true`), diffing `apply()` (validate-all-before-touching-anything, unmount removed, mount new/changed, no-op on an unchanged re-apply), `status()`, and a `LifecycleParticipant` (zero `/suspend` share, `on_resume` relaunches a dead daemon via a 2 s `stat` probe). Unit-tested with fakes for both ports.
- [x] 3.4 `grpc/health.rs`: the one call site flips `s3_mounts: true` (per that file's own pre-existing comment inviting this edit).
- [x] 3.5 `grpc/configure.rs`: minimal, mechanical 1-line fix (`request.s3_mounts.clone()?`) made necessary by `S3MountsConfig` no longer being `Copy` now that it carries real fields — no other line of this foundations-owned file touched.
- [x] 3.6 `image/Dockerfile`: `fuse` (AL2023 repo) + `mount-s3` (pinned RPM download + sha256, design.md D5) + the dedicated `rayito-mount` system user (uid/gid 990).

## 4. Gates: Rust (Lima VM `rayito`, user `tester`)

- [x] 4.1 `cargo check -p rayd-core -p rayd` clean.
- [x] 4.2 `cargo test -p rayd-core -p rayd -j 2` green (584 `rayd-core` + 246 `rayd` lib + every integration binary, review-fix round).
- [x] 4.3 `cargo clippy -p rayd-core -p rayd --all-targets -- -D warnings` clean; `cargo fmt` clean.

## 5. Python

- [x] 5.1 `_s3_mounts/{__init__,_domain,_section}.py`: `S3Mount`, `MountStatus`, `plan_s3_mounts`, proto translation, `check_section_result`. Pure except for constructing already-generated proto messages.
- [x] 5.2 `_stacks/components/s3_mounts.py`: real `COMPONENT` (`supported` default `True`), parameters `BucketName`/`Prefixes`/`ReadOnly`.
- [x] 5.3 `infra/s3-mounts.yaml` (`RayitoS3MountAccess`) + `scripts/gen_stack_assets.py` regenerated template into both SDKs.
- [x] 5.4 `tests/unit/test_m15_s3_mounts.py`: domain validation, proto round-trip, `check_section_result` per `SectionCode`, zero-AWS-client assertion.
- [x] 5.5 Fixed the two pre-existing `v06-foundations` tests (`test_m15_stacks_service.py`, `m15-stacks.test.ts`) that used `"s3-mounts"` as their example of a still-`Unsupported` stack component — a direct, unavoidable consequence of this change making that component real; both now use `"efs-volumes"` (still a stub), already the pattern for the analogous `destroy` test in the same files.
- [x] 5.6 `uv run pytest` (full suite, 2581 passed), `ruff check`, `ruff format --check`, `mypy` clean.

## 6. TypeScript

- [x] 6.1 `src/s3-mounts/{domain,section}.ts`: mirrors the Python package.
- [x] 6.2 `src/stacks/components/s3-mounts.ts`: real `COMPONENT`.
- [x] 6.3 `clients/typescript/src/stacks/templates/s3-mounts.gen.ts` regenerated.
- [x] 6.4 `tests/unit/m15-s3-mounts.test.ts`: mirrors the Python test file.
- [x] 6.5 `pnpm lint` (biome), `pnpm typecheck`, `pnpm test` (1145 passed), `pnpm run build` + `pnpm pack:check` clean.

## 7. Docs, ADR, AWS_API_NOTES, MILESTONES, CHANGELOGs

- [x] 7.1 `ARCHITECTURE.md` ADR-017 filled in (replacing the foundations stub).
- [x] 7.2 `AWS_API_NOTES.md` §23 filled in (`mount-s3` distribution, invocation, credentials, IAM shape, image-size estimate).
- [x] 7.3 `MILESTONES.md` M15 "s3-mounts" subsection filled in (replacing the foundations bullet).
- [x] 7.4 `docs/site/docs/funciones-opcionales/montajes-s3.md`: full guide page (replacing the "En construcción" stub), marked with the current `Sandbox.create(mounts=)` integration gap.
- [x] 7.5 Three `CHANGELOG.md` anchors filled in (`clients/python`, `clients/typescript`, `crates/rayd`).
- [x] 7.6 `openspec/changes/m15-s3-mounts/docs-delta.md`: exact replacement rows for `SECURITY.md` (T20), `docs/site/docs/security.md`, `e2b-parity.md` (row 111), `optional-features.md`, `cost.md`, `referencia/errores.md`, `referencia/variables-de-entorno.md` — none of those shared files edited directly.
- [x] 7.7 `mkdocs build -f docs/site/mkdocs.yml --strict` clean (nav entry and stub page already existed from foundations).

## 7a. e2e (not run here; feature-build agents must not touch AWS)

- [x] 7a.1 `clients/python/tests/e2e/test_s3_mounts_e2e.py`: S3M-1..S3M-4, gated on `RAYITO_E2E=1` + `RAYITO_TEMPLATE_CAPS` + `RAYITO_S3_MOUNT_BUCKET`. Review follow-up: no longer self-skips on `UnimplementedError` now that `Sandbox.create(mounts=)` is wired — it runs for real once those env vars are set.
- [x] 7a.2 `clients/typescript/tests/e2e/m15-s3-mounts.e2e.test.ts`: the same four scenarios, mirroring the Python file; same un-skip.
- [x] 7a.3 Verified only by collection (`pytest --collect-only`: 4 deselected; `vitest run`/`tsc --noEmit`: typecheck and collection clean) — never run against AWS from here.

## 10. Review follow-ups applied after the initial PR (this round)

High severity:

- [x] 10.1 **The feature is now reachable.** `_feature_options.plan_features`'s
  `mounts=` branch calls `require_caps_for` then `_s3_mounts.plan_s3_mounts`
  instead of raising unconditionally (mirrored in `feature-options.ts`);
  `create()`/`_open()` (`Sandbox.#open`) execute `FeaturePlan.configure_sections`
  after `Health` (new foundations pieces in `_configure_base.py`/
  `configure-base.ts`: `require_capabilities`, `build_configure_request`,
  `check_configure_response`, `ConfigureSection.check_result`); `S3Mount`/
  `MountStatus` are exported from `rayito`/`rayito/index.ts`; `sbx.mounts`
  (property)/`sbx.mounts()` (async method, async SDKs) read `ConfigureStatus`
  live; the e2e suites no longer self-skip.
- [x] 10.2 `rayd::features::s3_mounts::build` computes `supported()` once
  from real preconditions (the `mount-s3` binary on `PATH`, `/dev/fuse`
  existing, the `rayito-mount` system user existing) instead of a literal
  `true`; `HealthGrpc` now holds the same `Arc<FeatureSet>` `ConfigureGrpc`
  dispatches to and reports `features.s3_mounts.supported()` live, plus
  `RootEgressClass::S3` in `root_egress` when supported.
- [x] 10.3 `apply()` reports `SECTION_CODE_PENDING` immediately and settles
  a mount to `Mounted`/`Failed` in the background (`Inner::run_mount`),
  polling `FuseDevice::probe_ready` (a real `stat` subprocess, bounded,
  killed on timeout) and `FuseDaemon::is_alive`/`exit_class`; `mount-s3`'s
  exit is classified from its exit status and a bounded stderr tail
  (never logged); `mount(2)`'s own `EPERM`/`EACCES` map to `helper_missing`,
  never `iam_denied` (that was never an IAM decision).
- [x] 10.4 Fd/pid ownership rewritten around `Inner::claim`/`finish`: each
  (re)mount attempt is tagged with a generation and only ever writes its
  result if that generation is still current, so a racing `apply()` or
  relaunch tears its own result down instead of double-closing or
  resurrecting a mount the caller moved past; `apply()` always unmounts an
  existing entry before remounting a changed spec.

Medium severity:

- [x] 10.5 `adapters::mount_s3::TokioMountS3Daemon` registers/unregisters
  the `mount-s3` pid in the shared `ChildRegistry` (`FeatureContext` gained
  `child_registry: Arc<ChildRegistry>`).
- [x] 10.6 The background watcher (`Inner::watch_tick`, polled every
  `WATCHER_POLL_INTERVAL`) relaunches a dead daemon with backoff
  (`relaunch_backoff`, doubling, capped); every relaunch re-attaches a
  fresh FUSE descriptor rather than reusing the dead one. `/resume`'s probe
  (`on_resume`) runs as the guest uid via `FuseDevice::probe_ready` (never
  root), with an explicit kill on timeout, and forces the same relaunch
  path when unresponsive.
- [x] 10.7 FUSE mount data gained `allow_other` (so the root-run probe and
  the guest user can both reach the mount) and `mount-s3` gets
  `--uid`/`--gid` from the same guest-id constants `fuse_device.rs` uses,
  so file ownership matches what the kernel already reports.
- [x] 10.8 `rayd_core::mount_path` (shared, not s3-mounts-specific): the
  absolute/canonical/allowed-roots/no-overlap/max-count check the SDK
  already runs, re-run server-side before anything else in
  `s3_mount::spec::validate_mounts`, so a non-SDK or buggy client can never
  steer `mount(2)`/`create_dir_all` outside `/mnt/`/`/home/user/`.

Low severity:

- [x] 10.9 `infra/s3-mounts.yaml`'s `WriteObjects` statement gained
  `s3:AbortMultipartUpload` (regenerated into both SDKs via
  `scripts/gen_stack_assets.py`).
- [x] 10.10 Error-class handling de-duplicated: Python's `_section.py`
  reuses `MOUNT_ERROR_CLASSES`/new `UNKNOWN_ERROR_CLASS` from `_domain.py`
  and compares `configure_pb2.SECTION_CODE_*` ints (never a hand-compared
  string); same in TypeScript (`domain.ts`'s `MOUNT_ERROR_CLASSES`/
  `UNKNOWN_ERROR_CLASS`, `SectionCode` enum). A new `invalid_path` class
  (Rust `MountErrorClass::InvalidPath`, mirrored in both SDKs) reports a
  duplicate/invalid mount path distinctly from `not_allowed`.
- [ ] 10.11 Not done: renaming the e2e test files, and the shared
  `testdata/s3-mounts/mount-specs.json` vectors file. The existing names
  (`test_s3_mounts_e2e.py`, `m15-s3-mounts.e2e.test.ts`) already match this
  repo's real, established pattern for a topic-suffixed e2e file (see
  `test_metadata_index_e2e.py`, `test_secrets_e2e.py`, and every
  `*.e2e.test.ts` in `clients/typescript/tests/e2e/`), so renaming them
  would not actually improve consistency; left as a judgment call for a
  maintainer to override. The shared JSON vectors file was not created —
  genuinely not done, due to time, not a design decision — a reasonable
  follow-up.

## 8. OpenSpec

- [x] 8.1 `proposal.md`, `design.md`, `tasks.md` (this file).
- [x] 8.2 `specs/s3-mounts/spec.md` (new capability, ADDED requirements only).
- [x] 8.3 `npx -y @fission-ai/openspec@1.10.0 validate --strict` passes.
- [ ] 8.4 Not archived (per instructions).

## 9. PR

- [x] 9.1 Branch `feat/m15-s3-mounts`, worktree `rayito-wt-s3-mounts`, from `origin/main` (post-foundations).
- [ ] 9.2 Commits signed (`git commit -s -S`) with the required attribution lines.
- [ ] 9.3 PR opened against `main`; CI green. **Not merged** (serialized AWS acceptance and merge-order decisions are the maintainer's).

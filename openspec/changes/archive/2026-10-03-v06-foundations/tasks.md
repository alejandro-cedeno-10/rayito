## 1. Proto and drop-in registry

- [x] 1.1 `proto/rayito/v1/features.proto` (`AgentFeatures`, `RootEgressClass`).
- [x] 1.2 `proto/rayito/v1/configure.proto` (`ConfigureService`, `ConfigureRequest`/`Response`, `SectionResult`, `ConfigSection`, `SectionCode`, `ConfigureStatusRequest`/`Response`).
- [x] 1.3 Five empty per-feature protos (`s3_mounts`, `efs_volumes`, `lifecycle_events`, `telemetry_export`, `secret_gateway`), each with a header comment naming its owning change.
- [x] 1.4 `health.proto`: `AgentFeatures features = 16`.
- [x] 1.5 `crates/rayito-proto/build.rs` globs `proto/rayito/v1/*.proto` + `vendor/**/*.proto` instead of a hard-coded list.
- [x] 1.6 Regenerated Python (`scripts/gen_python.py`) and TypeScript (`buf generate`) gencode; both produce the same output.

## 2. rayd-core domain

- [x] 2.1 `orphans.rs`: `ZombieEntry`, `orphans_to_reap`, fully unit-tested.
- [x] 2.2 `configure.rs`: `ConfigSection`, `SectionCode`, `SectionOutcome`, `APPLY_ORDER`, `is_configurable` (phase rule).
- [x] 2.3 `features.rs`: `AgentFeatures` (domain mirror), `foundations_only()`.
- [x] 2.4 `root_egress.rs`: `RootEgressClass`.
- [x] 2.5 `credentials.rs`: `CredentialKind`, `CredentialLease`, `needs_refresh` (10-minute margin), `CredentialProvider` port.
- [x] 2.6 `suspend_sync.rs`: `ParticipantDemand`, `SuspendShares::allocate`, `ParticipantReport`, additive to the existing `SuspendBudget` contract.
- [x] 2.7 `lib.rs` registers every new module.

## 3. rayd adapters and gRPC

- [x] 3.1 `adapters/child_registry.rs` (`ChildRegistry`), fully unit-tested.
- [x] 3.2 `adapters/orphan_reaper.rs` (`OrphanReaper`, implements `Reaper`), unit-tested (`/proc/[pid]/stat` parsing, PID-1 guard). **Not added to `main.rs`'s `reapers` vec** (design.md E2).
- [x] 3.3 `adapters/credential_broker.rs` (`ImdsCredentialBroker`, `GuestCredentials`, `PushedCredentials`), unit-tested.
- [x] 3.4 `features/{mod.rs,slot.rs}` (`ConfigurableFeature`, `Unsupported`, `FeatureSet`, `FeatureContext`, `build()`).
- [x] 3.5 Six per-feature stub files (`features/{s3_mounts,efs_volumes,lifecycle_events,telemetry_export,secret_gateway,template_start}.rs`), each `Arc::new(Unsupported)`.
- [x] 3.6 `grpc/configure.rs` (`ConfigureGrpc`): dispatches present sections in `APPLY_ORDER`, phase-gated, never logs the request; registered in `grpc/mod.rs`'s router (design.md E1: builds its own `FeatureSet`, no `Services` field yet).
- [x] 3.7 `grpc/health.rs`: populates `HealthResponse.features` from `AgentFeatures::foundations_only()`.
- [x] 3.8 `lifecycle/participants.rs` (`LifecycleParticipant`, `ReadyVerdict`); wired into `hooks/mod.rs`'s `/suspend` (concurrent with `BoundedFlush::flush` via `SuspendShares`) and `/ready` (`participants_ready` combined with the existing decision). `HookServices.participants` defaults to empty everywhere; behavior with no participant is unchanged from 0.5.x (proven by the existing hook test suite passing unmodified).
- [x] 3.9 `Cargo.toml`: added `glob` (build-dep of `rayito-proto`), `thiserror`/`zeroize` (deps of `rayd`), all exact-pinned.

## 4. Gates: Rust

- [x] 4.1 `cargo fmt --all --check` clean.
- [x] 4.2 `cargo clippy --workspace --all-targets --locked -- -D warnings` clean (pedantic).
- [x] 4.3 `cargo test --workspace --locked` green (787 `rayd-core` + `rayd` unit tests, all integration suites, 0 failures) inside the Lima VM as `tester`.

## 5. Python: shared domain and ports

- [x] 5.1 `_role_policy.py`: `resolve_image_variant`, `require_caps_for`.
- [x] 5.2 `_mount_path.py`: `validate_mount_paths`, `MAX_MOUNTS`.
- [x] 5.3 `_feature_options.py`: `FeatureOptions`, `FeaturePlan`, `plan_features` (raises `UnimplementedError` per option while every feature is a stub).
- [x] 5.4 `_configure_base.py`: `AgentFeatures`, `agent_features_from_health`, `require_configure_support`, `ConfigureSection` protocol, `section_error`.
- [x] 5.5 `sandbox_sync/configure.py` / `sandbox_async/configure.py`: `call_configure`/`call_configure_status` adapters (no consumer yet; unit-tested against a fake stub).

## 6. Python: `OptionalStack`

- [x] 6.1 `_stacks/_model.py` (pure): `StackParameter`, `StackArtifact`, `CostStatement`, `StackComponent`, `StackStatus`, `DeployPlan`, `plan_deploy`, `stack_tags`.
- [x] 6.2 `_stacks/_port.py`: `StackProvisioner` protocol.
- [x] 6.3 `_stacks/_cloudformation.py`: `CloudFormationProvisioner`, verified against `AWS_API_NOTES.md` §21 and botocore 1.43.103.
- [x] 6.4 `_stacks/_packaging.py`: `load_template`/`load_artifact`/`artifact_key` over `importlib.resources`.
- [x] 6.5 `_stacks/_service.py` (`OptionalStacks`) and `_stacks/_service_async.py` (`AsyncOptionalStacks`).
- [x] 6.6 `_stacks/_registry.py`: static catalog of nine components; `_stacks/components/{metadata_index,secrets_access}.py` real, seven stubs (`supported=False`).
- [x] 6.7 `scripts/gen_stack_assets.py` (+ `--check`): renders `infra/*.yaml` (minus the three pre-`OptionalStack` templates) into both SDKs deterministically.
- [x] 6.8 `cli/stack.py` (`list`/`deploy`/`status`/`destroy`), registered in `cli/app.py`; stub sub-apps `cli/{events,template,domain}.py` registered alongside.
- [x] 6.9 `tests/unit/fake_stacks.py` (shared fake `StackProvisioner`).

## 7. Python: seams and exceptions

- [x] 7.1 Ten new exception classes in `exceptions.py` (`MountException`, `VolumeException`, `VolumeNotFoundException`, `VolumePathNotFoundException`, `BuildException`, `TemplateException`, `StackException`, `WebhookException`, `GatewayException`, `CustomDomainException`); `FileUploadException` left alone (already exists as a `TransferException` subclass — a naming note for `m15-templates`, not a code conflict in Python).
- [x] 7.2 Seven new `Sandbox.create()` kwargs (sync + async): `mounts`, `volumes`, `size`, `events`, `telemetry`, `gateways`, `domain`; `plan_features` called before `resolve_control_plane` (no AWS call when any is set but unsupported); `_pool_base.LaunchKwargDefaults` extended so `pool=` + any of them is `InvalidArgumentException`.
- [x] 7.3 `rayito/__init__.py`: exports `OptionalStacks`, `AsyncOptionalStacks`, `StackComponent`, `StackStatus`, `StackParameter`, `StackArtifact`, `CostStatement`, and the ten new exceptions.
- [x] 7.4 `limits.json` + `scripts/gen_limits.py`: `supportedMemoryMiB`, `guestMemoryMultiplier`, `reservedPorts` (fixed the generator's list rendering, which only supported string lists before this change).
- [x] 7.5 `cli/_compat.py`: `0.6` row.

## 8. Gates: Python

- [x] 8.1 `uv run pytest tests/unit` green (2500+ tests, including the new `test_m15_*.py` files and the zero-cost golden test).
- [x] 8.2 `uv run pytest ../../scripts/tests` green.
- [x] 8.3 `uv run ruff check .` / `uv run ruff format --check .` clean.
- [x] 8.4 `uv run mypy src tests` clean (strict).
- [x] 8.5 `uv build` + `check_wheel.py` (new `_stacks` package and its data files packaged correctly).

## 9. TypeScript: mirrors

- [x] 9.1 `role-policy.ts`, `mount-path.ts`, `feature-options.ts` (mirrors of 5.1-5.3; no `configure.ts` RPC adapter yet — no consumer, deferred alongside the Python one's actual use).
- [x] 9.2 `stacks/{model,port,cloudformation,packaging,service,registry}.ts`; `stacks/components/*.ts` (two real, seven stubs); `stacks/templates/{metadata-index,secrets-access}.gen.ts` (generated).
- [x] 9.3 `errors.ts`: ten new classes; `BuildError`/`TemplateError` defined but not exported from `index.ts` (design.md E4).
- [x] 9.4 `index.ts`: exports the new `OptionalStacks` surface and eight of the ten new error classes.
- [x] 9.5 `pool/core.ts`: `POOL_REJECTED_OPTIONS` extended with the seven new names.
- [x] 9.6 `sandbox/sandbox.ts`: seven new `SandboxCreateOptions` fields; `planFeatures` called before `resolveControlPlane`.
- [x] 9.7 `package.json`: `@aws-sdk/client-cloudformation` as optional peerDependency + devDependency.
- [x] 9.8 `tests/unit/m15-fake-stacks.ts` (shared fake `StackProvisioner`).

## 10. Gates: TypeScript

- [x] 10.1 `pnpm lint` (biome) clean.
- [x] 10.2 `pnpm typecheck` clean (`exactOptionalPropertyTypes` respected).
- [x] 10.3 `pnpm test` green (1128 tests, including the new `m15-*.test.ts` files; fixed a pre-existing `limits.test.ts` name-override gap the same way as its Python counterpart).
- [x] 10.4 `pnpm build` (tsdown) succeeds.
- [x] 10.5 `pnpm pack:check` (pack contents + `check-dts-cost-blocks.mjs`) clean.

## 11. Docs

- [x] 11.1 `ARCHITECTURE.md`: ADR-015, ADR-016 (full), ADR-017..024 (one-line stubs naming their owning change).
- [x] 11.2 `AWS_API_NOTES.md`: §21 (CloudFormation, full contract), §22-29 (one-line stubs).
- [x] 11.3 `MILESTONES.md`: `## M15 — Rayito 0.6` section (foundations subsection filled, eight feature subsections stubbed).
- [x] 11.4 Three `CHANGELOG.md` (`clients/python`, `clients/typescript`, `crates/rayd`): foundations entry under `[Unreleased]` → `Added`, plus eight `<!-- m15-<slug> -->` anchors each.
- [x] 11.5 `docs/RELEASE_NOTES_0.6.0.md`: one stub section per feature plus a filled foundations section.
- [x] 11.6 Nine `docs/site/docs/funciones-opcionales/*.md` pages wired into `mkdocs.yml` nav: `pilas-opcionales.md` is a real page for the shipped `rayito stack`/`OptionalStacks`; the other eight are `En construcción (0.6)` stubs.
- [x] 11.7 `docs/site/docs/limits.md`: `0.6` compatibility row (kept identical to `cli/_compat.py` by the existing `test_docs_table_matches_code` test).
- [x] 11.8 `referencia/python/<slug>.md` stubs and their nav entries: **not done** in this change — deferred (no existing test or doc-integration step depends on them yet; tracked in `proposal.md`'s follow-ups).

## 12. Gates: docs

- [x] 12.1 `mkdocs build --strict` clean.
- [x] 12.2 `scripts/check_docs_examples.py` (Python and TypeScript) clean on every new code example.
- [x] 12.3 `scripts/tests/test_optional_features_docs.py` still green (no new "disponible" row; ADR-014 assertions untouched).
- [x] 12.4 `scripts/check_hygiene.py`, `scripts/check_pins.py`, `scripts/check_license.py` clean.

## 13. OpenSpec

- [x] 13.1 `openspec/changes/v06-foundations/{proposal,tasks,design}.md`.
- [x] 13.2 `specs/sandbox-configure/spec.md` (ADDED requirements).
- [x] 13.3 `specs/optional-stacks/spec.md` (ADDED requirements).
- [x] 13.4 `npx -y @fission-ai/openspec@1.10.0 validate v06-foundations --strict` passes.

## 14. PR

- [x] 14.1 Branch `feat/v06-foundations`, worktree `rayito-wt-v06-foundations`, commits signed (`git commit -s -S`) with the required attribution lines.
- [x] 14.2 PR opened against `main`; CI green; merged with a merge commit (`gh pr merge --merge`). (Verified at archive time: merged with a merge commit, all checks green; released in 0.6.0.)

## 15. Follow-up: one shared `FeatureSet` and bounded participants

Shared wiring every 0.6 feature needs, moved here instead of each feature PR
editing `main.rs`/`grpc`/`hooks` on its own (review of PR #79).

- [x] 15.1 `FeatureSet::agent_features()` (derived from each slot's `supported()`) and `FeatureSet::participants()`.
- [x] 15.2 `grpc::router_with_features(services, settings, transfers, Arc<FeatureSet>)`; `router_with_transfers` builds its own set and delegates. `HealthGrpc::with_features` reports `agent_features()`.
- [x] 15.3 `main.rs` and `crates/rayd/tests/common/mod.rs` build one `Arc<FeatureSet>` and give it to the gRPC router and its `participants()` to `HookServices`.
- [x] 15.4 `hooks::run_concurrently`: one loop for `/suspend`, `/resume` and `/terminate`, each participant under its own cap (`SuspendShares`, `PARTICIPANT_RESUME_TIMEOUT`, `PARTICIPANT_TERMINATE_TIMEOUT`); `/resume`'s participants run concurrently with the kernel probe.
- [x] 15.5 Participants run only for an accepted transition (`Transition::changed`): a repeated `/suspend` or `/terminate` does not run them again.
- [x] 15.6 Workspace pin `hmac = "=0.13.0"` (first user: `m15-events-webhooks`'s event-line MAC).
- [x] 15.7 `plan_features(..., logging=)` / `planFeatures(options, imageVariant, logging)`: `create()` passes its `logging` option through the seam (unused here; `events=` needs a CloudWatch-enabled `logging`).
- [x] 15.8 Unit tests: all-`Unsupported` set reports `foundations_only()` and no participants; participants run once per accepted transition; a hung `on_resume`/`on_terminate` is cut at its cap.


## Why

Rayito 0.6 (milestone M15) adds eight optional, cost-incurring features
(s3-mounts, efs-volumes, sizes-catalog, events-webhooks, rayd-otlp,
templates, secrets-gateway, custom-domain), built in parallel by separate
agents after this change merges. Letting each one invent its own
per-sandbox configuration RPC, its own CloudFormation deployment flow, its
own zombie-reaping fix and its own compatibility row would duplicate work,
risk eight incompatible conventions and guarantee merge conflicts on the
shared files every feature needs to touch (proto services, SDK `create()`
signatures, exception hierarchies, exports, doc navigation, CHANGELOGs).

This change builds the shared foundation once: a single per-sandbox
configuration channel (`ConfigureSandbox`, ADR-015), a single convention for
optional customer-account infrastructure (`OptionalStack`, ADR-016), the
credential broker and role-policy gate every caps-only feature needs, the
PID-1 zombie reaper the research flagged (Q80 of
`docs/research/2026-10-e2b-out-of-scope.md`), the 0.6 compatibility row, and
every pre-created seam (proto stubs, SDK kwargs, exception classes, exports,
stack registry slots, nav entries, ADR/AWS_API_NOTES/MILESTONES/CHANGELOG
sections) that the eight feature changes will fill in without touching a
shared file more than once.

## What Changes

- **`ConfigureSandbox` (ADR-015).** New `ConfigureService` (`Configure`,
  `ConfigureStatus`) with one optional section per feature inside
  `ConfigureRequest`: an absent section is untouched, a present one
  (including empty) replaces the feature's state. Fixed apply order
  (events → telemetry → gateway → s3_mounts → efs_volumes). Accepted only
  in `RUNNING`/`RESUMED` (`FAILED_PRECONDITION not_running` otherwise). The
  agent side is a `FeatureSet` of six `ConfigurableFeature` slots
  (`rayd::features`), all `Unsupported` in this change; `Health.features`
  (`AgentFeatures`, proto field 16) reports which build supports what, so
  the SDK tells "pre-0.6 agent" (field absent) from "0.6 agent, feature
  still a stub" (field present, flag `false`).
- **`LifecycleParticipant` and bounded `/suspend` reuse.** A feature that
  needs to flush something on `/suspend` or gate `/ready` implements
  `LifecycleParticipant` (`rayd::lifecycle::participants`); its
  `on_suspend(share)` runs concurrently with the existing per-filesystem
  `syncfs` inside the same `SuspendBudget` (`SuspendShares`, additive to
  `rayd_core::suspend_sync`, never changing its existing contract).
- **`OptionalStack` convention (ADR-016).** A `StackProvisioner` port, a
  `CloudFormationProvisioner` adapter (verified against
  `AWS_API_NOTES.md` §21), an `OptionalStacks` service
  (`deploy`/`status`/`destroy`/`components`, never invoked implicitly) and
  a `rayito stack` CLI. A static catalog of nine components:
  `metadata-index` and `secrets-access` (M14/M13a, migrated to this
  convention with no template change) are real; the other seven are stubs
  (`supported: false`) that raise `UnimplementedError` before touching AWS,
  one per upcoming feature change.
- **PID-1 zombie reaping (Q80).** Pure domain (`rayd_core::orphans`) and a
  tested adapter (`ChildRegistry`, `OrphanReaper`, implementing the
  existing `Reaper` trait) that reaps zombies re-parented to `rayd`'s own
  pid without ever racing tokio's own `Child::wait` for pids `rayd` spawned
  directly. **Not yet wired into `main.rs`**: activating it safely needs
  `ChildRegistry` registration inside `process_spawner`, `pty_backend` and
  `sidecar_process`, a change to three adapters shared by the whole process
  lifecycle that this change defers rather than rush — see "Non-blocking
  follow-ups" below.
- **Credential broker and role policy.** `rayd_core::credentials`
  (`CredentialLease`, `needs_refresh`, 10-minute margin) and
  `rayd::adapters::credential_broker` (one shared `ImdsCredentialsProvider`
  instance, a `PushedCredentials` holder for `ConfigureSandbox`-delivered
  values); `_role_policy.py` / `role-policy.ts`
  (`require_caps_for`/`requireCapsFor`) for features that only work on
  `rayito-base-caps`.
- **SDK seams.** Seven new `Sandbox.create()` kwargs (`mounts`, `volumes`,
  `size`, `events`, `telemetry`, `gateways`, `domain`) that raise
  `UnimplementedError` naming the owning change while every feature stays a
  stub (`_feature_options.py`/`feature-options.ts`), rejected together with
  `pool=`/`pool` by the existing mechanism; `_mount_path.py`/`mount-path.ts`
  (shared validation for `mounts=`/`volumes=`); ten new exception classes
  pre-created in both SDKs; `limits.json` seeds for sizes-catalog
  (`supportedMemoryMiB`, `guestMemoryMultiplier`) and custom-domain
  (`reservedPorts`); nine documentation stub pages (one per feature) wired
  into `mkdocs.yml`'s nav; stub ADRs (ADR-017..024), `AWS_API_NOTES.md`
  stub sections (§22-29), a `## M15` section in `MILESTONES.md`, and one
  CHANGELOG anchor per feature in all three CHANGELOGs.
- **Compatibility.** A `0.6` row in
  `clients/python/src/rayito/cli/_compat.py` and
  `docs/site/docs/limits.md`.
- **Drop-in registries.** `crates/rayito-proto/build.rs` now globs
  `proto/rayito/v1/*.proto` (plus a `vendor/` tree for future vendored
  protos) instead of a hard-coded file list, so a feature's own `.proto`
  file needs no edit here.
- **Zero-cost golden tests.** A captured trace of every boto3/AWS-SDK-v3
  operation and gRPC method of a scripted `create → commands.run →
  files.write → pause → resume → commands.run → kill → list` session
  (`tests/unit/fixtures/zero_cost_0_5_trace.json`, both SDKs), asserted
  byte-for-byte with none of the seven new options set.

## Non-blocking follow-ups (explicitly deferred, tracked here for the next agent/maintainer)

- **Zombie reaper activation.** Register `ChildRegistry::register`/
  `unregister` around the single `spawn`/`wait` pair in
  `crates/rayd/src/adapters/{process_spawner,pty_backend,sidecar_process}.rs`,
  then add `OrphanReaper` to `main.rs`'s `reapers` vec. Deferred because it
  touches three adapters shared by the whole process/PTY/sidecar lifecycle;
  doing it carefully needs its own focused review rather than riding along
  with sixteen other new files.
- **`S3ObjectStore` credential-broker sharing.** `adapters::s3_store` still
  builds its own `ImdsCredentialsProvider` instance (ADR-009, unchanged).
  Collapsing it with `ImdsCredentialBroker` is a refactor for whichever
  feature first needs the shared instance (s3-mounts, efs-volumes or
  rayd-otlp with role auth).
- **`_images.py` extraction and `e2b/_volume.py`/`e2b/_template.py` moves**
  (architecture §1(g)) are deferred to `m15-sizes-catalog` and
  `m15-efs-volumes`/`m15-templates` respectively: both are behavior-
  preserving refactors with no consumer yet in this change, and bundling
  them here would only add review surface without changing what ships.
- **TS `TemplateError`/`BuildError` naming collision.** `src/e2b/errors.ts`
  already declares `TemplateError`/`BuildError` stand-ins (never thrown,
  different shape: the shim's `BuildError` is a plain `Error`, not a
  `SandboxError`). The new top-level `errors.ts` classes of the same name
  are defined but **not exported from `index.ts`** to avoid the collision;
  `m15-templates` decides whether to retire the shim stand-ins (the
  `NotEnoughSpaceError`/`FileUploadError` alias pattern) or rename one pair.
  Python has no equivalent export-identity test, so
  `rayito.TemplateException`/`BuildException` coexist with
  `rayito.e2b.exceptions.TemplateException`/`BuildException` without
  conflict; still worth the same decision for consistency.
- **Drop-in registries left untouched.**
  `clients/typescript/scripts/check-dts-cost-blocks.mjs`'s
  `COST_DECLARATIONS` array and
  `scripts/tests/test_optional_features_docs.py`'s `FUNCTION_ANCHORS`
  tuple stay hard-coded in this change (no new "disponible" row is added,
  so nothing requires touching them yet); converting them to glob a
  `cost-declarations/`/`optional_features.d/` directory, as the M15
  architecture plan names, is left for `m15-docs-integration` or whichever
  feature first adds a "disponible" row.
- **D1-D4 (needs the maintainer, drafted in `design.md`, not applied):**
  SPEC.md §4 non-goals amendments, ADR-014 rule 2 wording, the
  custom-domain acceptance domain/certificate, and the efs-volumes
  measurement budget.

## Impact

- **Rust**: `crates/rayito-proto/{build.rs, vendor/}`, new protos
  (`features.proto`, `configure.proto`, five empty per-feature protos),
  `health.proto` (+field 16); `rayd-core` (`orphans.rs`, `configure.rs`,
  `features.rs`, `root_egress.rs`, `credentials.rs`, `suspend_sync.rs`
  additions); `rayd` (`features/`, `grpc/configure.rs`,
  `lifecycle/participants.rs`, `adapters/{child_registry,orphan_reaper,
  credential_broker}.rs`, `hooks/mod.rs`, `grpc/mod.rs`, `grpc/health.rs`).
- **Python**: `_role_policy.py`, `_feature_options.py`, `_mount_path.py`,
  `_configure_base.py`, `sandbox_{sync,async}/configure.py`, `_stacks/`
  (new package), `cli/{stack,events,template,domain}.py`, `cli/app.py`,
  `exceptions.py`, `__init__.py`, `sandbox_{sync,async}/main.py`,
  `_pool_base.py`, `cli/_compat.py`, `limits.json`,
  `scripts/gen_stack_assets.py` (new), `scripts/gen_limits.py`.
- **TypeScript**: `role-policy.ts`, `feature-options.ts`, `mount-path.ts`,
  `stacks/` (new directory), `errors.ts`, `index.ts`, `pool/core.ts`,
  `sandbox/sandbox.ts`, `package.json` (new optional peer
  `@aws-sdk/client-cloudformation`).
- **Docs**: `ARCHITECTURE.md` (ADR-015..024), `AWS_API_NOTES.md` (§21-29),
  `MILESTONES.md` (## M15), three `CHANGELOG.md`, `docs/RELEASE_NOTES_0.6.0.md`,
  `docs/site/mkdocs.yml`, nine `docs/site/docs/funciones-opcionales/*.md`,
  `docs/site/docs/limits.md`.
- **Infra**: none new (both components this change implements for real
  reuse existing `infra/metadata-index.yaml` and `infra/secrets-access.yaml`
  unchanged).
- **No changes** to any 0.5.x public behaviour: every new kwarg/option
  defaults to `None`/`undefined` and the zero-cost golden tests prove the
  default path is byte-for-byte unchanged.

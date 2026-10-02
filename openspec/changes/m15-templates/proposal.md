## Why

Rayito 0.6 (M15) lets customers build their sandbox image declaratively
instead of hand-writing a Dockerfile and running `rayito image publish`
(research §3, `docs/research/2026-10-e2b-out-of-scope.md`: row 56
`E2B(...).Template`, row 81 the build API, row 89 `TemplateException`/
`BuildException`, row 112 the CLI `template` sub-app). This is the single
highest-value divergence for anyone migrating an existing E2B codebase,
whose `Template()` builder already looks exactly like this one (E2B v2
DSL, verified against `e2b/template/main.py`).

TPL-1 (Q83) and TPL-2 (Q84) settled the two open design questions before
this change started: the image log group receives the complete BuildKit
output (`#N [k/n] RUN …`, exit codes) for both a correct and a failing
build, so the SDK can explain a failure without CodeBuild or live log
streaming; and `codeArtifact.uri` only ever accepts an `s3://` URI, so
`from_base_image()` always composes on top of an existing zip in S3, never
an ECR reference. Option A of the research (compiler in the SDK, no
control plane of its own) is what this change builds.

## What Changes

- **DSL** (`Template`/`AsyncTemplate`, both SDKs): a fluent, immutable
  builder compiling to the five wire instructions E2B itself uses (`COPY`,
  `ENV`, `RUN`, `WORKDIR`, `USER`): `fromBaseImage()` (the only supported
  base in 0.6: an already-published `rayito-base`/`rayito-base-caps`),
  `copy`, `runCmd`/`pipInstall`, `setEnvs`, `workdir`, `setUser`,
  `setStartCmd` (bakes `/etc/rayito/template.json`, `rayito.template/1`),
  `skipCache` (forces a rebuild; 0.6 has no layer cache), `toDockerfile`/
  `toJSON`. `fromImage`/`fromTemplate`/`fromDockerfile`/`fromGcpRegistry`/
  `aptInstall` raise `UnimplementedError`, documented divergences (ADR-022):
  composing on top of an external image has no supported path yet (`rayd`
  and its hooks only exist inside an already-published Rayito image), and
  `rayito-base` is Amazon Linux 2023 (`dnf`, not `apt`).
- **Build pipeline**: resolve the base image's latest `ACTIVE`/
  `SUCCESSFUL` version (or a pinned one) -> fetch its `codeArtifact` zip
  (`s3:GetObject`) -> compile the new Dockerfile on top of it, inserting
  the new layer right before the base's own `CMD`/`ENTRYPOINT` and
  repeating that instruction afterward so `rayd` stays PID 1 -> assemble a
  deterministic zip (every base entry, the composed Dockerfile, the
  caller's context files, `template.json` if `setStartCmd` was called) ->
  upload it to S3 under a sha256 key (skipped if it already exists) ->
  `create`/`update-microvm-image` (reusing an existing version with an
  identical configuration unless `force`) -> poll the three-state gate ->
  on any non-`SUCCESSFUL`/`ACTIVE` outcome, re-read the image log group and
  turn BuildKit's `#N [k/n] RUN …`/`exit code: N` into
  `BuildException(step, command, exitCode, logTail)`, or a `stateReason`
  naming a 4xx/5xx `ready_cmd` failure into
  `BuildException(reason="ready_client_error"|"ready_server_error")`
  (TPL-5/Q85). An in-process `MAX_CONCURRENT_BUILDS = 10` guard (Q83)
  rejects an 11th concurrent build before calling AWS.
- **Public API**: `Template.build(template, name, {bucket, memoryMb,
  force, timeout, baseImageVersion, onBuildLogs, ...}) -> BuildInfo`,
  `build_in_background`/`buildInBackground` -> `BuildHandle`,
  `get_build_status`/`getBuildStatus`, `exists`/`templateExists`.
  `wait_for_port`/`wait_for_url`/`wait_for_process`/`wait_for_file`
  (`ReadyCommand`, with `.timeout()`) compile to the shell probe `rayd`
  runs. Nothing here is on the `Sandbox.create()` options list: the result
  of a build is just an image name/ARN, launched exactly like any other
  image.
- **E2B shim**: `rayito.e2b.Template`/`AsyncTemplate` (`e2b/_template.py`,
  `e2b/template.ts`) are thin subtypes of the native classes that inherit
  the DSL and adapt E2B's build signature (`alias`, `cpu_count`/`cpuCount`
  with a `RayitoCompatWarning`, `memory_mb`/`memoryMB` rounded up to a
  supported size with a warning, `skip_cache`/`skipCache` as `force`;
  `E2B(region, session, bucket)` binds `client.Template` like
  `client.Secret`), adding the four tag operations
  (`aliasExists`/`assignTags`/`removeTags`/`getTags`) E2B has and
  `create`/`update-microvm-image` cannot give (no per-version tags), which
  raise `UnimplementedError`. `rayito.e2b.Template`/`AsyncTemplate` (the
  "no templates API" stand-ins in `_unimplemented.py`/`unimplemented.ts`)
  are retired; `rayito.e2b.{BuildException,TemplateException}` (Python)
  and `rayito/e2b`'s `BuildError`/`TemplateError` (TS, the open item
  foundations left in `src/errors.ts`) become aliases of the native
  exception classes instead of the stand-ins that never threw, the same
  pattern already used for `NotEnoughSpaceException`/`FileUploadException`.
- **Infra** (`infra/templates.yaml`, OptionalStack `templates`): a single
  managed policy, `RayitoTemplateBuilder`
  (`UpdateMicrovmImage`/`GetMicrovmImage*`/`ListMicrovmImageVersions` on
  this account's `microvm-image:*`, `CreateMicrovmImage` on `*` — the
  only resource AWS authorizes it on, Q114 —, an explicit `Deny` on
  updating the published base images (`ProtectedImageNamePrefix`),
  `lambda:PassNetworkConnector` on the AWS managed connectors,
  `iam:PassRole` on the build role, S3
  access to the artifact bucket and the base image's bucket — never `*` —
  and read-only CloudWatch Logs access) for whoever calls
  `Template.build()`. No bucket, no image, no Lambda function: $0 at rest.
  Deviation from the plan, recorded in ADR-022: the stack does not create
  an encrypted artifact bucket with lifecycle expiry; the bucket stays the
  caller's, and the docs ask for a lifecycle rule on `rayito/templates/`.
- **Shared image-build core**: `rayito/_images.py` and
  `src/images/gateway.ts` (architecture §1(g)): the
  `create`/`update-microvm-image` submission, the three-state gate, the
  configuration reuse check (Q52 normalisation) and the content-addressed
  upload, extracted from `cli/_publish.py` with no behaviour change and
  consumed by both `rayito image publish` and `Template.build()`.
- **Docs**: `docs/site/docs/funciones-opcionales/templates.md` (filled in,
  replacing the stub), `docs-delta.md` with the exact replacement rows for
  `e2b-parity.md` (56, 81, 89, 112), `optional-features.md`,
  `cost.md` and `security.md` (T26, template supply chain), applied later
  by `m15-docs-integration`.

## Non-blocking follow-ups (tracked here for the next agent/maintainer)

- **Shared-file registries.** `check-dts-cost-blocks.mjs` now reads a
  drop-in registry, `clients/typescript/scripts/cost-declarations/*.json`,
  and templates registers its `class Template` declaration through its own
  `templates.json` instead of a line in the shared `COST_DECLARATIONS`
  list; sibling features can do the same without touching the script. The
  remaining foundations-owned edits are one line or one entry each:
  `crates/rayd-core/src/lib.rs` (`pub mod template;`),
  `clients/typescript/package.json`/`pnpm-lock.yaml` (the optional
  `@aws-sdk/client-cloudwatch-logs` peer) and
  `clients/typescript/src/stacks/packaging.ts` (one generated-asset
  entry); the PR body lists them for merge-order coordination. An
  `optional_features.d/*.json` registry has no consumer yet (the
  optional-features row ships through `docs-delta.md`); designing its
  generator belongs to foundations.
- **`cli/_publish.py` ownership.** The image-build core extraction touches
  `cli/_publish.py`, which `m15-sizes-catalog` also edits; the extraction
  is behaviour-preserving (the CLI tests pass unchanged except for two
  imports moved to `rayito._images`) and should be merged before, or
  rebased under, sizes-catalog.
- **Compatibility row.** `Health.features.template_start` is now `true`:
  foundations owns the 0.6 row of `rayito.cli._compat.COMPATIBILITY` and
  `docs/site/docs/limits.md`.
- **Disk-space cross-cutting risk.** The shared Lima VM's 58 GB disk keeps
  filling with the sibling M15 branches' `CARGO_TARGET_DIR`s (it hit 0
  bytes free twice during the review fixes); either a larger disk, a
  shared `CARGO_TARGET_DIR` with `sccache`, or pruning finished branches'
  target dirs between rounds.

## Impact

- **Rust**: `crates/rayd-core/src/template.rs` (new, pure domain:
  `StartSpec`, `ReadyPoll`, `ready_decision`), `crates/rayd-core/src/lib.rs`
  (`pub mod template;`); `crates/rayd/src/adapters/{fs_template_spec,
  shell_ready_probe}.rs`, `crates/rayd/src/features/template_start.rs`
  (the real slot and its `TemplateParticipant`), `hooks/mod.rs` (calls
  each participant's `on_run` after `/run`; `ReadyVerdict::Fail` answers
  500), `lifecycle/participants.rs` (default no-op `on_boot`),
  `process/manager.rs` (`start_at_boot`), `main.rs` (calls every
  participant's `on_boot` and builds the process's one `FeatureSet` with
  the real `ProcessManager`); `Health.features.template_start` comes from
  the slot's own `supported()` (`FeatureSet::agent_features`).
  No proto changes (templates has no `ConfigureSandbox` section; start/
  ready happens at boot from a file, not a per-sandbox RPC).
- **Python**: new package `clients/python/src/rayito/_templates/`
  (`_instructions.py`, `_ready_cmds.py`, `_dsl.py`, `_dockerfile.py`,
  `_context.py`, `_artifact.py`, `_concurrency.py`, `_logs.py`, `_build.py`,
  `_build_async.py`, `_models.py`), `e2b/_template.py` (new),
  `e2b/_unimplemented.py` (removes the `Template`/`AsyncTemplate` stand-ins
  and the `Template` D14 entry, adds the four tag-method entries),
  `e2b/exceptions.py` (`BuildException`/`TemplateException` become aliases
  of the native classes), `e2b/__init__.py` and `__init__.py` (exports),
  `_stacks/components/templates.py` (fills the stub), `_limits.py`
  (reused, not changed).
- **TypeScript**: new directory `clients/typescript/src/templates/`
  (`instructions.ts`, `ready-cmds.ts`, `dsl.ts`, `dockerfile.ts`,
  `context.ts`, `context-node.ts`, `zip-node.ts`, `artifact.ts`, `logs.ts`,
  `concurrency.ts`, `build.ts`), `e2b/template.ts` (new), `e2b/resources.ts`
  (`Template` moves out), `e2b/unimplemented.ts` (same table change as
  Python), `e2b/client.ts` (`client.Template` returns the real class),
  `e2b/index.ts` and `index.ts` (exports; `BuildError`/`TemplateError`
  promoted from stand-ins to native aliases), `e2b/errors.ts` (deleted: no
  longer needed), `stacks/components/templates.ts` (fills the stub),
  `stacks/packaging.ts` (registers the `templates` generated asset),
  `package.json`/`pnpm-lock.yaml` (`@aws-sdk/client-cloudwatch-logs` added
  as an optional peer + devDependency, lazily loaded only when a build
  fails), `scripts/check-dts-cost-blocks.mjs` (`class Template` entry).
- **Infra**: `infra/templates.yaml` (new), regenerated
  `_stacks/_templates/templates.yaml` / `stacks/templates/templates.gen.ts`
  via `scripts/gen_stack_assets.py`.
- **Docs**: `docs/site/docs/funciones-opcionales/templates.md` filled in;
  `openspec/changes/m15-templates/docs-delta.md` for
  `m15-docs-integration` to apply to the shared tables later.
- **No changes** to any 0.5.x or already-shipped 0.6-foundations
  behaviour: no `Sandbox.create()` kwarg changes, no new import-time AWS
  client, no new RPC. The zero-cost golden tests (`test_m15_zero_cost.py`,
  `zero-cost-defaults.test.ts`) are untouched and still pass.

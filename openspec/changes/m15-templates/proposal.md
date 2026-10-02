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
  `e2b/template.ts`) are thin subtypes of the native classes — the Python
  and JS E2B APIs already name their build methods the same way Rayito
  does — adding only the four tag operations
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
  (`CreateMicrovmImage`/`UpdateMicrovmImage`/`GetMicrovmImage*`/
  `ListMicrovmImageVersions`/`ListMicrovmImageBuilds`/
  `GetMicrovmImageBuild`, `iam:PassRole` on the build role, S3 access to
  the artifact bucket, and read-only CloudWatch Logs access) for whoever
  calls `Template.build()`. No bucket, no image, no Lambda function: $0 at
  rest.
- **Docs**: `docs/site/docs/funciones-opcionales/templates.md` (filled in,
  replacing the stub), `docs-delta.md` with the exact replacement rows for
  `e2b-parity.md` (56, 81, 89, 112), `optional-features.md`,
  `cost.md` and `security.md` (T26, template supply chain), applied later
  by `m15-docs-integration`.

## Non-blocking follow-ups (explicitly deferred, tracked here for the next agent/maintainer)

- **`rayd`-side start/ready cmd execution is not wired in this change.**
  `rayd_core::template` (`StartSpec`, `ready_decision`) is complete and
  unit-tested, matching the schema `_artifact.start_spec_to_json`/
  `startSpecToJson` write. The adapter side (`adapters/fs_template_spec.rs`
  reading `/etc/rayito/template.json` once at boot, `adapters/
  shell_ready_probe.rs` running `ready_cmd`, a `TemplateParticipant`
  plugging that into `LifecycleParticipant::ready_gate` — the seam
  `features/template_start.rs`'s stub docstring already names as "a
  template's `ready_cmd` is a natural `LifecycleParticipant::ready_gate`"
  — and actually spawning `start_cmd` as a managed, `commands.list`-visible
  process) is deferred: wiring a managed, supervised `start_cmd` needs the
  same `ProcessSpawner`/`ChildRegistry` integration foundations' own
  orphan-reaper deferral already flagged as its own focused piece of work
  (touching shared process-management adapters used by the whole agent
  lifecycle), not something to bundle in as a drive-by alongside sixteen
  other new files. (`rayd_core::template` itself — the pure domain this
  deferred work would build on — compiles, passes `cargo test
  --workspace` and clippy pedantic cleanly, verified by this PR's own CI;
  the shared Lima VM's disk was at 0 bytes free for most of this session,
  so that verification happened in CI rather than locally, see below.)
  Until this lands, an image
  built with `setStartCmd()` bakes `/etc/rayito/template.json` correctly,
  but nothing on the agent side reads it yet: `rayd` boots exactly as it
  does for an image with no template (TPL-15's "survives suspend/resume"
  acceptance criterion cannot be exercised until this follow-up ships).
  **This also means `Template.build()`'s own e2e acceptance (and the
  ADR-022 "fully implemented" claim) are blocked on this follow-up**: the
  AWS stage can still exercise the DSL -> Dockerfile -> zip -> S3 ->
  `create-microvm-image` -> log-based failure path end to end (steps 1 and
  2 of the plan below), but not a `setStartCmd()` image's actual
  start/ready behavior inside a launched sandbox (step 4).
- **Disk-space cross-cutting risk (observed, since resolved for this PR).**
  The Lima VM's 58 GB disk was completely exhausted by the combined
  `CARGO_TARGET_DIR`s of the sibling M15 feature branches for most of this
  session (`efs-volumes`, `rayd-otlp`, `secrets-gateway` each over 10 GB;
  it recovered to 11 GB free later in the session as sibling builds
  finished and freed their own space). This PR's own Rust change ended up
  verified by the PR's CI instead of locally for that reason. Flagged for
  the maintainer/orchestrator regardless, since it will recur with the
  next round of parallel Rust work: either a larger disk, a shared
  `CARGO_TARGET_DIR` with `sccache`, or pruning finished branches' target
  dirs between rounds.

## Impact

- **Rust**: `crates/rayd-core/src/template.rs` (new, pure domain:
  `StartSpec`, `ReadyPoll`, `ready_decision`), `crates/rayd-core/src/lib.rs`
  (`pub mod template;`). No `rayd` adapter changes (see follow-ups above).
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

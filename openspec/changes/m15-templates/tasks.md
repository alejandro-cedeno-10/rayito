## 1. rayd-core domain

- [x] 1.1 `crates/rayd-core/src/template.rs`: `StartSpec`, `ReadyPoll`,
      `ready_decision`, `TEMPLATE_SPEC_VERSION`, `TEMPLATE_SPEC_PATH`,
      unit-tested (JSON round-trip, the SDK's actual JSON shape, the
      retry/fail/ok decision table).
- [x] 1.2 `crates/rayd-core/src/lib.rs`: `pub mod template;`.
- [x] 1.3 `cargo fmt --all --check`, `cargo clippy --workspace
      --all-targets --locked -- -D warnings` and `cargo test --workspace`
      all clean — verified by the PR's own CI (`lint + test (x86_64)`,
      `Analyze (rust)`, `rayd aarch64-unknown-linux-musl`), not inside the
      Lima VM: the shared VM's 58 GB disk was at 0 bytes free for most of
      this session (every sibling M15 branch has its own multi-GB
      `CARGO_TARGET_DIR`), so `cargo` could not be bootstrapped there in
      time; CI caught and this change fixed two real issues this way (a
      `cargo fmt` line-length diff, and `Duration::from_secs_f64` panicking
      on non-finite input, replaced with the non-panicking
      `try_from_secs_f64`).
- [x] 1.4 `rayd` adapters (`adapters/fs_template_spec.rs`,
      `adapters/shell_ready_probe.rs`), `features/template_start.rs` real
      slot with its `TemplateParticipant` (`LifecycleParticipant`:
      `on_boot`, before the build-time `/ready`, spawns `start_cmd` through
      `ProcessManager::start_at_boot`, visible in
      `commands.list`, and polls `ready_cmd`; `ready_gate` maps
      `ready_decision`; `Fail` answers 500, Q85), `main.rs` calling `on_boot`,
      `main.rs` participant collection; `no_template_json_yields_no_participant`
      proves `/ready`/`/suspend` are unchanged without `template.json`.
      `cargo fmt`, `cargo clippy -p rayd -p rayd-core --all-targets -D
      warnings` and `cargo test -p rayd -p rayd-core` clean in the Lima VM
      (user `tester`, `-j 2`, under `flock`).

## 2. Python: domain and build pipeline

- [x] 2.1 `_templates/_instructions.py`: `BaseImageRef`, the five wire
      step dataclasses, `StartSpec`, `ReadyPoll`, `TemplateSpec`.
- [x] 2.2 `_templates/_ready_cmds.py`: `ReadyCommand`,
      `wait_for_port/url/process/file`.
- [x] 2.3 `_templates/_dsl.py`: `Template`, `AsyncTemplate` (fluent,
      immutable; `from_image`/`from_template`/`from_dockerfile`/
      `from_gcp_registry`/`apt_install` raise `UnimplementedError`).
- [x] 2.4 `_templates/_dockerfile.py`: `render_appended_layer`,
      `compose_dockerfile` (inserts before the base's terminal
      `CMD`/`ENTRYPOINT`, re-bases cleanly when composing on an
      already-composed image).
- [x] 2.5 `_templates/_context.py` + local-fs reads inline:
      `DockerIgnore`, `collect_context_files`, `files_hash`.
- [x] 2.6 `_templates/_artifact.py`: `read_zip_entries`,
      `assemble_artifact`, `start_spec_to_json`.
- [x] 2.7 `_templates/_logs.py`: `parse_build_failure`,
      `classify_ready_failure`.
- [x] 2.8 `_templates/_concurrency.py`: `build_slot`
      (`MAX_CONCURRENT_BUILDS = 10`, Q83).
- [x] 2.9 `_templates/_build.py` (sync) + `_templates/_build_async.py`
      (`asyncio.to_thread` wrapper): `build`, `build_in_background`,
      `get_build_status`, `template_exists`; `BuildClients` Protocol +
      `_Clients` adapter (lazy `boto3` clients, zero at construction).
- [x] 2.10 `_templates/_models.py`: `BuildInfo`, `BuildHandle`,
      `BuildStatus`.
- [x] 2.11 `rayito/__init__.py`: exports every public name.
- [x] 2.12 Unit tests: `test_m15_templates_{dsl,dockerfile,context,
      artifact,logs,concurrency,build,build_async}.py`,
      `tests/unit/fake_templates.py` (new fake, never edits a shared one).
- [x] 2.13 `tests/e2e/test_m15_templates.py` (gated by `RAYITO_E2E=1`,
      run only in the AWS acceptance stage): a successful build, a build
      failing a RUN step, a build failing its `ready_cmd`.

## 3. Python: E2B shim and stacks

- [x] 3.1 `e2b/_template.py`: `Template`/`AsyncTemplate` subtype the
      native classes, add `alias_exists`/`assign_tags`/`remove_tags`/
      `get_tags` raising `UnimplementedError`.
- [x] 3.2 `e2b/_unimplemented.py`: removes the `Template`/`AsyncTemplate`
      `unimplemented_resource` stand-ins and the blanket `"Template"` D14
      entry, adds the four tag-method entries (`TEMPLATE_TAGS_REASON`,
      citing `AWS_API_NOTES.md §27`).
- [x] 3.3 `e2b/exceptions.py`: `TemplateException`/`BuildException`
      become re-exports of the native classes.
- [x] 3.4 `e2b/__init__.py`: imports `Template`/`AsyncTemplate` from
      `e2b._template` instead of `_unimplemented`.
- [x] 3.5 `_stacks/components/templates.py`: fills the stub
      (`RayitoTemplateBuilder`, parameters, cost statement).
- [x] 3.6 `infra/templates.yaml` + `scripts/gen_stack_assets.py` run
      (regenerates `_stacks/_templates/templates.yaml` and the TS
      `.gen.ts` mirror).

## 4. Python gates

- [x] 4.1 `uv run pytest` — 2,672 passed (full suite, including the new
      ~55 template tests), 0 failed.
- [x] 4.2 `uv run ruff check` clean.
- [x] 4.3 `uv run ruff format --check` clean.
- [x] 4.4 `uv run mypy` clean (strict).
- [x] 4.5 `uv run --group docs mkdocs build -f ../../docs/site/mkdocs.yml
      --strict` clean.

## 5. TypeScript: domain and build pipeline

- [x] 5.1 `templates/instructions.ts`: `BaseImageRef`, the five wire
      step interfaces, `StartSpec`, `ReadyPoll`, `TemplateSpec`.
- [x] 5.2 `templates/ready-cmds.ts`: `ReadyCommand`,
      `waitForPort/Url/Process/File`.
- [x] 5.3 `templates/dsl.ts`: `Template` (fluent, immutable; single class,
      no `AsyncTemplate` — TS has no sync variant to mirror).
- [x] 5.4 `templates/dockerfile.ts`: `renderAppendedLayer`,
      `composeDockerfile`.
- [x] 5.5 `templates/context.ts` (pure: `DockerIgnore`, `filesHash`,
      `copySteps`) + `templates/context-node.ts` (the `node:fs` adapter).
- [x] 5.6 `templates/zip-node.ts`: dependency-free ZIP reader (stored +
      DEFLATE via `node:zlib`) and writer (stored), see design.md T3.
- [x] 5.7 `templates/artifact.ts`: `assembleArtifact`, `startSpecToJson`.
- [x] 5.8 `templates/logs.ts`: `parseBuildFailure`,
      `classifyReadyFailure`.
- [x] 5.9 `templates/concurrency.ts`: `withBuildSlot`.
- [x] 5.10 `templates/build.ts`: `build`, `buildInBackground`,
      `getBuildStatus`, `templateExists`; `BuildClients` interface +
      `AwsBuildClients` adapter. `@aws-sdk/client-s3` loaded lazily
      (`loadS3Module`, mirroring `sandbox/transfer.ts`'s own lazy loader)
      so importing `rayito` never statically imports it;
      `@aws-sdk/client-cloudwatch-logs` loaded via the existing
      `loadOptionalSdkClient` seam, only on a failed build.
- [x] 5.11 `package.json`/`pnpm-lock.yaml`: `@aws-sdk/client-cloudwatch-logs`
      added as optional peer + devDependency.
- [x] 5.12 `index.ts`: exports every public name, plus `BuildError`/
      `TemplateError` promoted to real exports (design.md T5).
- [x] 5.13 `tests/e2e/m15-templates.e2e.test.ts` (gated by
      `RAYITO_E2E=1`, run only in the AWS acceptance stage): same three
      scenarios as the Python e2e file.

## 6. TypeScript: E2B shim and stacks

- [x] 6.1 `e2b/template.ts`: `Template` subtypes the native class, adds
      the four tag stubs.
- [x] 6.2 `e2b/resources.ts`: `Template` class removed (moved to
      `template.ts`); `Volume` untouched (efs-volumes owns it).
- [x] 6.3 `e2b/unimplemented.ts`: same table change as Python.
- [x] 6.4 `e2b/client.ts`: `get Template()` returns the real class
      instead of throwing.
- [x] 6.5 `e2b/index.ts`: `Template` exported from `./template.js`;
      `BuildError`/`TemplateError` re-exported as native-class aliases;
      `src/e2b/errors.ts` deleted (design.md T5).
- [x] 6.6 `stacks/components/templates.ts`: fills the stub.
- [x] 6.7 `stacks/packaging.ts`: registers the `templates` generated
      asset.
- [x] 6.8 `scripts/check-dts-cost-blocks.mjs` reads the drop-in
      registry `scripts/cost-declarations/*.json`; templates registers
      `class Template` in its own `cost-declarations/templates.json`; the
      "Coste y activación" TSDoc block sits directly above the `Template`
      class.

## 7. TypeScript gates

- [x] 7.1 `pnpm typecheck` clean.
- [x] 7.2 `pnpm lint` (biome) clean (0 errors; 16 info-level
      `useLiteralKeys` suggestions on already-typed `Record<string,
      unknown>` lookups, left as bracket access for consistency with the
      rest of the file).
- [x] 7.3 `pnpm test` — 1,170 passed, 0 failed (one unrelated, confirmed
      pre-existing timing flake in `abort.test.ts` reproduced in
      isolation as a pass when machine load from sibling agents' parallel
      test runs was lower; not touched by this change, see the final
      report).
- [x] 7.4 `pnpm build && pnpm pack:check` clean (both `pnpm build`'s
      `tsdown` and `pack:check`'s `check-dts-cost-blocks.mjs` run on the
      local Node toolchain, not the disk-starved Lima VM; a cost block was
      also added to `e2b/template.ts`'s `Template`, which
      `check-dts-cost-blocks.mjs` checks separately from the native one
      since `rayito/e2b`'s bundle re-declares the class).

## 8. OpenSpec and docs

- [x] 8.1 `openspec/changes/m15-templates/{proposal,design,tasks}.md`.
- [x] 8.2 `specs/declarative-templates/spec.md` (new capability, ADDED
      requirements only).
- [x] 8.3 `docs-delta.md` (exact replacement rows for e2b-parity,
      optional-features, cost, security — applied later by
      `m15-docs-integration`).
- [x] 8.4 `docs/site/docs/funciones-opcionales/templates.md` filled in
      (replacing the "En construcción" stub).
- [x] 8.5 `ARCHITECTURE.md` ADR-022 filled in (inside its pre-created
      section only).
- [x] 8.6 `AWS_API_NOTES.md` §27 filled in (inside its pre-created
      section only).
- [x] 8.7 `MILESTONES.md`: the one-line `templates` bullet under "## M15"
      expanded to a short paragraph (same section, no other bullet
      touched).
- [x] 8.8 Three `CHANGELOG.md` files: `<!-- m15-templates -->` anchor
      replaced with the real entry in each.
- [x] 8.9 `docs/RELEASE_NOTES_0.6.0.md`: its pre-created `templates` stub
      section filled in.
- [x] 8.10 `npx -y @fission-ai/openspec@1.10.0 validate --strict` passes.

## 9. Review fixes

- [x] 9.1 Shared image-build core `rayito/_images.py` /
      `src/images/gateway.ts`, extracted from `cli/_publish.py` with no
      behaviour change; `Template.build()` consumes it (failed image
      states settle the gate, Q52 reuse normalisation, `ACTIVE` check).
- [x] 9.2 Composed configuration inherits every configuration key of the
      base version (`additionalOsCapabilities` included).
- [x] 9.3 `skip_cache()`/`skipCache()` acts as `force=True`.
- [x] 9.4 `build()` holds its `MAX_CONCURRENT_BUILDS` slot through the
      gate wait; `ServiceQuotaExceededException` maps to
      `reason="build_quota"`; gate timeout is `reason="build_timeout"`.
- [x] 9.5 Default `context_dir` resolved; a `src` escaping the context is
      `reason="context_path_outside"` (both SDKs).
- [x] 9.6 Context files under `__rayito_context/` in the zip (T26).
- [x] 9.7 `COPY` JSON form, escaped `ENV`, line breaks rejected;
      `shlex.quote`/`shellQuote` in ready commands; shared vectors in
      `testdata/templates/dockerfile-cases.json` run by both SDKs.
- [x] 9.8 Error messages carry the template name and state, never an
      ARN or the raw `stateReason`.
- [x] 9.9 Named defaults instead of repeated literals.
- [x] 9.10 E2B shim build signature (`alias`, `skip_cache`, `cpu_count`,
      `memory_mb`), `E2B(bucket=...)` binding, `TemplateException` for an
      invalid name.
- [x] 9.11 `infra/templates.yaml`: image actions scoped to
      `microvm-image:*`, `Deny` on the published base images, no
      `s3:HeadObject`/unused build actions, base-artifact read defaults to
      the artifact bucket; `scripts/tests/test_templates_template.py`.

## 10. PR

- [x] 10.1 Branch `feat/templates`, worktree
      `~/github.com/alejandro-cedeno-10/rayito-wt-templates`, from
      `origin/main`.
- [ ] 10.2 Push and open the PR; wait for CI; fix until green.
- [ ] 10.3 Do not merge (per instructions).

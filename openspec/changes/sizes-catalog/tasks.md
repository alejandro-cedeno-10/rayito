## 1. Python: pure domain and ports

- [x] 1.1 `_sizing.py`: `SizeRequest`, `ResolvedSize`, `resolve_size`,
      `apply_size_suffix`, `baseline_cpu_for`, `warn_if_rounded`,
      `SIZE_NAMES`/`NAME_TO_MEMORY_MIB`/`MEMORY_MIB_TO_NAME`,
      `BASELINE_MEMORY_MIB`, `MIB_PER_VCPU_Q88` — pure, no I/O.
- [x] 1.2 `_size_catalog.py`: `ImageVersionReader`/`SizeCatalog` ports,
      `ConventionCatalog` adapter (cached per `(imageArn, imageVersion)`
      and per process), `DEFAULT_SIZE_CATALOG` singleton.
- [x] 1.3 `_aws.py`: `ControlPlane.get_microvm_image_version` (port) +
      `LambdaMicrovmsControlPlane.get_microvm_image_version` (adapter,
      `GetMicrovmImageVersion`); `_models.py`: `ImageVersionInfo`.
- [x] 1.4 `sandbox_sync/pool.py`: `LaunchObserver.get_microvm_image_version`
      delegation (keeps it a full `ControlPlane`).

## 2. Python: SDK wiring

- [x] 2.1 `_models.py`: `SandboxInfo.size`/`baseline_memory_mib`/
      `baseline_cpu`; `LaunchOptions.size`.
- [x] 2.2 `_sandbox_base.py`: `with_size_facts` (pure merge).
- [x] 2.3 `_feature_options.py`: the `size` branch of `plan_features` no
      longer raises (real resolution happens in `create()`, not here —
      `size` produces no `ConfigureSandbox` section).
- [x] 2.4 `sandbox_sync/main.py` / `sandbox_async/main.py`: `create()`
      resolves `size` (pure, before `resolve_control_plane`), applies the
      suffix to the template name before resolving the ARN, stores the
      resolved size in `LaunchOptions`; `get_info()` confirms the baseline
      via `DEFAULT_SIZE_CATALOG` only when `size` was used.

## 3. Python: CLI

- [x] 3.1 `cli/_publish.py`: `PublishSettings.environment_variables`;
      `desired_configuration` adds `environmentVariables` only when
      non-empty; `prepare_build`/`reusable_version` extracted from
      `publish()` (behavior-preserving); `sized_settings`,
      `publish_sizes` (waves of `MAX_CONCURRENT_IMAGE_BUILDS_Q83`).
- [x] 3.2 `cli/image.py`: `--sizes`, `--env` on `rayito image publish`;
      `validate_sizes`, `parse_environment_assignments`.

## 4. Python: exports and stack component

- [x] 4.1 `__init__.py`: exports `SizeRequest`.
- [x] 4.2 `_stacks/components/sizes_guard.py`: real `StackComponent`
      (`supported: True`, `ImageArns` required parameter,
      `CAPABILITY_IAM`).
- [x] 4.3 `infra/sizes-guard.yaml`; rendered into both SDKs with
      `scripts/gen_stack_assets.py` (no script changes needed).

## 5. TypeScript mirror

- [x] 5.1 `sizing/sizing.ts`, `sizing/catalog.ts` (mirrors 1.1/1.2).
- [x] 5.2 `aws/control-plane.ts`: `ControlPlane.getMicrovmImageVersion`
      (port + `LambdaMicrovmsControlPlane` adapter via
      `GetMicrovmImageVersionCommand`); `pool/pool.ts`:
      `LaunchObserver.getMicrovmImageVersion` delegation.
- [x] 5.3 `models.ts`: `SandboxInfoFields`/`SandboxInfo`
      `size`/`baselineMemoryMib`/`baselineCpu`, `withSizeFacts`;
      `sandbox/persistence.ts`: `LaunchOptions.size`.
- [x] 5.4 `feature-options.ts`: `size` no longer raises.
- [x] 5.5 `sandbox/sandbox.ts`: `create()`/`getInfo()` wiring, mirrors 2.4.
- [x] 5.6 `index.ts`: exports `SizeRequest`/`SizeInput`/`SizeName`/
      `ResolvedSize` types; `stacks/components/sizes-guard.ts`: real
      component.
- [x] 5.7 No TS CLI change: `rayito image publish --sizes`/`--env` is
      Python-only (confirmed: TS has no CLI package).

## 6. Shared test fixtures (structural addition, not feature logic)

- [x] 6.1 `tests/unit/fake_control_plane.py`,
      `tests/unit/cli/conftest.py`'s `FakeControlPlane`,
      `tests/unit/fake/control-plane.ts`,
      `tests/unit/fake/pool-plane.ts`: added
      `get_microvm_image_version`/`getMicrovmImageVersion` (the `ControlPlane`
      port this change extends); one literal `ControlPlane` fixture in
      `transfer-credentials.test.ts` updated the same way.
- [x] 6.2 Fixed a pre-existing gap in `tests/unit/fake/control-plane.ts`'s
      `info()`: it returned the hardcoded `IMAGE_ARN` instead of the
      actually-launched `imageArn` (the Python fake already used the real
      launch request) — needed so a sized template's suffix shows up in
      `sandbox.info.template` in tests; verified no existing test depended
      on the old (wrong) hardcoded value.

## 7. Tests

- [x] 7.1 `test_m15_sizes_catalog_domain.py` /
      `m15-sizes-catalog-sizing.test.ts`: `resolve_size`/`resolveSize`
      rounding and errors, `apply_size_suffix`/`applySizeSuffix`,
      `baseline_cpu_for`/`baselineCpuFor` against the RES-2/Q88 table,
      `warn_if_rounded`/`warnIfRounded`.
- [x] 7.2 `test_m15_sizes_catalog_catalog.py` /
      `m15-sizes-catalog-catalog.test.ts`: `ConventionCatalog` caching,
      per-key isolation, concurrent-call sharing (TS), failed-read
      invalidation (TS).
- [x] 7.3 `test_m15_sizes_catalog_create.py` /
      `m15-sizes-catalog-create.test.ts`: end-to-end `create(size=...)`
      against the fake control plane — suffix applied, baseline kept
      unsuffixed, no `GetMicrovmImageVersion` without `size`, one cached
      call with it, rounding warning, ARN + size rejected, impossible size
      rejected, all before any AWS call where applicable.
- [x] 7.4 `test_m15_sizes_catalog_stack.py`: `sizes-guard` is `supported`,
      declares `ImageArns` required, rejects deploy without it, deploys
      with it.
- [x] 7.5 `cli/test_image_sizes.py`: `validate_sizes`/
      `parse_environment_assignments` (pure), `sized_settings`,
      `desired_configuration` environment variables, `publish_sizes`
      end-to-end reuse path with `Stubber`.
- [x] 7.6 Updated (not feature-owned, but required by implementing a
      previously-stub branch): `test_m15_feature_options.py`,
      `test_m15_create_kwargs.py`, `m15-feature-options.test.ts`,
      `m15-create-options.test.ts` — removed `size` from the "every 0.6
      option raises/is rejected" parametrized cases, added a dedicated
      `pool + size` rejection case in each.

## 8. Gates (all green on this branch)

- [x] 8.1 Python: `uv run pytest` (2600+ tests), `uv run ruff check`,
      `uv run ruff format --check`, `uv run mypy` — all clean.
- [x] 8.2 TypeScript: `pnpm lint` (biome), `pnpm typecheck` (tsc),
      `pnpm test` (vitest, 1165 tests), `pnpm build` + `pnpm pack:check`
      — all clean.
- [x] 8.3 `python scripts/gen_stack_assets.py --check` and
      `python scripts/gen_limits.py --check` — no drift.
- [x] 8.4 `cd clients/python && uv run --group docs mkdocs build -f
      ../../docs/site/mkdocs.yml --strict` — clean (after routing the new
      guide page's cross-links away from anchors `m15-docs-integration`
      has not created yet).
- [x] 8.5 `npx -y @fission-ai/openspec@1.10.0 validate --strict` on this
      change — clean.

## 9. Docs

- [x] 9.1 ADR-019 filled in `ARCHITECTURE.md`.
- [x] 9.2 AWS_API_NOTES.md §24 filled (table + RES-2/Q88 per-size data +
      Q90 guardrail note).
- [x] 9.3 `MILESTONES.md`'s sizes-catalog bullet filled.
- [x] 9.4 `docs/RELEASE_NOTES_0.6.0.md`'s sizes-catalog section filled.
- [x] 9.5 Both `CHANGELOG.md` anchors filled (content added after the
      `<!-- m15-sizes-catalog -->` anchor comment, which stays in place).
- [x] 9.6 `docs/site/docs/funciones-opcionales/tamanos.md`: full guide
      (Coste y activación, Python sync/async + TypeScript tabs, how it
      works, the RES-2/Q88 guest-vs-image table, publishing, the
      `sizes-guard` guardrail, errors, E2B differences).
- [x] 9.7 `docs/site/docs/referencia/python/opcionales.md`: new "Tamaños"
      section (`::: rayito.SizeRequest`).
- [x] 9.8 `docs-delta.md` in this directory: exact replacement rows for
      `e2b-parity.md`, `optional-features.md`, `cost.md`, `security.md`
      (threat T27) and `limits.md`, for `m15-docs-integration` to apply.

## 10. Not done in this change (explicitly out of scope)

- AWS acceptance (serialized stage, separate agent); `aws_plan` for it is
  in this change's PR description / the task's final report, not in this
  repo.
- `_images.py` extraction as a standalone `ImageBuildGateway` port: the
  M15 architecture names it, but nothing in this change needs the full
  port abstraction (only `environment_variables` and the
  `prepare_build`/`reusable_version` split, both done). Left for whichever
  future change (e.g. `m15-templates`, which also publishes images) first
  needs to swap the adapter.

## Why

`create-microvm-image` fixes the guest's memory with
`resources[0].minimumMemoryInMiB` per *image version*, not per launch: there
is no `RunMicrovm`-time way to pick a size. Today every Rayito image is the
same 2048 MiB baseline. Several workloads need more (or less) memory
without the operator hand-maintaining a parallel set of differently-named
images and keeping their `rayd` version in sync by hand.

SPEC.md §4 said "no hay, ni se promete, un resolvedor de tamaño por
sandbox" (ADR-019 replaces this; D1 leaves the exact non-goals wording to
the maintainer). Measured (`docs/research/2026-10-e2b-out-of-scope.md`,
RES-1/Q87): only 512/1024/2048/4096/8192 MiB build; any other value is a
synchronous `ValidationException` with no image created. Measured
(RES-2/Q88, confirming the single earlier data point of Q61/Q68 at 2048
MiB): the guest sees memory/512 vCPU and up to ≈4x the declared memory
across all five catalog sizes. Measured (RES-13/Q90): an IAM policy scoped
to a specific image ARN does block `RunMicrovm` on a larger one.

## What Changes

- **A closed catalog, resolved entirely in the SDK, no RPC.** `size=`
  (Python) / `size` (TypeScript) is not a `ConfigureSandbox` section: it
  decides *which image to launch*, not a running-guest adjustment.
  `resolve_size`/`resolveSize` (`_sizing.py`/`sizing/sizing.ts`, pure)
  rounds up to the first published value that covers the request (never
  down), warns with `RayitoCompatWarning` when it does not land exactly on
  a catalog value, and raises `InvalidArgumentException`/
  `InvalidArgumentError` before any AWS call for an impossible request
  (e.g. 16384 MiB) or for an ARN template (an ARN names the image by
  itself; the `<variant>[-<size>]` suffix convention needs a name).
- **Naming convention.** `apply_size_suffix`/`applySizeSuffix` appends the
  size name (`"rayito-base"` + `4gb` -> `"rayito-base-4gb"`); the baseline
  (2048 MiB) never gets a suffix, so `rayito image publish` without
  `--sizes` keeps publishing byte-for-byte what it published before this
  change.
- **Publishing.** `rayito image publish --sizes 512mb,4gb` publishes, from
  the *same* artifact, one additional image per requested size
  (`cli/_publish.py`: `sized_settings`/`publish_sizes`), baking
  `RAYITO_BASELINE_MEMORY_MIB` into each one's `environmentVariables`
  (image configuration, never an activation switch — ADR-014 rule 4) and
  submitting the `create`/`update-microvm-image` calls in waves of at most
  `MAX_CONCURRENT_IMAGE_BUILDS_Q83` (10) simultaneous builds before
  waiting for any of them to settle. A generic `--env KEY=VALUE`
  (repeatable) option adds arbitrary image-level environment variables to
  every image an invocation publishes, baseline included (the `_images.py`
  extraction the M15 architecture names; done here as the minimal,
  behavior-preserving refactor of `publish()` into `prepare_build`/
  `reusable_version` helpers that `publish_sizes` reuses, not a full
  `ImageBuildGateway` port — no consumer needs that port yet).
- **Confirming the real size.** `ConventionCatalog`
  (`_size_catalog.py`/`sizing/catalog.ts`) makes, only when `size=`/`size`
  was used, a single free `GetMicrovmImageVersion` call per
  `(imageArn, imageVersion)` (cached per process) to read
  `resources[0].minimumMemoryInMiB`, and `get_info()`/`getInfo()` uses it
  to fill `SandboxInfo.baseline_memory_mib`/`baselineMemoryMib` and
  `baseline_cpu`/`baselineCpu` (RES-2/Q88: the guest's vCPU count for that
  memory, measured exactly for all five catalog sizes, not extrapolated).
  `cpu_count`/`memory_mb` (`cpuCount`/`memoryMb`) are left untouched: they
  keep reporting what `Health` says the guest actually has.
  `create(pool=, size=)`/`create({ pool, size })` stays
  `InvalidArgumentException`/`InvalidArgumentError`: a pool slot already
  launched from a fixed image.
- **Cost guardrail.** `infra/sizes-guard.yaml` (`OptionalStack` component
  `sizes-guard`, `RayitoRunAllowedSizes`): an IAM managed policy that
  restricts `lambda:RunMicrovm` to the image ARNs the operator lists, so a
  caller cannot accidentally launch a larger (costlier) size than
  intended. `CAPABILITY_IAM` only, no billable resource.

## Impact

- **Python**: new `_sizing.py` (pure), `_size_catalog.py` (port +
  `ConventionCatalog` adapter); `_feature_options.py`'s `size` branch
  (stub -> no-op, real resolution moved to `create()`);
  `sandbox_sync/main.py`/`sandbox_async/main.py` (`create()` resolves and
  suffixes the template before resolving the ARN, `get_info()` confirms
  the baseline via the catalog); `_models.py` (`ImageVersionInfo`,
  `SandboxInfo.size`/`baseline_memory_mib`/`baseline_cpu`,
  `LaunchOptions.size`); `_aws.py`/`sandbox_sync/pool.py`
  (`ControlPlane.get_microvm_image_version`, `LaunchObserver` delegation);
  `_sandbox_base.py` (`with_size_facts`); `cli/_publish.py`
  (`environment_variables`, `sized_settings`, `publish_sizes`,
  `prepare_build`/`reusable_version` refactor); `cli/image.py`
  (`--sizes`/`--env`, `validate_sizes`, `parse_environment_assignments`);
  `__init__.py` (exports `SizeRequest`); `_stacks/components/sizes_guard.py`
  (real `StackComponent`, `supported: True`).
- **TypeScript**: mirrors every Python change (`sizing/sizing.ts`,
  `sizing/catalog.ts`, `models.ts`, `sandbox/persistence.ts`,
  `sandbox/sandbox.ts`, `aws/control-plane.ts`, `pool/pool.ts`,
  `feature-options.ts`, `index.ts`, `stacks/components/sizes-guard.ts`); no
  CLI (`rayito image publish --sizes`/`--env` is Python-only, per the M15
  architecture).
- **Infra**: new `infra/sizes-guard.yaml`, rendered into both SDKs by the
  existing `scripts/gen_stack_assets.py` (no change to that script: it
  already globs `infra/*.yaml`).
- **Tests**: `test_m15_sizes_catalog_{domain,catalog,create,stack}.py`,
  `cli/test_image_sizes.py` (Python); `m15-sizes-catalog-{sizing,catalog,create}.test.ts`
  (TypeScript); the two shared fixture files (`fake_control_plane.py`,
  `tests/unit/cli/conftest.py`, `tests/unit/fake/{control-plane,pool-plane}.ts`)
  gained `getMicrovmImageVersion`/`get_microvm_image_version` to keep
  implementing the `ControlPlane` port after this change extends it (a
  structural addition, not feature-specific test logic). The existing
  `test_m15_feature_options.py`/`m15-feature-options.test.ts` and
  `test_m15_create_kwargs.py`/`m15-create-options.test.ts` had their
  `size`/stub-raises case removed (size is no longer a stub) and a
  dedicated `pool + size` case added in its place.
- **Docs**: ADR-019 (`ARCHITECTURE.md`), AWS_API_NOTES.md §24,
  `MILESTONES.md`'s sizes-catalog bullet, `docs/RELEASE_NOTES_0.6.0.md`,
  both `CHANGELOG.md` anchors, `docs/site/docs/funciones-opcionales/tamanos.md`
  (full guide), `docs/site/docs/referencia/python/opcionales.md` (new
  `SizeRequest` section). `docs-delta.md` in this directory carries the
  exact rows for `e2b-parity.md`/`optional-features.md`/`cost.md`/
  `security.md`/`limits.md`, applied later by `m15-docs-integration`
  (features never touch those shared tables directly).
- **No changes** to any 0.5.x public behaviour: `size=`/`size` defaults to
  `None`/`undefined` and the M15-foundations Python zero-cost golden test
  (`test_m15_zero_cost.py`) still passes unmodified. No TypeScript
  equivalent exists yet in the repo despite `v06-foundations`' proposal
  naming one (`zero_cost_0_5_trace.json` is Python-only today); noted here,
  not fixed by this change (not a sizes-catalog file).

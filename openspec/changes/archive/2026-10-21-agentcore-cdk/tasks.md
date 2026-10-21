## How to use this file

Four lanes, each on its own branch cut from `feat/agentcore-cdk`, each
opening a pull request back into it (nothing goes to `main` from a lane, and
nothing merges without the maintainer's review): **Track PY**
(`clients/python/` only), **Track TS** (`clients/typescript/` only), **Track
DOCS** (`docs/site/`, `SECURITY.md`, `infra/README.md`, `README*.md`) and
**E2E** (`.github/workflows/e2e.yml`, the e2e tests and the runbook, after
the other three merge). The AWS acceptance runs once on the tip of
`feat/agentcore-cdk`, after every lane is merged for review.

Every lane reads `proposal.md`, `design.md`, `specs/agentcore-cdk/spec.md`,
`testdata/agentcore-cdk/` (README and `contract.json`), `AWS_API_NOTES.md`
(«Constructos CDK», Q181-Q186) and ADR-032 before writing code. Hard rules:
hexagonal layers (pure domain, ports, AWS only in adapters; the constructs are
adapters over the pure planning code); code, file and folder names in
English; docstrings, TSDoc, comments and user messages in Spanish; no magic
numbers (constants from `limits.json`, the contracts or `AWS_API_NOTES.md`);
no inline comments inside function bodies; no AWS parameter or CloudFormation
property absent from `AWS_API_NOTES.md`; reuse instead of duplicating; no
real account id, ARN, bucket, org name or tag value in any file (examples use
`123456789012` and `amzn-s3-demo-bucket`); commits `git commit -s -S`,
Conventional Commits, no AI attribution of any kind. If the contract and the
code disagree, the contract wins and the lane reports the mismatch.

Nobody edits in a lane: `testdata/agentcore-cdk/contract.json`,
`testdata/agentcore-cdk/cases/*/input.json`, `limits.json`,
`infra/agentcore-cdk-handler.py`, `infra/agentcore-runtime.yaml`,
`scripts/gen_stack_assets.py`, `AWS_API_NOTES.md`; a needed change goes back
to the planner. Exception: Track PY owns the generated
`testdata/agentcore-cdk/cases/*/expected.template.json` (written by
`scripts/gen_cdk_vectors.py`); Track TS rebases on it and never edits it.

## 0. Shared prerequisites (done in this change, do not edit in a lane)

- [x] 0.1 Verify the CloudFormation schemas of
      `AWS::BedrockAgentCore::Runtime` (no metadata/MMDS property) and
      `AWS::Lambda::MicrovmImage` (required properties, write-only inputs,
      create-only name, handlers, return values) and the `aws-cdk-lib`
      version boundaries; record them as `AWS_API_NOTES.md` Q181-Q184 and
      the pending measurements as Q185-Q186.
- [x] 0.2 ADR-032 in `ARCHITECTURE.md`, superseding ADR-031 D12.
- [x] 0.3 `limits.json` group `agentcore-cdk` (`cdkOwnershipSuffix`,
      `cdkManagedByTagValue`, version floors, handler runtime, timeouts,
      intervals, image log retention, the 2048-character environment value cap of the runtime schema), generated into `_limits.py` and
      `limits.ts` (`python3 scripts/gen_limits.py --check`).
- [x] 0.4 `infra/agentcore-cdk-handler.py` and its packaging in
      `scripts/gen_stack_assets.py` with the botocore model of
      `bedrock-agentcore-control` (`docs/aws-api/bedrock-agentcore-control/`):
      `clients/python/src/rayito/_stacks/_artifacts/agentcore-cdk-handler.zip`
      and `clients/typescript/src/stacks/templates/agentcore-cdk-handler.gen.ts`
      (`python3 scripts/gen_stack_assets.py --check`); handler tests in
      `scripts/tests/test_agentcore_cdk_handler.py`.
- [x] 0.5 `testdata/agentcore-cdk/` (README, `contract.json`, 12 cases,
      fixtures) and `scripts/gen_cdk_vectors.py` with `--check` and the
      reference `normalise_template` (`scripts/tests/test_gen_cdk_vectors.py`).
- [x] 0.6 This OpenSpec change, validated with
      `openspec validate agentcore-cdk --strict`.

## 1. Track PY (`clients/python/` only)

### 1.1 AgentCore ownership marker
- [x] 1.1.1 `_agentcore/_domain.py`: `CDK_DESCRIPTION_MARKER =
      AGENTCORE_DESCRIPTION_MARKER + CDK_OWNERSHIP_SUFFIX`;
      `runtime_description_for(name, *, managed_by_cdk=False)`; `has_marker`
      accepts both markers; new `is_cdk_managed(listed)`;
      `RUNTIME_DESCRIPTION_PATTERN` and `agent_name_of_runtime` read both.
- [x] 1.1.2 `check_runtime_usable` and the delete path raise `name_conflict`
      with `contract.cliRefusals.agentDeploy.message` (the constant
      `CDK_MANAGED_MESSAGE` in `_agentcore/_errors.py`, `CDK_MANAGED_MESSAGE`
      in TS `agentcore/messages.ts`, interpolating `name` and `stackHint` =
      `del runtime <runtimeName>`; the tests compare it with the contract)
      for a CDK runtime, before any write; `list`,
      `status`, `invoke` and `AgentCoreFleet.connect` unchanged. Tests:
      a CDK listing in `test_agentcore_name_collision.py`.

### 1.2 Pure image publish domain (no behaviour change)
- [x] 1.2.1 Move the pure part of `cli/_publish.py` to
      `src/rayito/_images_publish_domain.py` (`IMAGE_HOOKS`, `HOOKS_PORT`,
      `MANAGED_BASE_IMAGE_NAME`, `ALL_OS_CAPABILITIES`, `EFS_IMAGE_NAME`,
      `DEFAULT_IMAGE_NAMES`, `BASELINE_MEMORY_ENV_VAR`, `default_image_name`,
      `require_efs_combination`, `base_image_arn(region)`, `sized_settings`,
      and `desired_configuration(*, region, settings, artifact_uri,
      build_role_arn)`); `cli/_publish.py` imports and re-exports them;
      `tests/unit/cli/test_publish*.py` and `testdata/images/publish-vectors.json`
      pass untouched.
- [x] 1.2.2 `rayito image publish` refuses an image whose `GetMicrovmImage`
      `tags["rayito:managed-by"] == "rayito-cdk"`
      (`contract.cliRefusals.imagePublish.message`) before
      `UpdateMicrovmImage`; `image_exists` returns the response so no extra
      call is made. `image prune` unchanged.

### 1.3 `src/rayito/cdk/` (adapters over the pure code)
- [x] 1.3.1 `__init__.py`: `require_module("aws_cdk", extra="cdk",
      feature="rayito.cdk")`, then re-export `RayitoAgent`,
      `AgentImageBuild`, `RayitoMicrovmImage`, `MicrovmImageArtifact`,
      `RayitoCdkException` (`__all__` = `contract.sdk.python.exports`).
- [x] 1.3.2 `_errors.py`: `RayitoCdkException(code, **fields)` with the
      messages of `contract.errors.messages`.
- [x] 1.3.3 `_environment.py`: concrete region check (`region_unresolved`),
      account token handling, fixed tags (`componentTagValues`,
      `managedByTag`, `rayito:sdk-version`) through `Tags.of`.
- [x] 1.3.4 `_handler.py`: stage `agentcore-cdk-handler.zip` from package
      data to a temporary file once per process; `custom_resources.Provider`
      with `OnEvent`/`IsComplete` under `Handler`, runtime, timeouts and
      query interval from `_limits.py`, log groups with 14-day retention.
- [x] 1.3.5 `_agent.py`: `RayitoAgent` and `AgentImageBuild` per design D3-D6
      and `contract.constructIds.RayitoAgent`; plan through `plan_agentcore`
      (local files only, no session); nested stack via `CfnInclude` of the
      packaged `agentcore-runtime.yaml` with `_stack_parameters`'
      values; `localDocker` removes the build resources; attributes,
      `grant_invoke`, `runtime` (`Runtime.from_agent_runtime_attributes`),
      optional outputs.
- [x] 1.3.6 `_microvm_image.py`: `RayitoMicrovmImage` and
      `MicrovmImageArtifact` per design D9 and
      `contract.constructIds.RayitoMicrovmImage`; `cfn_image_properties`
      (pure, in `_image_properties.py`) maps `desired_configuration` to
      CloudFormation names; `download_release` reuses
      `GithubReleaseSource` and `verify_checksum` with an offline cache.
- [x] 1.3.7 «Coste y activación» block in every public docstring.

### 1.4 Packaging and tests
- [x] 1.4.1 `pyproject.toml` extra `cdk` and `rayito[cdk]` in the `dev`
      group; `uv lock`; `scripts/check_wheel.py` (`rayito/cdk/__init__.py`,
      `_stacks/_artifacts/agentcore-cdk-handler.zip`, `Provides-Extra: cdk`).
- [x] 1.4.2 `tests/unit/test_cdk_zero_cost.py`: `import rayito`, the CLI and
      MCP leave `aws_cdk`, `jsii`, `constructs` out of `sys.modules`.
- [x] 1.4.3 `tests/cdk_contract_harness.py` (`synthesize_case`) and
      `tests/unit/test_cdk_contract.py` over every case; generate
      `expected.template.json` with `scripts/gen_cdk_vectors.py`.
- [x] 1.4.4 `tests/unit/test_cdk_agent.py` and `test_cdk_microvm_image.py`:
      the synth assertions of design D12.1 (IAM equality with the YAML, tags
      on every taggable resource, errors, MMDS wiring, localDocker, image
      properties equal to `desired_configuration`).
- [x] 1.4.5 `scripts/check_sdk_parity.py`: compare `rayito.cdk.__all__` with
      `src/cdk/index.ts`.

## 2. Track TS (`clients/typescript/` only)

- [x] 2.1 `ProjectReader` port with an async fs adapter (SDK) and a sync
      adapter (CDK); `planAgentCoreSync` and a sync `renderExport` path that
      share every rule with the async ones; existing tests untouched.
- [x] 2.2 `agentcore/names.ts` and `decisions.ts`: same changes as 1.1
      (`CDK_DESCRIPTION_MARKER`, `isCdkManaged`, refusal message).
- [x] 2.3 `images/publish-service.ts`: same refusal as 1.2.2.
- [x] 2.4 `src/cdk/`: `index.ts` (exports of `contract.sdk.typescript`),
      `errors.ts` (`RayitoCdkError`), `environment.ts`, `handler.ts` (writes
      `ARTIFACT_BASE64` to a temporary zip once per process), `agent.ts`,
      `microvm-image.ts`, `image-properties.ts` (`cfnImageProperties` over
      `desiredConfiguration`); `downloadRelease` reuses the account-setup
      release source and checksum; TSDoc with «Coste y activación».
- [x] 2.5 `package.json` (`./cdk` export, optional peers, devDependencies),
      `tsdown.config.ts` entry and externals, `scripts/pack-check.mjs`
      (`dist/cdk.*` and the no-import assertion for other chunks),
      `check-dts-cost-blocks.mjs`, `public-exports.test.ts`.
- [x] 2.6 Tests: contract cases against the goldens written by Track PY
      (normaliser reimplemented from `contract.normalisation`, with one test
      that it equals the rules), synth assertions of D12.1, zero-cost import
      test.

## 3. E2E (after tracks 1, 2 and D merge)

- [ ] 3.1 `.github/workflows/e2e.yml`: `workflow_dispatch` choice
      `suite: cdk`, never on PRs or the schedule; existing `e2e`
      environment and OIDC role.
- [ ] 3.2 Runbook: one manual, tagged `cdk bootstrap` per test account and
      region; the e2e role may assume the bootstrap roles.
- [x] 3.3 Python and TypeScript apps: a run-named stack with one
      `RayitoAgent` (codeBuild) and one `RayitoMicrovmImage`; invoke the
      agent once, `connect` and `list` see it, `rayito agent deploy` and
      `rayito image publish` refuse; one sandbox from the image, killed;
      `cdk destroy --force` in `always()`; tag sweep limited to the run.
      Done as a manual run (2026-10-20, us-east-1, Python and TypeScript):
      deploy, invoke and stream, CLI list/status/invoke, CLI refusals, an
      unchanged redeploy with no rebuild, a code change that rebuilt, a
      sandbox from the CDK image, `cdk destroy`, and an account inventory
      equal before and after. The workflow (3.1) and runbook (3.2) are
      still to do.
- [x] 3.4 Record Q185 (does a CloudFormation update of the runtime reset
      `requireMMDSV2`?) and Q186 (does CloudFormation wait for a
      `SUCCESSFUL`/`ACTIVE` version, and does an update delete earlier
      versions?) in `AWS_API_NOTES.md`. Q185: no reset, runtimes come up
      with MMDSv2 on. Q186: the create waits for a usable version; whether an
      update deletes earlier versions is still unmeasured.

## D. Docs (Spanish default plus `.en.md`, `scripts/check_docs_i18n.py`)

- [x] D.1 `guias/desplegar-con-cdk.md` and `.en.md` (both constructs, the
      cost box, migration from the CLI, restricted bootstraps), linked from
      the AgentCore guide and the image guide; `mkdocs.yml` nav.
- [x] D.2 `optional-features.md` row, `novedades/0.15.0*.md`,
      `infra/README.md` (the handler), `referencia/python` entry for
      `rayito.cdk`.
- [x] D.3 `SECURITY.md` and `security*.md`: threat entry for the
      custom-resource and image build roles (scope, `iam:PassRole` only on
      the runtime role, read only of the asset object).

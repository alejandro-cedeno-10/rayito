## How to use this file

Three lanes run in parallel and never edit the same file: **Track PY**
(`clients/python/` only), **Track TS** (`clients/typescript/` only) and
**Track DOCS** (`docs/site/`, `ARCHITECTURE.md`, `SECURITY.md`, `README.md`,
`README.es.md`, `infra/README.md`, `CHANGELOG` of no package). Each lane works
on its own branch cut from `feat/agentcore-deploy` and opens a pull request
back into it; nothing merges without the maintainer's review, nothing goes to
`main` from a lane. The AWS acceptance (section 4) runs once on the tip of
`feat/agentcore-deploy` after the three lanes merge, driven by the
orchestrator.

Every lane reads `proposal.md`, `design.md`, `specs/agentcore-deploy/spec.md`,
`testdata/agentcore-deploy/*.json`, `AWS_API_NOTES.md` («Despliegue en
AgentCore Runtime»), `.claude/skills/rayito-engineering/SKILL.md` and its
references before writing code. Hard rules for every task: hexagonal layers
(pure domain, ports, AWS only in adapters, thin CLI and public classes); code,
file and folder names in English; docstrings, TSDoc, comments and user
messages in Spanish; no magic numbers (every value a named constant citing
`limits.json`, the contract or `AWS_API_NOTES.md`); no inline comments inside
function bodies; no AWS parameter absent from `AWS_API_NOTES.md`; nothing
speculative; reuse instead of duplicating; no secret, ARN with a real account,
bucket, org name or tag value in any file; commits `git commit -s -S`,
Conventional Commits, **no AI attribution of any kind**. If the contract and
the code disagree, the contract wins and the lane reports the mismatch.

Ownership exceptions (generated or golden files outside a lane's folder):

- Track PY owns `clients/typescript/src/agent/assets/host-assets.gen.ts` and
  `template-assets.gen.ts` **only as the output of**
  `python3 scripts/gen_agent_template_assets.py` after changing the host or
  the Node bridge, and the new export golden
  `testdata/agent/export/vercel-ai-agentcore/` (written with
  `RAYITO_UPDATE_SNAPSHOTS=1`). Track TS never edits those files; it rebases
  on Track PY before running the TypeScript export tests for that case.
- Nobody edits `testdata/agentcore-deploy/`, `testdata/cli/surface.json`,
  `limits.json`, `infra/agentcore-runtime.yaml`, `scripts/` or
  `AWS_API_NOTES.md`; a needed change goes back to the planner.

## 0. Shared prerequisites (done in this change, do not edit in a lane)

- [x] 0.1 This OpenSpec change, validated with
      `npx -y @fission-ai/openspec@1.10.0 validate --all --strict --no-interactive`.
- [x] 0.2 `testdata/agentcore-deploy/contract.json`: commands and options,
      exit codes, error codes and exit codes, constants (names, templates,
      tag digest, client token, session rules, MMDSv2, compared fields,
      export additions), `optionsTable`, `awsErrorMap`, the public SDK
      surface of both languages, JSON keys and every Spanish message.
- [x] 0.3 Vectors: `plan-vectors.json` (29), `apply-vectors.json` (22),
      `model-arns.json` (7 + 2), `session-ids.json` (10), `image-tags.json` (6).
- [x] 0.4 `infra/agentcore-runtime.yaml` (cfn-lint 1.56.3 clean), its
      `make infra-lint` entry, and the packaged copies
      `clients/python/src/rayito/_stacks/_templates/agentcore-runtime.yaml` and
      `clients/typescript/src/stacks/templates/agentcore-runtime.gen.ts`
      (`python3 scripts/gen_stack_assets.py --check` passes; the dashboard and
      quota-alerts zips were regenerated because they embed `_limits.py`).
- [x] 0.5 `limits.json` group `agentcore-deploy` (regions, names, prefixes,
      session, lifecycle, quotas, timeouts, build environment, retention,
      base-image mirror, env-credential adapters, prices, Bedrock profile
      prefixes) and `agentAwsCredentialsRequirementsSha256`, generated into
      `_limits.py` and `limits.ts` (`python3 scripts/gen_limits.py --check`),
      with its constant-name override in `scripts/gen_limits.py` and in both
      limits tests (`test_limits.py`, `limits.test.ts`).
- [x] 0.6 Agent assets `clients/python/src/rayito/_agent/_assets/requirements-aws-credentials.txt`
      (botocore 1.43.103, hash-pinned, arm64 wheels) and
      `aws_env_credentials.py`, registered in
      `scripts/gen_agent_template_assets.py` as `AWS_CREDENTIALS_REQUIREMENTS`
      and `AWS_ENV_CREDENTIALS_SCRIPT`; `template-assets.gen.ts` regenerated.
- [x] 0.7 `testdata/cli/surface.json`: the new options of `agent deploy`,
      `agent invoke`, `agent status`, `agent stop`, and the new
      `agent delete` and `agent list`, and their phase entries in
      `scripts/check_sdk_parity.py` (`CLI_PHASES`, phase 3).
- [x] 0.10 `scripts/tests/test_agentcore_runtime_template.py` (parameters and
      outputs equal the contract, defaults equal `limits.json`, CodeBuild on ARM
      with Docker, immutable repository emptied with the stack, least-privilege
      runtime role).
- [x] 0.8 Export golden `testdata/agent/export/deepagents-agentcore/expected/`
      (`Dockerfile` with `ARG AGENT_BASE_IMAGE`, `README.md` with the new
      AgentCore and security sections).
- [x] 0.9 `AWS_API_NOTES.md` «Despliegue en AgentCore Runtime».

### Tests that fail until the lanes land (expected)

| Test | Fails until | Why |
|---|---|---|
| Python `tests/unit/cli/test_cli_surface.py::test_cli_surface_matches_the_golden` | Track PY | the golden has the new options and commands |
| Python `tests/unit/test_agent_export.py` (`deepagents-agentcore` snapshot and the pinned-`FROM` check) | Track PY | the golden has the `ARG` line; the check must accept `ARG AGENT_BASE_IMAGE=<pinned>` + `FROM ... ${AGENT_BASE_IMAGE}` |
| TypeScript `tests/unit/agent-export.test.ts` (`deepagents-agentcore`) | Track TS | same golden |
| `python3 scripts/check_sdk_parity.py` and `--cli` | both lanes | new exports and commands exist in one SDK only until both land |
| TypeScript `tests/unit/public-exports.test.ts` | Track TS | the new exports |

Everything else (`gen_limits.py --check`, `gen_stack_assets.py --check`,
`gen_agent_template_assets.py --check`, `check_hygiene.py`, OpenSpec) passes
now.

## 1. Track PY (`clients/python/` only)

### 1.1 Shared code moved, not duplicated
- [x] 1.1.1 Move `StackObservation`, `StackCondition`, `classify_stack`,
      `USABLE_STACK_STATES`, `DELETING_STACK_STATE` and
      `IN_PROGRESS_STACK_SUFFIX` from `_account_setup/_domain.py` to a new
      `src/rayito/_stacks/_condition.py`; `_account_setup` imports them from
      there (no behaviour change; the account-setup tests still pass
      untouched).

### 1.2 Domain `src/rayito/_agentcore/_domain.py` and `_models.py` (pure)
- [x] 1.2.1 Constants from `_limits.py` only (no literal): regions, prefixes,
      session lengths, lifecycle range and defaults, quotas, timeouts, poll
      intervals, retention, marker, endpoint name, prices.
- [x] 1.2.2 `AgentCoreOptions` (frozen dataclass; fields of
      `contract.sdk.python.options`; validates types, not ranges) and
      `AgentCoreDeployPlan`, `AgentCorePlanStep`, `AgentCoreStatus`,
      `AgentCoreDeleteResult`, `AgentCoreApplyStep` in `_models.py`.
- [x] 1.2.3 `AgentCoreException(Exception)` in `rayito/exceptions.py` with
      `code: AgentCoreErrorCode` (the closed list of
      `contract.errorExitCodes`), `exit_code`, messages from
      `ERROR_MESSAGES` (the contract templates), never an ARN, URL, token or
      AWS text.
- [x] 1.2.4 Names: `agent_name_of(project)`, `validate_agent_name`,
      `runtime_name_for`, `stack_name_for`, `repository_name_for`,
      `build_project_name_for`, `source_key_for(name, tag)`,
      `runtime_description_for`, `runtime_log_group_for(runtime_id)`,
      `is_owned(listed)`.
- [x] 1.2.5 `image_tag_for(tree: Mapping[str, bytes]) -> str` (framing of
      `constants.imageTagDigest`), `client_token_for(request: Mapping) -> str`.
- [x] 1.2.6 `agentcore_session_id(session_id: str | None) -> str`
      (`sessionIdRules`; uuid source injected for tests).
- [x] 1.2.7 `bedrock_resource_arns(models, model_region, account, partition)`
      and `secret_resource_arns(names, region, account, partition)`
      (`model-arns.json`).
- [x] 1.2.8 `check_call_options(options: AgentDeployOptions, cli_flags:
      Mapping[str, object]) -> None` and `file_warnings(project) -> tuple[str, ...]`
      driven by one table mirroring `optionsTable` (order of the table
      description); `check_project(project)` for provider and network;
      `resolve_lifecycle`, `validate_tags` (counting `FIXED_TAG_KEYS`),
      `runtime_environment(project, region)` with `env_invalid`.
- [x] 1.2.9 `build_plan(project, options, agentcore, region, environ) ->
      AgentCoreDeployPlan` (offline; bucket from argument, project file or
      `RAYITO_BUCKET`, else `discover`).
- [x] 1.2.10 Apply decisions: `decide_stack(observation)`,
      `decide_image(stack_ready, image_observation, build_image)`,
      `check_image_size(size_bytes)`, `decide_runtime(listed, details,
      desired)`, `runtime_matches(details, desired)` (`apply-vectors.json`).

### 1.3 Ports, service, adapters
- [x] 1.3.1 `_ports.py`: `RuntimeControl` (`list_runtimes`, `get_runtime`,
      `create_runtime`, `update_runtime`, `delete_runtime`), `RuntimeInvoker`
      (`invoke`, `stop_session`), `ImageBuilder` (`start_build`,
      `get_build`), `ImageRegistry` (`describe_image`), `SourceStore`
      (`put_source`, `delete_source`, `bucket_region`), `RuntimeLogs`
      (`delete_log_group`), `AccountIdentity` (`account_id`); the stack goes
      through the existing `StackProvisioner`/`OptionalStacks`.
- [x] 1.3.2 `_stack.py`: `AGENTCORE_STACK_COMPONENT` (`StackComponent`,
      `agentcore-runtime`, the eight parameters and six outputs of the
      contract, `CAPABILITY_IAM`, `CostStatement`); **not** in
      `_stacks/_registry.COMPONENTS`.
- [x] 1.3.3 `_service.py` (`AgentCoreDeployer.apply(plan) ->
      AgentCoreFleet`, `AgentCoreDeleter.delete(name)`, `list_agents()`), and
      `_service_async.py` with the same surface (`asyncio.to_thread` around
      the sync adapters, like `deploy_with_async`); progress through
      `on_step` with `AgentDeployStep` and the contract progress keys; poll
      with injected `clock`/`sleep`.
- [x] 1.3.4 `_aws.py`: boto3 adapters with `LazyClient` and
      `client_config()`, only the parameters of `AWS_API_NOTES.md`; error
      translation by code from `awsErrorMap`; `sanitize_aws_error` on every
      cause; `InvokeAgentRuntime` read timeouts 900 s / 3600 s
      (`AGENTCORE_REQUEST_TIMEOUT_SECONDS`, `AGENTCORE_STREAM_MAX_SECONDS`)
      through a dedicated botocore `Config(read_timeout=...)`.
- [x] 1.3.5 Source zip: deterministic zip of `render_export(project,
      target="agentcore")` (fixed timestamps, sorted entries, `0o644`), uploaded
      with `PutObject` + `ChecksumSHA256`, deleted in `finally` after the build.

### 1.4 Public surface
- [x] 1.4.1 `_fleet.py` / `_fleet_async.py`: `AgentCoreFleet`,
      `AsyncAgentCoreFleet`, `AgentCoreFleetStream`,
      `AsyncAgentCoreFleetStream` with the members of `contract.sdk.python`;
      `invoke`/`stream` reuse `invocation_body`, `result_from_body`,
      `EventDecoder`, `ResultBuilder`; `connect(name, region=, session=)`.
- [x] 1.4.2 `_deploy_facade.py`: `target` and `agentcore` keyword-only on
      `deploy_agent`, `deploy_agent_async`, `plan_deploy` (`typing.overload`
      by `Literal` target for the return types); the MicroVM branch untouched.
- [x] 1.4.3 «Coste y activación» blocks (six entries, us-east-1 prices with
      the 2026-10-19 date and URLs) on `deploy_agent`, `deploy_agent_async`,
      `AgentCoreFleet`, `AgentCoreOptions`.
- [x] 1.4.4 Export in `rayito/__init__.py` (`__all__`): `AgentCoreFleet`,
      `AsyncAgentCoreFleet`, `AgentCoreFleetStream`,
      `AsyncAgentCoreFleetStream`, `AgentCoreOptions`, `AgentCoreStatus`,
      `AgentCoreDeleteResult`, `AgentCoreDeployPlan`, `AgentCoreException`.

### 1.5 Export and host (the image)
- [x] 1.5.1 `_agent/_export.py`: for `target == "agentcore"` only, the
      `ARG AGENT_BASE_IMAGE=<BASE_IMAGE_REFERENCE>` line before `FROM` and
      `FROM --platform=linux/arm64 ${AGENT_BASE_IMAGE}`; the README section of
      the golden; for frameworks in `AGENTCORE_ENV_CREDENTIAL_ADAPTERS`, the
      steps of `constants.export.awsCredentials` (sha from
      `AGENT_AWS_CREDENTIALS_REQUIREMENTS_SHA256`) and `host.json`
      `aws_env_credentials`. `docker` output byte-for-byte unchanged.
- [x] 1.5.2 Host (`_runner/agent_host/`, standard library only): when
      `aws_env_credentials` is present, run `python script` before each
      invocation with a 10 s timeout, parse the JSON, pass it as `aws_env` to
      the Node bridge request; on failure `agent_failed` with
      `detail_code="aws_credentials_unavailable"`; never log the values.
- [x] 1.5.3 Node bridge (`_assets/node_bridge.mjs`): apply `aws_env` to
      `process.env` for the invocation and restore afterwards.
- [x] 1.5.4 Regenerate `python3 scripts/gen_agent_template_assets.py` and add
      the golden case `testdata/agent/export/vercel-ai-agentcore/`
      (`case.json` from the `typescript/vercel-ai-sdk` sample shape, target
      `agentcore`; `RAYITO_UPDATE_SNAPSHOTS=1`).

### 1.6 CLI (`cli/agent.py`, new `cli/_agentcore.py`)
- [x] 1.6.1 Options appended exactly as `testdata/cli/surface.json` and
      `contract.commands` (help texts verbatim); defaults from SDK constants.
- [x] 1.6.2 `deploy --target agentcore`: build the plan, print it
      (`messages`), `--dry-run` stops, confirmation, apply with progress
      lines, final lines `messages.deployed`; `--json` documents with
      `json.planDocumentKeys`, `deployKeys`, error document like
      `rayito setup` (`json.errorDocument`).
- [x] 1.6.3 `invoke|status|stop --target agentcore` (`AgentCoreFleet.connect`;
      `--token-file` → `option_not_supported`; `stop` requires `--session-id`);
      `--session-id` on `stop` without the target → `option_requires_agentcore`.
- [x] 1.6.4 `list --target agentcore` and `delete <name> --target agentcore
      [--yes]` with `messages.list*`, `messages.delete*`, `listKeys`,
      `deleteKeys`.
- [x] 1.6.5 `--tag` parsed with `cli/stack.parse_pairs`.

### 1.7 Tests (names by behaviour, fakes only)
- [x] `tests/unit/test_agentcore_contract.py` (constants and messages equal
      the contract), `test_agentcore_plan_vectors.py`,
      `test_agentcore_apply_vectors.py`, `test_agentcore_model_arns.py`,
      `test_agentcore_session_ids.py`, `test_agentcore_image_tag.py`,
      `test_agentcore_invoke.py` (body without `session_id`, header, accept,
      decoders, error mapping), `test_agentcore_delete.py` (only owned
      resources), `test_agentcore_zero_cost.py` (default target and the plan
      create no AgentCore/CodeBuild/ECR client), `test_agentcore_signatures.py`
      (sync/async parity with `inspect.signature`),
      `tests/unit/cli/test_agentcore_cli.py` (dry run, `--json`, confirmation,
      exit codes), and the updated export tests.
- [x] Container test (optional, local Docker in CI only):
      `tests/container/test_agentcore_export_container.py` builds the
      `deepagents-agentcore` context with
      `--build-arg AGENT_BASE_IMAGE=...` and checks `/ping` and the session
      header.
- [x] e2e `tests/e2e/test_agentcore_deploy.py` + `agentcore_deploy_support.py`
      (section 4; never runs without `RAYITO_E2E=1` and `RAYITO_E2E_AGENTCORE=1`).

### 1.8 Changelog
- [x] `clients/python/CHANGELOG.md` `## [Unreleased]` → `Added`.

### Gates (Track PY)

```bash
cd clients/python
uv run pytest tests/unit
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
uv run pytest ../../scripts/tests -p no:cacheprovider
uv run --isolated --python 3.11 pytest tests/unit
uv run --isolated --python 3.13 pytest tests/unit
cd ../.. && python3 scripts/check_sdk_parity.py && python3 scripts/check_sdk_parity.py --cli   # green only after Track TS too
python3 scripts/gen_agent_template_assets.py --check
python3 scripts/check_hygiene.py $(git ls-files --others --exclude-standard)
```

## 2. Track TS (`clients/typescript/` only)

### 2.1 Shared code moved, not duplicated
- [ ] 2.1.1 Move `StackObservation`, `StackCondition`, `classifyStack`,
      `USABLE_STACK_STATES` and related constants from
      `src/account-setup/domain.ts` to `src/stacks/condition.ts`;
      account-setup imports them.

### 2.2 Domain `src/agentcore/domain.ts`, `models.ts` (pure; no `@aws-sdk/*`, no `node:` I/O)
- [ ] The same functions as 1.2.4-1.2.10 in camelCase
      (`agentcoreSessionId`, `imageTagFor` over `Map<string, Uint8Array>`,
      `bedrockResourceArns`, `checkCallOptions`, `fileWarnings`, `buildPlan`,
      `decideStack`, `decideImage`, `decideRuntime`, `runtimeMatches`);
      `AgentCoreOptions` with `idleSessionTimeoutMs`/`maxSessionLifetimeMs`
      (converted to whole seconds; not a multiple of 1000 →
      `lifecycle_invalid`); `AgentCoreError extends Error` in `src/errors.ts`
      (`code`, `exitCode`).

### 2.3 Ports, service, adapters
- [ ] `src/agentcore/ports.ts`, `service.ts`, `stack.ts`
      (`AGENTCORE_STACK_COMPONENT`, not in the registry), `aws.ts` with
      `loadOptionalSdkClient` for `@aws-sdk/client-bedrock-agentcore-control`,
      `@aws-sdk/client-bedrock-agentcore`, `@aws-sdk/client-codebuild`,
      `@aws-sdk/client-ecr`, plus the existing CloudFormation, S3, STS and
      CloudWatch Logs clients; `requestTimeout` 900000 / 3600000 ms for
      invoke and stream; errors by `name` through `awsErrorMap`,
      `sanitizeAwsError`.
- [ ] `package.json`: the four clients as optional peers (`^3.1138.0`) in
      `peerDependencies` + `peerDependenciesMeta` and in `devDependencies`;
      `pnpm install` updates `pnpm-lock.yaml`.
- [ ] Deterministic zip of the export tree (same entries and order as
      Python; the bytes need not be identical, the tag is computed on the
      tree).

### 2.4 Public surface
- [ ] `src/agentcore/fleet.ts`: `AgentCoreFleet`, `AgentCoreFleetStream`
      (`contract.sdk.typescript`), reusing `agent/deployment.ts` decoders.
- [ ] `src/agent/deploy.ts`: `target` and `agentcore` in
      `DeployAgentOptions`, overloads of `deployAgent` and `planDeploy` by
      target; MicroVM branch untouched.
- [ ] TSDoc «Coste y activación» on `deployAgent`, `AgentCoreFleet`,
      `AgentCoreOptions`; `scripts/cost-declarations/agentcore-deploy.json`.
- [ ] `src/index.ts` exports; `tests/unit/public-exports.test.ts`.
- [ ] `src/agent/export.ts`: the same additions as 1.5.1 (goldens decide).

### 2.5 CLI
- [ ] `src/cli/agentcore.ts` and the registrations in `src/cli/registry.ts`
      (options and help verbatim from the contract), output and JSON like 1.6;
      `--tag` with `cli/tokens.ts` `parsePairs`.

### 2.6 Tests
- [ ] `tests/unit/agentcore-contract.test.ts`, `agentcore-plan.test.ts`,
      `agentcore-apply.test.ts`, `agentcore-model-arns.test.ts`,
      `agentcore-session-ids.test.ts`, `agentcore-image-tag.test.ts`,
      `agentcore-invoke.test.ts`, `agentcore-delete.test.ts`,
      `agentcore-zero-cost.test.ts`, `agentcore-cli.test.ts`, the export
      tests (after rebasing on Track PY for `vercel-ai-agentcore`).
- [ ] e2e `tests/e2e/agentcore-deploy.e2e.test.ts` (section 4).

### 2.7 Changelog
- [ ] `clients/typescript/CHANGELOG.md` `## [Unreleased]` → `Added`.

### Gates (Track TS)

```bash
cd clients/typescript
pnpm install --frozen-lockfile   # after adding the peers: pnpm install, commit the lockfile
pnpm lint
pnpm typecheck
pnpm build
pnpm test
pnpm pack:check
python3 ../../scripts/check_docs_examples.py --typescript
cd ../.. && python3 scripts/check_sdk_parity.py && python3 scripts/check_sdk_parity.py --cli
python3 scripts/check_hygiene.py $(git ls-files --others --exclude-standard)
```

## 3. Track DOCS (no file under `clients/`, `infra/*.yaml`, `scripts/`, `testdata/`, `limits.json`, `AWS_API_NOTES.md`)

- [ ] D.1 `ARCHITECTURE.md`: ADR-031 «agentcore-deploy: despliega en Amazon
      Bedrock AgentCore Runtime», in Spanish, summarising `design.md` (context,
      decision D1-D13, consequences, alternatives, reversibility).
- [ ] D.2 Guide `docs/site/docs/guias/desplegar-en-agentcore.md` +
      `.en.md` (how-to): prerequisites (region list, bucket from `rayito
      setup`, caller IAM permissions list), deploy, invoke, session reuse,
      status, list, stop a session, delete, redeploy, `--tag` for SCPs,
      `--bedrock-model`, `--readable-secret`, lifecycle; the options table;
      «Coste y activación» box; «Seguridad: sin pasarela de secretos» admonition
      with the contract wording; tabs `=== "Python"` / `=== "TypeScript"`
      (examples compiled by `check_docs_examples.py`); nav entry and
      `nav_translations`.
- [ ] D.3 `despliega-tu-agente.md/.en.md` and `flota-de-agentes.md/.en.md`:
      a short section linking the new guide; `export --target agentcore` now
      has `AGENT_BASE_IMAGE`.
- [ ] D.4 Reference: `cli.md/.en.md` (the new options and commands, exit
      codes, JSON documents), `referencia/typescript.md/.en.md`, the Python API
      page entries (mkdocstrings), `referencia/errores.md/.en.md`
      (`AgentCoreException`/`AgentCoreError` codes), `limits.md/.en.md`
      (AgentCore quotas), `cost.md/.en.md` (the four price lines with source
      and date).
- [ ] D.5 `optional-features.md/.en.md`: one row for `--target agentcore`.
- [ ] D.6 `security.md/.en.md` and `SECURITY.md`: the security difference
      and the least-privilege role (what the runtime role can and cannot do;
      MMDSv2 always on).
- [ ] D.7 `infra/README.md`: `agentcore-runtime.yaml` (resources, parameters,
      outputs, not a catalog component).
- [ ] D.8 `README.md` / `README.es.md`: one line in the feature list.
- [ ] D.9 `docs/site/docs/novedades/` entry for the next minor (es + en).

### Gates (Track DOCS)

```bash
cd clients/python && uv run --group docs mkdocs build -f ../../docs/site/mkdocs.yml --strict
cd clients/python && uv run --group dev python ../../scripts/check_docs_examples.py --ruff --mypy --cli   # after Track PY merges
python3 scripts/check_docs_i18n.py
npx -y @fission-ai/openspec@1.10.0 validate --all --strict --no-interactive
python3 scripts/check_hygiene.py $(git ls-files --others --exclude-standard)
```

## 4. AWS acceptance (orchestrator, after the three lanes merge)

Region `us-east-1` (the samples' model is the `us.` inference profile of
Claude Haiku 4.5). Credentials from the maintainer's profile, never written.
Variables (documented generically in the test headers): `RAYITO_E2E=1`,
`RAYITO_E2E_AGENTCORE=1`, `AWS_REGION`, `RAYITO_BUCKET` (the setup bucket),
`RAYITO_E2E_RUN_ID` (8 lowercase characters), `RAYITO_E2E_TAGS` (`K=V,...`
for tag-enforcing SCPs).

### 4.1 Preconditions (read-only)
- [ ] Inventory before: `ListAgentRuntimes`, stacks `rayito-agentcore-*`, ECR
      repositories `rayito/agentcore/*`, CodeBuild projects
      `rayito-agentcore-*`, log groups `/aws/bedrock-agentcore/runtimes/` and
      `/aws/codebuild/rayito-agentcore-*`, S3 prefix `rayito/images/agentcore/`,
      IAM roles of those stacks, plus the MicroVM inventory of
      `release-and-aws.md`.
- [ ] Service Quotas: CodeBuild concurrent ARM small builds and AgentCore
      active sessions; Bedrock model access for the profile.
- [ ] IAM simulation of the caller for the actions of `AWS_API_NOTES.md`.

### 4.2 Matrix (one project per framework, all from `testdata/agent-samples/`)

Agent name `<sample-id>-<run-id>` through `[image].template` (≤ 41 chars).
No fixture is missing: the fifteen samples of 0.14 cover every framework.

| # | Sample | Framework | Language | Model (runtime role) | Extra checks |
|---|---|---|---|---|---|
| 1 | `python-langgraph-fastapi` | LangGraph | Python | Haiku 4.5 `us.` profile | tool use |
| 2 | `python-langchain-create-agent` | LangChain `create_agent` | Python | idem | tool use |
| 3 | `python-deepagents` | Deep Agents | Python | idem | tool use |
| 4 | `python-strands` | Strands | Python | idem | tool use; **redeploy with an env change → runtime `update`, one runtime by name** |
| 5 | `python-strands-harness` | Strands harness | Python | idem | tool use |
| 6 | `python-hermes` | Hermes | Python | idem | — |
| 7 | `python-flask-custom` | custom Flask | Python | idem | — |
| 8 | `python-agentcore-native` | Strands (AgentCore-native entry) | Python | idem | tool use |
| 9 | `typescript-langgraph-js` | LangGraph.js | TypeScript | idem | tool use |
| 10 | `typescript-deepagents-js` | Deep Agents JS | TypeScript | idem | tool use |
| 11 | `typescript-strands-ts` | Strands TypeScript | TypeScript | idem | tool use |
| 12 | `typescript-strands-harness-ts` | Strands harness TS | TypeScript | idem | tool use |
| 13 | `typescript-vercel-ai-sdk` | Vercel AI SDK | TypeScript | idem | env-credential helper path |
| 14 | `typescript-express-custom` | custom Express | TypeScript | idem | **redeploy with a code change → image `build` + runtime `update`** |
| 15 | `opencode` | OpenCode | config | idem | records whether OpenCode reads the role (risk in design) |

For every row: deploy (`--yes`, `--tag` from `RAYITO_E2E_TAGS`), status
`READY`, `invoke` returns `42`, `stream` returns `42` with events, a second
`invoke` with the returned session id keeps the session, redeploy unchanged =
three `reuse` and no build, `stop --session-id`, `delete --yes`, and the
role check: IAM `SimulatePrincipalPolicy` on the runtime role denies
`bedrock:InvokeModel` on another model and `secretsmanager:GetSecretValue`
without `--readable-secret`. Python e2e covers rows 1-8 and 15 plus the CLI;
TypeScript e2e covers rows 9-14 and one Python row (4) through
`deployAgent`. Plus one `--dry-run --json` per SDK and a `name_conflict` check
against a runtime created by hand without the marker (then deleted).

### 4.3 MicroVM backward compatibility
- [ ] `rayito agent deploy <sample> --dry-run` without `--target` equals the
      0.14 output (unit golden) and makes no AWS call.
- [ ] Deploy `python-strands` and `typescript-strands-ts` to MicroVMs with the
      default target (subset of `tests/e2e/test_agent_zero_config.py` and its
      TypeScript mirror), invoke, stop.
- [ ] `rayito setup --dry-run` unchanged.

### 4.4 Parallelism and limits
- At most 3 CodeBuild builds at a time (raise only if the quota allows),
  Python and TypeScript lanes in parallel with disjoint agent names; each
  lane's samples sequential after their build. Control-plane mutations stay
  far below 50 TPS; new sessions below 25/s.
- Hard cap: stop the run if more than 40 builds or 25 runtimes exist for the
  run id.

### 4.5 Cost estimate (us-east-1 list prices, 2026-10-19)
- CodeBuild: ~17 builds × ~8 min = ~136 min; 100 free minutes/month, the rest
  × $0.0034 ≈ **$0.12** (≈ $0.46 with no free tier).
- AgentCore runtime: ~60 sessions × (~20 s CPU at 1 vCPU + ~120 s × 2 GB
  memory until `stop`) ≈ 60 × ($0.0005 + $0.0006) ≈ **$0.07**.
- ECR: ~15 × 1.5 GB for one day ≈ **$0.08**; logs ≈ **$0.01**.
- Bedrock Haiku 4.5: ~120 invocations × ~3k tokens ≈ **$0.30**.
- MicroVM compatibility: two images and ~10 min of VMs ≈ **$0.20**.
- **Total ≈ $0.80; cap $5.**

### 4.6 Cleanup (only what this run created)
- [ ] Every test deletes its agent in `finally` (`delete --yes`); afterwards a
      sweep deletes only runtimes, stacks, repositories, projects, log groups
      and S3 objects whose names contain the run id; the MicroVM images of the
      run follow `release-and-aws.md` (`RAYITO_E2E_RUN_ID`).
- [ ] Inventory after; the difference with 4.1 must be zero. Anything that
      existed before stays, even if it looks like Rayito's.
- [ ] Record in `AWS_API_NOTES.md` (new Q rows): image size measured by ECR
      vs what AgentCore accepts, cold start to first `invoke`, build minutes
      per framework, OpenCode credentials result.

## Open questions (defaults chosen; change only if the maintainer says so)

- Region list = AgentCore microVMs ∩ CodeBuild `ARM_CONTAINER` (21).
- Image retention 5, build log retention 14 days, build timeout 30 min,
  ready timeout 900 s, delete timeout 600 s (`limits.json`).
- Tags on an existing runtime are not updated (no `TagResource`).
- `rayito agent list` and `delete` require `--target agentcore` explicitly.
- CDK construct deferred to a follow-up change (design D12).

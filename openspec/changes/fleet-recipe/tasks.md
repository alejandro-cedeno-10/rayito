## How to use this file

The work is split into five groups. Each group cuts its branch from
`feat/sdk-017` and opens one pull request back into it. Nothing goes to
`main` from a group branch, and only `feat/sdk-017` opens the single PR to
`main`. Merge order: G1, G2, G3, G4, G5 (G5 after G2). Delete each group
branch and worktree after its merge.

Before writing code, each group reads:

- `proposal.md`;
- `design.md` (decisions and «Integration contract»);
- its spec deltas;
- `testdata/fleet-recipe/contract.json`;
- ADR-034;
- `.claude/skills/rayito-engineering/SKILL.md`, `CLAUDE.md` and
  `openspec/project.md`.

Hard rules:

- **Compatibility.** Backward compatible with 0.16. An explicit `project=`
  keeps the exact call trace, existing tests pass unchanged, and a group
  only adds tests.
- **Code.** Hexagonal layers; Python and TypeScript parity over the shared
  vectors; named constants in `limits.json`.
- **Cost and secrets.** Cost features stay off by default, except the
  recipe as ADR-034 D1 records. No secret value, token or secret name in
  logs, errors or `repr`. No AWS parameter that is absent from
  `AWS_API_NOTES.md`.
- **Docs and examples.** Spanish docs with `.en.md` pairs. Generic examples
  only (`123456789012`, `example.com`, `my-agent`).
- **Git.** Commit with `git commit -s -S` using Conventional Commits. No AI
  attribution of any kind (no co-author trailers, session links or
  generated-by footers). Never `git stash`, never push to `main`, and use
  pnpm only.

## 0. Shared prerequisites (done in this change; no group edits these files)

- [x] 0.1 ADR-034 in `ARCHITECTURE.md`.
- [x] 0.2 `AWS_API_NOTES.md` section «Receta de la flota y claves de corta
      duración de Bedrock», with Q191–Q194 allocated.
- [x] 0.3 Integration contract in `design.md` and
      `testdata/fleet-recipe/contract.json`.
- [x] 0.4 Vector skeletons: `testdata/fleet-recipe/{recipes,renewal,env-rejection}.json`.
- [x] 0.5 Spec deltas validated with
      `pnpm dlx @fission-ai/openspec@1.10.0 validate fleet-recipe --strict --no-interactive`.
- [ ] 0.6 (G5, first commit) Verify the SSM names against the
      `@aws-sdk/client-ssm` `.d.ts` and record the version in
      `AWS_API_NOTES.md`. Botocore is already verified.

## G1. Agent host logging (`feat/sdk-017-host-logging`, items 1 and 7)

- [ ] G1.1 `QUIET_HTTP_LOGGERS` in `agent_host/serve.py`, applied in
      `__main__.serve()` after `basicConfig`.
- [ ] G1.2 `log_request` tolerates a missing `command`/`path`.
- [ ] G1.3 Tests: `test_agent_host_logging.py` (an httpx INFO record is
      dropped, while the host's own records stay) and a malformed request
      line in `test_agent_host_serve.py` (400, no traceback, then `/ping`
      works).
- [ ] G1.4 Regenerate `host-assets.gen.ts`. The TS asset-sync test passes.
- [ ] G1.5 Docs: `security.md`/`.en.md` (logging hygiene) and
      `operacion/solucion-de-problemas.md`/`.en.md`. CHANGELOG (Python and
      TypeScript: host assets ship in both).

## G2. Secret creation and setup (`feat/sdk-017-secrets-setup`, items 2, 3 and 4)

- [x] G2.1 `AgentProject.gateway_presets` / `gatewayPresets`: filled by the
      file reader and by detection, excluded from the serialised document,
      with existing project vectors byte-identical.
- [x] G2.2 `secret_value_from_env(..., preset=)` and messages by
      `preset or gateway`. `ensure_model_secrets` passes the preset.
- [x] G2.3 Detection writes the gateway secret relative to `rayito/`
      (`GATEWAY_SECRET_NAME_TEMPLATE`) and the deploy facade builds the store
      with `GATEWAY_SECRET_PREFIX`; a 0.16 doubled secret is reused.
      Vector `testdata/agent-deploy/create-secret.json` covers a gateway
      `model` with preset `bedrock` and the exact name, with no doubled
      prefix.
- [x] G2.4 Setup: the `capsImage` step and `skip` action;
      `SetupRequest.caps_image` / `capsImage`; one download reused;
      `additionalOsCapabilities=["ALL"]`; `--no-caps-image` in both CLIs;
      plan vectors and `cli-cases.json` updated.
- [x] G2.5 Tests in both SDKs: preset lookup, name, plan with and without
      caps, and service publishing both images from one artifact.
- [x] G2.6 Docs: `cli.md`/`.en.md` (setup, `--create-secret`),
      `primeros-pasos/configurar-aws.md`/`.en.md`,
      `guias/despliega-tu-agente.md`/`.en.md`, `cost.md`/`.en.md`.
      CHANGELOG entries.

## G3. OpenCode zero-config (`feat/sdk-017-opencode`, item 5)

- [x] G3.1 Read `AGENTS.md` into `spec.instructions` when the project
      defines none.
- [x] G3.2 Map the `opencode.json`/`.jsonc` `permission` block to
      `AgentPermissions`. Unknown keys give `opencode_permission_ignored`.
- [x] G3.3 Detection evidence for both files, never their text. New case
      `testdata/agent-detection/cases/opencode-agents-md/`. Regenerate the
      detection rules if `agent-detection.json` changes.
- [x] G3.4 Tests in both SDKs (extend `*opencode*` tests, add
      zero-config-files tests).
- [x] G3.5 Docs: `guias/agente-proveedores.md`/`.en.md` and
      `funciones-opcionales/templates-de-agente.md`/`.en.md`. CHANGELOG.

## G4. TypeScript e2e secret names (`feat/sdk-017-e2e-secrets`, item 6)

- [ ] G4.1 `secretName(scope, kind)` returns a name unique per run, test and
      call. Update every caller, including `e2e-${RUN_ID}/ts-llm`.
- [ ] G4.2 Each test deletes only its own secrets.
- [ ] G4.3 Unit test `tests/unit/e2e-secret-names.test.ts`. CHANGELOG (TS,
      «Tests»).

## G5. Fleet recipe, warm gateways, scale-in, renewal and status (`feat/sdk-017-fleet-recipe`, items 8–12)

- [ ] G5.1 `limits.json` keys from the contract; `gen_limits.py`.
- [ ] G5.2 Pure recipe domain (`_fleet_recipe.py` / `fleet-recipe.ts`):
      build from project, flags, image, scaling and presets; encode as JSON
      or envelope; decode; validate. Fill `value` in `recipes.json` and
      compare it byte-for-byte in both SDKs.
- [ ] G5.3 SSM adapter (`_fleet_recipe_aws.py` / `fleet-recipe-aws.ts`):
      only the contract operations and parameters, with errors mapped by
      code. Add the allowed-operations test.
- [ ] G5.4 `deploy_agent*` saves the recipe (`save_recipe`), and the plan
      shows `recipe: save|off`.
- [ ] G5.5 `AgentFleet.connect(use_recipe=)` reads it and rebuilds the
      project, image version, scaling and renewal (sync, async and TS).
- [ ] G5.6 `FleetStatus.scaling_source`; CLI status text and JSON.
- [ ] G5.7 `stop(keep_recipe=)`, `rayito agent stop --keep-recipe`.
- [ ] G5.8 `apply_gateways` helper used by `SandboxPool.take` and by the
      fleet when it opens a parked slot (sync, async and TS).
- [ ] G5.9 `scale_in_after_seconds` / `scaleInAfterMs` and
      `--scale-in-after` in both CLIs.
- [ ] G5.10 `--env` secret-looking rejection with the contract message.
- [ ] G5.11 Gateway key renewal (`_gateway_keys.py` / `gateway-keys.ts`),
      driven by the `renewal.json` decisions; optional dependency (extra and
      peer); «Coste y activación» block and cost declaration.
- [ ] G5.12 `infra/iam.yaml` launcher statement `AgentRecipe`. Add it to the
      stack tests and the cost statements if they list actions.
- [ ] G5.13 Zero-cost traces: no SSM, `DescribeSecret` or `PutSecretValue`
      call without `deploy_agent()`, with `project=`, with
      `save_recipe=False`, and without renewal.
- [ ] G5.14 Unit tests in both SDKs (see design §4). Env-gated e2e for warm
      slot plus reconnect plus status plus stop.
- [ ] G5.15 Docs: `guias/flota-de-agentes.md`/`.en.md`,
      `funciones-opcionales/pasarela-de-secretos.md`/`.en.md`,
      `optional-features.md`/`.en.md`, `cost.md`/`.en.md`,
      `operacion/iam.md`/`.en.md`, `cli.md`/`.en.md`,
      `concepts.md`/`.en.md`. CHANGELOG.

## Z. Integration on `feat/sdk-017`

- [ ] Z.1 Regenerate the generated files (limits, detection, host assets,
      CLI surface) and run the full gates in both SDKs plus Rust untouched.
- [ ] Z.2 `novedades/0.17.0.md`/`.en.md`, `mkdocs.yml` nav and
      `nav_translations`, and `MILESTONES.md`.
- [ ] Z.3 Review sweep (`reference/review-checklist.md`): leaks first, then
      hexagonal design, clean code, parity, cost and gates.
- [ ] Z.4 AWS acceptance (infra changes, so full regression; the 60-minute
      rotation test is skipped): Q191–Q194 recorded. Delete only what the
      run created.
- [ ] Z.5 One PR to `main` with CI and CodeQL green, left open for review.
      Delete the intermediate branches.
- [ ] Z.6 Archive this change after acceptance.

## How to use this file

Four feature branches, each cut from `feat/agnostic-deploy` and each opening
one pull request back into it (nothing goes to `main` from a feature branch,
and nothing merges without the maintainer's review): **A**
`feat/agent-model-optional`, **B** `feat/agent-structured-input`, **C**
`feat/agent-secret-env`, **D** `feat/agent-langgraph-api`. Merge order A, B,
C, D; D's tasks marked «after B» start once B is merged. The AWS acceptance
runs once on the tip of `feat/agnostic-deploy` after the four merges
(section Z).

Every branch reads `proposal.md`, `design.md` (decisions and the integration
contract), its spec deltas, `testdata/agnostic-deploy/contract.json`, its
vectors, ADR-033, `.claude/skills/rayito-engineering/SKILL.md`, `CLAUDE.md`
and `openspec/project.md` before writing code. Hard rules: backward
compatibility (no existing behaviour, flag, project-file key, API or default
changes; existing tests pass unchanged; only add tests); hexagonal layers;
Python/TypeScript parity over the shared vectors; named constants (no magic
numbers; shared limits in `limits.json`); cost features off by default with
the «Coste y activación» block; Spanish docs with `.en.md` pairs; errors
mapped by code, never by message; no secret, environment value, prompt,
input or output in files, logs, errors or `repr`; no AWS parameter absent
from `AWS_API_NOTES.md`; generic examples only (`123456789012`,
`https://llm-gateway.example.com`, a LangGraph graph with input
`{"ticket_id": ...}`); commits `git commit -s -S`, Conventional Commits, no
AI attribution of any kind (no co-author trailers, session links or
generated-by footers). Never `git stash`; never push to `main`. Each branch
follows merge rules M1-M8 of `design.md` and ticks only its own section here.

## 0. Shared prerequisites (done in this change, do not edit in a branch)

- [x] 0.1 ADR-033 in `ARCHITECTURE.md` (amends ADR-031 D10 and D11 for the
      opt-in external model).
- [x] 0.2 Integration contract (`design.md` «Integration contract») and its
      machine-readable names in `testdata/agnostic-deploy/contract.json`.
- [x] 0.3 Vector skeletons: `testdata/agent-model-optional/requirement.json`,
      `testdata/agent-deploy/invocation-bodies.json`,
      `testdata/agent-secret-env/{parse,expand,spec,reserved}.json`,
      `testdata/agent-langgraph-api/{requests,responses}.json`.
- [x] 0.4 Spec deltas validated with
      `npx -y @fission-ai/openspec@1.10.0 validate agnostic-agent-deploy --strict`.
- [x] 0.5 Numbers pre-allocated: T38 (C); `AWS_API_NOTES.md` Q187, Q188 (C),
      Q189 (D), Q190 (A).

## A. Model optional when not Bedrock (`feat/agent-model-optional`)

- [x] A.1 `AgentModel` / `AgentModelOptions`: `id` optional, `id_env`,
      `base_url_env`, `api_key_env` appended; `effective_small_model` optional;
      `_read_model`, `_model_to_dict`, schema.json (4.3); existing vectors
      byte-identical.
- [x] A.2 `MODEL_ID_REQUIRED_PRESETS` derived from the gateway presets;
      `require_model_id` / `requireModelId` over
      `testdata/agent-model-optional/requirement.json` (MicroVM rows).
- [x] A.3 `modelFromCode` in `agent-detection.json`, regenerate; base URL
      evidence turns `openai` into `openai-compatible`; model variable names
      from `.env.example`, getenv/process.env and `langgraph.json` `env` keys;
      pydantic-settings mapping; `model_envs` and `model_id_from_code`.
      Test: the report holds no value (extend
      `test_the_report_holds_no_file_content_or_secret` with a new test).
- [x] A.4 `DetectOverrides.model_id`, `model_provider`, `model_base_url`;
      flags `--model-id`, `--model-provider`, `--model-base-url` on
      `agent detect` / `agent deploy` in both CLIs (4.18, 4.19).
- [x] A.5 MicroVM wiring: `standard_model_envs` exports the `*_env` names;
      `RAYITO_MODEL_ID` omitted without id; `strands` / `deepagents`
      `build_context` lazy; `factoryContext` with `model: null`; telemetry
      without `gen_ai.request.model`; `opencode`, `hermes`, defaults without
      entrypoint raise `missing_key model.id`.
- [x] A.6 AgentCore: `AgentCoreOptions.external_model`, `--external-model`,
      option row and label (4.5); `check_provider`, `bedrock_models_of`,
      `check_iam_inputs` conditional; `external_model_environment`;
      `runtime_environment` and `security_keys` final forms (4.16);
      `externalModel` security line; `model_required` raised in `build_plan`;
      AgentCore rows of `requirement.json`; new plan vectors (M5).
- [x] A.7 CDK prop `externalModel` / `external_model` (4.22) with a synth test
      that has no Bedrock statement.
- [x] A.8 Docs (4.23), CHANGELOG (Añadido, and «Cambiado» for the type-only
      widening of `AgentModel.id`), `AWS_API_NOTES.md` Q190 placeholder for
      the acceptance.
- [x] A.9 Gates: Python and TypeScript unit suites, `ruff`, `mypy --strict`,
      Biome, typecheck, build, `pack:check`, `check_sdk_parity.py --cli`,
      generators `--check`, `mkdocs --strict`, `check_hygiene.py`, OpenSpec
      `--strict`.

## B. Structured input and graph selection (`feat/agent-structured-input`)

- [x] B.1 `select_graph` / `selectGraph` in `_detect_entry.py`;
      `DetectOverrides.graph`; `--graph` on `agent detect` / `agent deploy`;
      messages `ambiguous_graph`, `graph_not_found`, `graph_conflict`;
      detection cases (B list of design §3).
- [x] B.2 `[runtime] graph` (schema 4.3, `_read_runtime_extras`,
      `_runtime_to_dict`, `AgentProject.graph`), resolved to `entrypoint`
      before the runtime is built; `_complete_runtime` and `_project_file.py`
      keep the file's graph; project vectors.
- [x] B.3 Host: `Request.input`, `parse_request`, `parse_invocation`
      (`hostCases` of `invocation-bodies.json`), `output` in the JSON body and
      the `done` event, `agentServiceMaxOutputBytes`.
- [x] B.4 Driver deepagents/langgraph: `_messages_run` unchanged (test: same
      `stream` kwargs as today), `_structured_run`, `input_kind`,
      `validate_input`, output filter and serialization, `detail_code`s.
- [x] B.5 Node bridge `runLanggraph` with `getInputJsonSchema` when present.
- [x] B.6 SDKs: `invoke` / `stream` with `input` on every deployment and
      fleet class (sync and async), `require_prompt_or_input`,
      `invocation_body` (`cases`), `result_from_body` (`resultCases`),
      `AgentResult.output`, `Done.output`, `host_too_old`; TS overloads and
      the parity entry (4.21).
- [x] B.7 CLI `rayito agent invoke`: optional `PROMPT`, `--input`,
      `--input-file` (stdin with `-`), exclusivity, `print_result` with
      `output`; golden regenerated (M4).
- [x] B.8 Sample `testdata/agent-samples/python/langgraph-structured/`
      (graphs `triage` and `echo`, no `agent`, deterministic nodes, no LLM).
- [x] B.9 Docs (4.23) and CHANGELOG.
- [x] B.10 Gates as A.9.

## C. Secrets Manager values as environment variables (`feat/agent-secret-env`)

- [x] C.1 Domain `SecretEnvRef`, `SecretEnvSource`, parsing (flags and
      tables), expansion, reserved names, spec document; exports (4.20);
      vectors `parse.json`, `expand.json`, `reserved.json`, `spec.json`;
      limits (4.1).
- [x] C.2 Project: fields, readers and `to_dict` (4.6), schema (4.3),
      `conflict` with `[env]`; project vectors.
- [x] C.3 Host: `secret_env.py`, the `serve()` call before `create_driver`,
      fail-closed codes, `count=N` log, `extra_values` redaction; tests with a
      fake helper (order, every code, spec popped, nothing leaked).
- [x] C.4 AgentCore: `secret_env_environment`, union into readable secrets,
      `secretEnv` security line, plan `secret_env_names`, option rows (4.5);
      export adds `aws_secret_env.py` and `host.json` `secret_env` only when
      used (existing export vectors byte-identical); CDK lines (4.22).
- [x] C.5 IAM least-privilege test (fails if widened): `ReadSecrets` action
      exactly `secretsmanager:GetSecretValue`, resources exactly the expected
      ARNs, no `*` beyond the `-??????` suffix, no `secretsmanager:*`,
      `ListSecrets` or `kms:*`; same for the CDK template.
- [x] C.6 MicroVM: values resolved with the caller's credentials and passed
      as `secrets=` in `_project_service.serve()` (sync, async, TS); fleets
      and pool slots covered.
- [x] C.7 Flags `--secret-env`, `--secret-env-from`, `--check-env`,
      `--create-secrets-from` (confirmation with names only, `--yes`,
      `needs_confirmation`); `required_env_names`; `env_missing`.
- [x] C.8 Zero-cost test extended: without the feature no `secretsmanager`
      client is created, the export is identical and the golden trace does
      not change (both SDKs).
- [x] C.9 Docs: new guide, `secrets`, `security` (T38), `SECURITY.md`,
      `optional-features` row, «Coste y activación» blocks (Python docstrings,
      TSDoc, `cost-declarations/secret-env.json`), `AWS_API_NOTES.md` Q187
      (and Q188 placeholder), CHANGELOG.
- [x] C.10 Gates as A.9.

## D. `langgraph-api` protocol (`feat/agent-langgraph-api`)

- [x] D.1 `HttpApp.assistant_id`, protocol value, validation, `to_mapping`,
      `hides_own_routes`, `_app_options` key tuple (4.10); TS mirror.
- [x] D.2 Driver branch: body, thread derivation (`requests.json`
      `threadCases`, prompt rows of `bodyCases`), reading
      (`responses.json`), `graph_interrupted`, health `/ok`, own ready wait;
      limits (4.1); fake loopback server tests.
- [x] D.3 Detection with `--protocol langgraph-api`: launcher, kind
      `custom`, `assistant_id` from `preferKeys`, error without
      `langgraph.json`, warnings; `protocol_ignored` unchanged for any other
      protocol; cases (D list of design §3).
- [x] D.4 Child environment `LANGGRAPH_CLI_NO_ANALYTICS=1`,
      `LANGSMITH_TRACING=false` unless set.
- [x] D.int.1 (after B) `Request.input` goes as `input` (structured row of
      `bodyCases`).
- [x] D.int.2 (after B) `assistant_id` from `select_graph`, honouring
      `--graph`.
- [x] D.5 Docs (4.23: experimental, dev server, Elastic-2.0 license of
      `langgraph-api`, not installed by Rayito), CHANGELOG.
- [x] D.6 Gates as A.9.

## Z. Integration and acceptance (on `feat/agnostic-deploy` after A-D merge)

- [x] Z.1 Every generator with `--check`, the CLI golden, parity, and the
      full Python and TypeScript suites with no existing test edited.
- [x] Z.2 Review sweep: leak sweep of the diff and
      `reference/review-checklist.md`.
- [x] Z.2b Second review follow-ups: secret values read from a dedicated
      pipe (never the helper's stdout) and no JSON error kept in the
      exception context; CRLF and control characters normalised in the
      redacted values; `__proto__` keys kept in the TypeScript secret maps;
      own-key input validation in the Node bridge and plain objects only in
      the TypeScript `input`; integer accepts `3.0`; pydantic and dataclass
      outputs; `langgraph-api` output without repeated text and strict JSON
      text; the host's `400` says `accepts_input`; the whole body measured
      with `input`; the model id checked when `AgentProject` is built; the
      TypeScript `--create-secrets-from` prompt; the old exit code for a
      prompt in `rayito agent invoke` (shared vectors in
      `testdata/agent-deploy/input-validation.json`,
      `invocation-bodies.json`, `agent-langgraph-api/responses.json` and
      `agent-secret-env/`).
- [ ] Z.3 Local e2e with Docker (host image), including the
      `langgraph-structured`, `secret-env-echo` and `langgraph-api-echo`
      samples and the regression of `langgraph-fastapi` with a prompt.
- [x] Z.4 AWS acceptance (full regression tier: runtime and host change):
      MicroVM and AgentCore for A (`--model-provider openai-compatible
      --model-base-url https://llm-gateway.example.com`, external model with no
      Bedrock statement), B (`--graph triage`, `--input`, `invalid_input`,
      `input_required`, session continuity), C (plain and JSON secrets,
      denied secret fails closed, values absent from `GetAgentRuntime` and
      logs, rotation seen by a new session), D (two invokes, same thread,
      cold start measured); record Q187-Q190; delete only what the run
      created. Recorded in `test_agnostic_deploy.py` /
      `agnostic-deploy.e2e.test.ts`; Q188 stays pending (not isolated). The
      acceptance found that `langgraph dev` could not start in the read-only
      app dir; fixed in the host (D5.2) and measured again.
- [ ] Z.5 `openspec archive agnostic-agent-deploy --yes` after acceptance.

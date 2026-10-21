## Context

This design is ADR-033 in `ARCHITECTURE.md`. It closes the decisions of four
features that are implemented in parallel on four branches, and fixes the
integration contract that lets those branches merge mechanically. Names that
code must use verbatim are normative in `testdata/agnostic-deploy/contract.json`;
this document mirrors them and adds where each addition goes.

| Id | Feature | Branch | ADR | Vectors |
|---|---|---|---|---|
| A | Model optional when not Bedrock | `feat/agent-model-optional` | ADR-033 D2 | `testdata/agent-model-optional/` |
| B | Structured input and graph selection | `feat/agent-structured-input` | ADR-033 D3 | `testdata/agent-deploy/invocation-bodies.json` |
| C | Secrets Manager values as environment variables | `feat/agent-secret-env` | ADR-033 D4 | `testdata/agent-secret-env/` |
| D | `langgraph-api` protocol (experimental) | `feat/agent-langgraph-api` | ADR-033 D5 | `testdata/agent-langgraph-api/` |

Hard constraints for every branch: backward compatibility (existing tests
pass unchanged, only tests are added; new things are opt-in or additive);
hexagonal layers; Python/TypeScript parity over the shared vectors; named
constants; cost features off by default with the «Coste y activación» block;
Spanish docs with `.en.md` pairs; error mapping by code, never by message;
nothing secret in files, logs, errors or `repr`; generic examples only
(`123456789012`, `https://llm-gateway.example.com`, a LangGraph graph with
input `{"ticket_id": ...}`); commits `git commit -s -S`, Conventional Commits,
no AI attribution of any kind.

## Goals / Non-Goals

**Goals:** deploy a LangGraph-style agent that chooses its model by
configuration, takes a structured input and reads secrets from Secrets
Manager, without code changes, on MicroVM and AgentCore; serve a project
that already speaks the LangGraph Platform API.

**Non-Goals:** streaming over `langgraph-api`; `langgraph up` or the
standalone Agent Server; human-in-the-loop; structured input for
`sbx.agent.run()` inside a sandbox; KMS customer keys, cross-account secrets
and secret env in the Docker export (phase 2); auto-detecting
`langgraph-api`; host-side injection of the external model key other than
through C.

## Decisions

### D1. Integration (see «Integration contract» below)

Four branches from `feat/agnostic-deploy`, one PR each into it, merged in
the order A, B, C, D. D depends on B for two tasks (D.int.1, D.int.2) that
start after B merges. The acceptance in AWS runs once on the tip of
`feat/agnostic-deploy` after the four merges.

### D2. Model optional when not Bedrock (A)

1. `[model] id` is optional in the schema and in `_read_model`. Three
   optional keys hold variable **names**: `id_env`, `base_url_env`,
   `api_key_env`, validated with the existing environment-name pattern. A
   name that also appears in `[env]` is `conflict`.
2. `AgentModel(provider, id, gateway, region=None, base_path="",
   prompt_caching=True, id_env=None, base_url_env=None, api_key_env=None)`;
   positions do not change. `id` is `str | None` (TS `id?: string`). Chosen
   over a sentinel id (a magic value every reader would need to know); the
   CHANGELOG states it under «Cambiado» as a type-only widening.
   `effective_small_model` returns `str | None`.
3. `_model_to_dict` emits `id` only when not `None` and the `*_env` keys only
   when set, so every existing project serializes byte for byte the same.
4. `MODEL_ID_REQUIRED_PRESETS = ("bedrock", "gemini")` is derived from the
   presets whose builder takes `models` (`_GATEWAY_PRESETS`), not repeated as
   a literal. One pure function `require_model_id(project)` (TS
   `requireModelId`) decides, with the matrix of
   `testdata/agent-model-optional/requirement.json`: the id is required for
   those presets, and for `opencode`, `hermes`, and `deepagents` / `strands`
   without `entrypoint` (`missing_key model.id`). Detection keeps
   `not_detected model.id` for the required presets.
5. Detection (`agent-detection.json` → `modelFromCode`, common regex subset):
   `clientBaseUrl` per language turns an `openai` provider detected by
   dependency into `openai-compatible` (replaced, not added, so
   `several_providers` is not triggered); `modelEnvPattern` and
   `baseUrlEnvPattern` find variable **names** in `.env.example`, in
   `os.getenv` / `os.environ` / `process.env` calls and in the keys of the
   `env` map of `langgraph.json`; pydantic-settings fields map to
   `UPPER(env_prefix + field)`. Values are never read. When no id literal is
   found and the preset is not in `MODEL_ID_REQUIRED_PRESETS`, the bridge
   emits `[model]` without `id`, with the `*_env` names found, and the warning
   `model_id_from_code`. `DetectionReport.model_envs` is emitted only when not
   empty and only on that branch, so no existing `expected.json` changes.
6. Overrides `DetectOverrides.model_id`, `model_provider` (a preset name,
   `invalid_value model.provider` otherwise), `model_base_url` (becomes
   `gateways.<preset>.upstream`); flags `--model-id`, `--model-provider`,
   `--model-base-url` on `agent detect` and `agent deploy`. With a project
   file they behave exactly like `--entry` does today.
7. MicroVM wiring: `standard_model_envs` also exports `base_url_env` = gateway
   URL + `base_path`, `api_key_env` = `MODEL_CREDENTIAL_PLACEHOLDER`, and
   `id_env` = id when known. `RAYITO_MODEL_ID` is omitted when the id is
   `None`. Drivers `strands` and `deepagents` build the model only when an id
   is known (request or config); otherwise `ctx.model = None`. The Node bridge
   sets `ctx.model` to `undefined` for `null`. Telemetry omits
   `gen_ai.request.model` without an id.
8. AgentCore opt-in `AgentCoreOptions.external_model = False`
   (`--external-model`, CDK `externalModel`). With it: `check_provider` does
   not raise for a non-Bedrock provider; `bedrock_models_of` leaves out
   `model.id` when the provider is not `bedrock`; `check_iam_inputs` checks
   `model.id` only for `bedrock`; `external_model_environment(project)` adds
   `base_url_env = upstream + base_path` (not a secret) when both are known.
   The key never goes to `environmentVariables`; the code reads it with the
   role (`--readable-secret`) or C injects it (`--secret-env`). Security line
   `externalModel`. Without the opt-in, `provider_unsupported` as today.
9. `model_required` is raised in exactly one case, in `build_plan` before
   `check_iam_inputs`: target `agentcore`, provider `bedrock`, `model.id is
   None` and no `bedrock_models`. Message and exit code 2 unchanged; only
   reachable from new (id-less) files or detections.

### D3. Structured input and graph selection (B)

1. Graph precedence: `--entry` / `[runtime] entrypoint` (unchanged) →
   `--graph` / `[runtime] graph` / `DetectOverrides.graph` → one graph → the
   `agent` key (`preferKeys`, unchanged) → `ambiguous` (`runtime.entrypoint`,
   message `ambiguous_graph` listing names up to `maxCandidatesListed`). An
   unknown name is `invalid_value runtime.graph` (`graph_not_found`); `graph`
   with `entrypoint` is `conflict runtime.graph` (`graph_conflict`); `graph`
   on a kind other than `langgraph` is `invalid_value runtime.graph`.
   Selection is the pure function `select_graph(graphs, requested)` (TS
   `selectGraph`) in `_detect_entry.py`, returning `(name, entry)`; D reuses
   it. `graph` is resolved into `entrypoint` before the runtime is built, so
   the driver configuration and its hash do not change. The report carries
   `options.graph` only when a graph was chosen.
2. Request (`rayito.agent-service/1`, additive): exactly one of `prompt`
   (non-empty string) or `input` (JSON object). Both or neither is
   `400 invalid_request`; every other rule of `parse_invocation` is
   unchanged. `Request.input: Mapping[str, Any] | None = None` is appended
   after `aws_env`.
3. Host, deepagents/langgraph driver, only with an `entrypoint`: `execute`
   splits into `_messages_run` (today's body, unchanged, including the
   `build_context(config, request.model or config["model"])` call) and
   `_structured_run`. `input_kind(graph)` calls `get_input_jsonschema()` in a
   `try`; the graph is a message graph when the schema has
   `properties.messages` or when introspection fails. `prompt` against a
   structured graph is `protocol_error/input_required`. `input` against a
   message graph is passed as is (history prepended to `messages` when there
   is a session). `input` against a structured graph is checked with the
   standard library only (object, `required` present, no extra keys when
   `additionalProperties: false`, top-level types) → `invalid_input`; a
   pydantic `ValidationError` raised by the graph is classified by module and
   class name as `invalid_input`. The structured run passes
   `config={"recursion_limit": RECURSION_LIMIT, "configurable":
   {"thread_id": session_id}}` and `stream_mode=["messages", "updates",
   "values"]`; the last `values` is filtered to
   `get_output_jsonschema()["properties"]` and serialized (LangChain messages
   with `messages_to_dict`), else `output_not_serializable`, capped by
   `agentServiceMaxOutputBytes`. Sessions keep the `rda_` id.
4. Response: the JSON body and the `done` event gain `output` only when the
   run produced one. No new event type. `text` is empty unless the graph
   emitted AI messages. `input` and `output` are never logged.
5. Node bridge `runLanggraph`: the same steps when `getInputJsonSchema`
   exists (LangGraph.js ≥ 1.1); otherwise the prompt path as today.
6. SDK: `invoke(prompt=None, session_id=None, model=None, reasoning=False, *,
   input=None)` and `stream` alike on `AgentDeployment`, `AgentFleet`,
   `AgentCoreFleet` and the async twins; `require_prompt_or_input` in
   `_stream_base.py` enforces exactly one, an object, and the request size.
   TypeScript: overloads `invoke(prompt, options?)` and
   `invoke({ input }, options?)` (declared in `check_sdk_parity.py`).
   `AgentResult.output` / `Done.output`. A `400 invalid_request` after sending
   `input` raises `InvalidArgumentException` with `host_too_old`. Errors stay
   `AgentException(reason="protocol_error", detail_code=...)`.
7. CLI `rayito agent invoke`: `PROMPT` becomes optional; `--input JSON` and
   `--input-file PATH` (`-` is stdin) are mutually exclusive with it and with
   each other, for both targets; `print_result` prints `output` as JSON.
   `--graph` on `agent detect` and `agent deploy`.

### D4. Secrets Manager values as environment variables (C)

1. Surface: `AgentProject.secret_env: Mapping[str, str | SecretEnvRef]` and
   `secret_env_from: Sequence[SecretEnvSource]`;
   `SecretEnvRef(secret, key=None, version_stage=None)`,
   `SecretEnvSource(secret, prefix="", keys=None)`, exported from both SDKs.
   TOML `[secret_env]` and `[[secret_env_from]]`; flags `--secret-env
   NAME=<id>[#key]` and `--secret-env-from <id>[,prefix=P][,keys=A|B]`
   (applied to the project like `--env`). `<id>` is a full secret name
   (no `rayito/` prefix) or an ARN; values pass `_secret_name`. A name in
   both `[env]` and `[secret_env]` is `conflict`. `[env]`, `_validated_env`
   and `_read_env` are untouched. Parsing per `testdata/agent-secret-env/parse.json`.
2. AgentCore: `secret_env_environment(project)` returns
   `{"RAYITO_SECRET_ENV": <spec>}` (compact JSON, sorted keys, names and
   ARNs only; `testdata/agent-secret-env/spec.json`) only when the project
   uses secret env; its length is checked against
   `agentcoreEnvironmentValueMaxChars` (`env_invalid`). The secret
   references are unioned into the readable secrets before
   `secret_resource_arns`, so `ReadableSecretArns`, the template and the CDK
   nested stack do not change. The export adds `aws_secret_env.py` to the
   existing botocore venv and the key `secret_env: {python, script}` to
   `host.json` only when used; `aws_env_credentials.py` is untouched, so
   images without the feature stay byte-identical.
3. Host startup (`agent_host/__main__.py::serve`), between
   `load_host_config` and `create_driver`: pop `RAYITO_SECRET_ENV`; run the
   helper with the spec on stdin and read a JSON map on stdout within
   `agentSecretEnvHelperTimeoutSeconds`; expand with
   `testdata/agent-secret-env/expand.json`; check reserved names
   (`reserved.json` plus the runtime's `host.envs`); `os.environ.update`;
   log only `secret_env_loaded count=N`. Failure is closed: `mark_failed`
   with `secret_env_unavailable`, `secret_env_denied`, `secret_env_invalid`,
   `secret_env_invalid_key` or `secret_env_reserved`; `/ping` never reports
   Healthy; no secret name or value appears anywhere. `sanitize_diagnostics`
   gains `extra_values=()` so injected values shorter than
   `MIN_REDACTED_VALUE_LENGTH` (but at least `agentSecretEnvRedactMinChars`)
   are redacted too.
4. MicroVM: the SDK resolves values with the caller's credentials
   (`SecretStore(prefix="")` behind a cache) and passes them as `secrets=` to
   the host process in `_project_service.serve()` (sync, async and TS), which
   also covers fleets and pool slots. Expansion uses the same function and
   vectors as the host. No `rayd` or proto change.
5. `--check-env` (or any project using secret env) warns `env_missing` for
   names of `.env.example` that receive no value; covered are `[env]`,
   `secret_env`, allow-listed `secret_env_from` keys, gateway variables
   (`GATEWAY_SECRET_ENV_VARS`) and the region variables. `DetectionReport`
   does not change; the names come from a new pure
   `required_env_names(tree)` that reuses the existing `.env.example` scan.
6. `--create-secrets-from PATH` (opt-in, $0.40 per secret per month): a
   minimal own `.env` parser (`KEY=VALUE`, quotes, `export`, comments, no
   interpolation), one JSON secret `rayito/agents/<name>/env` via
   `CreateSecret` or `PutSecretValue` (parameters of `AWS_API_NOTES.md` §19
   only), wired as a `secret_env_from`; confirmation shows names only and
   needs `--yes` or a terminal (`needs_confirmation`).
7. `export(target="docker")` ignores secret env with a warning (phase 2).
   Security line `secretEnv`, `security.md`, `SECURITY.md` T38, export README.

### D5. `langgraph-api` protocol (D)

1. `"langgraph-api"` joins `HTTP_APP_PROTOCOLS` / `PROTOCOLS`;
   `HttpApp.assistant_id: str | None = None` is required with that protocol
   and refused with any other (`InvalidArgumentException`, like
   `_check_json_fields`). `hides_own_routes` treats it like `agentcore`.
   `to_mapping` emits `assistant_id` only when set. No `input_mode`: the
   structured input is B's `Request.input`.
2. Driver branch (`if self._protocol == "langgraph-api"` only): body
   `{"assistant_id", "input", "if_not_exists": "create",
   "multitask_strategy": "reject"}`; `input` is
   `{"messages": [{"role": "user", "content": prompt}]}` for a prompt and
   `Request.input` as is (D.int.1, after B). Path from
   `agentLanggraphInvokePath` with `thread_id = uuid5(agentLanggraphThreadNamespace,
   session_id)`; a request without session gets a new `app_<hex>` id and the
   same derivation, so the thread continues across app restarts. Reading per
   `testdata/agent-langgraph-api/responses.json`; `__interrupt__` is
   `protocol_error/graph_interrupted`; other statuses keep `app_http_<n>`.
   Health `agentLanggraphHealthPath`; its own ready wait
   `agentLanggraphReadyWaitSeconds` (global default unchanged).
3. Detection, only with `--protocol langgraph-api` over the `langgraph-json`
   framework: kind `custom`, app from the launcher `langgraph-dev` on
   `AGENT_APP_INTERNAL_PORT`, protocol `langgraph-api`, `assistant_id` = the
   graph chosen by `select_graph` (D.int.2, after B; before B merges the
   current `preferKeys` choice is used). Any other `--protocol` on a
   framework keeps `protocol_ignored`. Without `langgraph.json`:
   `invalid_value runtime.app.protocol`. Warnings `langgraph_cli_missing` and
   `langgraph_dev_server`. `DetectionApp.assistant_id` is emitted only when
   set.
4. Child environment with this protocol only: `LANGGRAPH_CLI_NO_ANALYTICS=1`
   and `LANGSMITH_TRACING=false` unless the project sets them.

## Integration contract

### 1. Branches and merge order

- Base: `feat/agnostic-deploy` (this change). Each feature branch is cut
  from it and opens one PR back into it; nothing goes to `main` from a
  feature branch, and nothing merges without the maintainer's review.
- Merge order: **A → B → C → D**. Before opening its PR, each branch
  rebases (or merges) the current `feat/agnostic-deploy`.
- D's tasks D.int.1 and D.int.2 start after B is merged.

### 2. Merge rules

- **M1. Exclusive files.** Only the owner edits a file of §3. Another feature
  that needs a change there asks the owner (or the planner amends this
  change on `feat/agnostic-deploy`).
- **M2. Shared files.** A feature edits a shared file of §4 only at the
  anchors listed for it, with exactly the names, types and order listed.
  Edits elsewhere in a shared file are not allowed.
- **M3. Conflict resolution.** A textual conflict in a shared file is
  resolved by keeping both sides in feature order A, B, C, D, and by taking
  the «final form» given in §4 where one is given. No other edit is part of
  a resolution.
- **M4. Generated files are regenerated, never merged by hand:**
  `clients/python/src/rayito/_limits.py`, `clients/typescript/src/limits.ts`
  (`scripts/gen_limits.py`), `_agent/_detection_rules.py`,
  `agent/detection-rules.gen.ts` (`scripts/gen_agent_detection.py`),
  `agent/assets/host-assets.gen.ts`, `template-assets.gen.ts`
  (`scripts/gen_agent_template_assets.py`), and the CLI golden
  `testdata/cli/surface.json` (`RAYITO_UPDATE_CLI_SURFACE=1 uv run pytest
  tests/unit/cli/test_cli_surface.py`). After a merge, run every generator
  with `--check`.
- **M5. Append-only lists.** In lists that grow at the end
  (`testdata/agent-project/invalid/cases.json`,
  `testdata/agentcore-deploy/plan-vectors.json`, `apply-vectors.json`,
  CHANGELOG sections, error tables in docs, `check_sdk_parity.py`
  intentional differences), each feature appends its entries; the order
  after resolution is A, B, C, D.
- **M6. Planning documents.** Nobody edits ADR-033, `proposal.md`,
  `design.md`, the spec deltas or `testdata/agnostic-deploy/contract.json`
  in a feature branch. Each branch ticks only its own section of
  `tasks.md`. A needed change goes back to the planner as a commit on
  `feat/agnostic-deploy`.
- **M7. Pre-allocated numbers.** ADR-033 (this change), threat T38 (C),
  `AWS_API_NOTES.md` Q187 and Q188 (C), Q189 (D), Q190 (A). No other number
  is taken by these branches.
- **M8. Exported names.** `__all__` (Python) and `src/index.ts` stay
  alphabetical; a new name goes to its alphabetical position.

### 3. File ownership (exclusive, M1)

TypeScript twins follow their Python file: Python `_agent/_x_y.py` ↔
`agent/x-y.ts`, `_agentcore/_x.py` ↔ `agentcore/x.ts`. Paths are relative
to `clients/python/src/rayito/` and `clients/typescript/src/` unless they
start with a top-level directory.

**A. agent-model-optional**

- Python: `_agent/_domain.py`, `_agent/_detect_providers.py`,
  `_agent/_host_runtime.py`, `_agent/_telemetry.py`, `_agent/_strands.py`,
  `_agent/_deepagents.py`, `_agent/_hermes.py`, `_agent/_opencode.py`,
  `_agent/_runner/agent_host/drivers/strands.py`,
  `_agentcore/_iam_resources.py`.
- TypeScript: `agent/domain.ts`, `agent/detect-providers.ts`,
  `agent/host-runtime.ts`, `agent/telemetry.ts`, `agent/strands.ts`,
  `agent/deepagents.ts`, `agent/hermes.ts`, `agent/opencode.ts`,
  `agentcore/iam-resources.ts`.
- Data and tests: `testdata/agent-model-optional/**`; new cases
  `testdata/agent-detection/cases/{python-langgraph-openai-compatible-env,ts-langgraphjs-openai-compatible-env,python-openai-model-from-env,error-bedrock-model-from-env-still-required,override-model-id-and-provider}/`;
  `testdata/agent-project/valid/model-without-id.{toml,expected.json}`,
  `testdata/agent-project/invalid/opencode-without-model-id.toml`;
  `testdata/agent-project-e2e/{py,ts}/openai-compatible-env/`; new test files
  named `test_agent_model_optional*.py` / `agent-model-optional*.test.ts`.
- Docs: `docs/site/docs/guias/agente-proveedores{,.en}.md`.

**B. agent-structured-input**

- Python: `_agent/_detect_entry.py`, `_agent/_project_file.py`,
  `_agent/_stream_base.py`, `_agent/_deployment_base.py`,
  `_agent/_deployment.py`, `_agent/_deployment_async.py`, `_agent/_fleet.py`,
  `_agent/_fleet_async.py`, `_agent/_events.py`,
  `_agent/_runner/agent_host/protocol.py`, `_agent/_runner/agent_host/serve.py`,
  `_agent/_runner/agent_host/limits.py`, `_agentcore/_fleet_base.py`,
  `_agentcore/_fleet.py`, `_agentcore/_fleet_async.py`, `_agentcore/_ports.py`.
- TypeScript: `agent/detect-entry.ts`, `agent/project-file.ts`,
  `agent/deployment.ts`, `agent/stream.ts`, `agent/events.ts`,
  `agent/fleet.ts`, `agent/fleet-base.ts`, `agentcore/fleet.ts`,
  `cli/agent-output.ts`.
- Data and tests: `testdata/agent-deploy/invocation-bodies.json`;
  `testdata/agent-samples/{python,typescript}/langgraph-structured/` and their
  `expected/`; new cases
  `testdata/agent-detection/cases/{langgraph-json-two-graphs-prefer-agent,langgraph-json-graph-override,error-langgraph-json-ambiguous-graphs,error-langgraph-json-graph-not-found,override-graph-and-entrypoint-conflict}/`;
  `testdata/agent-project/valid/langgraph-graph.*`,
  `testdata/agent-project/invalid/{langgraph-graph-and-entrypoint,graph-on-non-langgraph,langgraph-graph-unknown}.toml`;
  new tests `test_agent_structured_input*.py` / `agent-structured-input*.test.ts`.

**C. agent-secret-env**

- Python (new unless noted): `_agent/_secret_env_domain.py`,
  `_agent/_detect_env_names.py`, `_agent/_assets/aws_secret_env.py`,
  `_agent/_runner/agent_host/secret_env.py`; existing
  `_agent/_runner/agent_host/__main__.py`,
  `_agent/_runner/agent_host/diagnostics.py`, `_agent/_export.py`,
  `_agent/_project_service.py`, `_agent/_project_service_async.py`,
  `_agentcore/_service.py`, `_agentcore/_service_async.py`.
- TypeScript: `agent/secret-env.ts`, `agent/detect-env-names.ts` (new);
  `agent/export.ts`, `agent/project-service.ts`, `agentcore/service.ts`;
  `clients/typescript/scripts/cost-declarations/secret-env.json` (new).
- Data and tests: `testdata/agent-secret-env/**`;
  `testdata/agent-samples/python/secret-env-echo/` and its `expected/`;
  `testdata/agent-project/valid/secret-env.*`,
  `testdata/agent-project/invalid/secret-env-*.toml`; new tests
  `test_agent_secret_env*.py` / `agent-secret-env*.test.ts`.
- Docs: `docs/site/docs/guias/secretos-como-entorno{,.en}.md` (new),
  `docs/site/docs/secrets{,.en}.md`, `docs/site/mkdocs.yml` (nav entry and
  `nav_translations`), `AWS_API_NOTES.md` (Q187, Q188).

**D. agent-langgraph-api**

- Python: `_agent/_runner/agent_host/drivers/http_app.py`,
  `_agent/_detect_app.py`.
- TypeScript: `agent/detect-app.ts`.
- Data and tests: `testdata/agent-langgraph-api/**`; new cases
  `testdata/agent-detection/cases/{python-langgraph-json-langgraph-api-override,error-langgraph-api-without-langgraph-json,langgraph-api-cli-missing-warning}/`;
  `testdata/agent-samples/python/langgraph-api-echo/`; new tests
  `test_agent_host_langgraph_api.py`, `test_agent_custom_http_app_langgraph.py`,
  `agent-custom-langgraph-api.test.ts`.
- Docs: `AWS_API_NOTES.md` (Q189).

### 4. Shared files: exact additions (M2, M3)

**4.1 `limits.json`** (then `gen_limits.py`; M4). Distinct anchors, so no
textual conflict:

| Feature | Insert after | Keys (value) |
|---|---|---|
| A | — | none |
| B | `agentServiceMaxRequestBytes` | `agentServiceMaxOutputBytes` (1048576) |
| C | `agentSecretNameTemplate` | `agentSecretEnvVariable` ("RAYITO_SECRET_ENV"), `agentSecretEnvSpecVersion` (1), `agentSecretEnvMaxSecrets` (20), `agentSecretEnvMaxTotalBytes` (262144), `agentSecretEnvHelperTimeoutSeconds` (15, provisional, Q188), `agentSecretEnvReservedNames` (["HOME","NODE_OPTIONS","PATH","PYTHONPATH"]), `agentSecretEnvReservedPrefixes` (["AWS_","LD_","RAYITO_"]), `agentSecretEnvRedactMinChars` (4) |
| D | `agentAppPortEnv` | `agentLanggraphHealthPath` ("/ok"), `agentLanggraphInvokePath` ("/threads/{thread_id}/runs/wait"), `agentLanggraphThreadNamespace` ("9f31c836-3dec-4f92-9808-a4a78225e7b9"), `agentLanggraphReadyWaitSeconds` (60, provisional, Q189) |

**4.2 `agent-detection.json`** (then `gen_agent_detection.py`; M4):

| Feature | Section | Insert after | Key |
|---|---|---|---|
| A | top level | `maxModelsListed` | `modelFromCode` (`clientBaseUrl`, `modelEnvPattern`, `baseUrlEnvPattern`) |
| A | `messages` | `invalid_override` | `invalid_model_provider` |
| A | `warnings` | `no_provider` | `model_id_from_code` |
| B | `messages` | `ambiguous_entrypoint` | `ambiguous_graph` |
| B | `messages` | `not_detected_entrypoint` | `graph_not_found`, `graph_conflict` |
| C | `warnings` | `file_unreadable` | `env_missing` |
| D | `app.launchers` | `flask` | `langgraph-dev` |
| D | `messages` | `unsupported_port` | `langgraph_api_without_langgraph_json` |
| D | `warnings` | `protocol_ignored` | `langgraph_cli_missing`, `langgraph_dev_server` |

Texts are those of `contract.json.agentDetectionRules`.

**4.3 `testdata/agent-project/schema.json`:**

- A, `properties.model`: drop `"id"` from `required` (final:
  `["provider", "gateway"]`); after `prompt_caching` add `id_env`,
  `base_url_env`, `api_key_env`, each
  `{"type": "string", "pattern": "^[A-Za-z_][A-Za-z0-9_]*$"}`.
- B, `properties.runtime.properties`: after `adapter` add
  `"graph": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$"}`;
  in the `allOf` branch of `kind = "langgraph"`, append `"graph"` after
  `"package_manager"`.
- C, `properties`: after `env` add `secret_env` (object; `propertyNames`
  the env-name pattern; values a non-empty string or an object with required
  `secret` and optional `key`, `version_stage`, `additionalProperties: false`)
  and `secret_env_from` (array of objects with required `secret`, optional
  `prefix` (env-name prefix pattern `^[A-Za-z_][A-Za-z0-9_]*$` or empty) and
  `keys` (array of env names), `additionalProperties: false`).
- D: none (`runtime.app` is an open object; validation is in code).

**4.4 `testdata/agent-project/invalid/cases.json`:** M5 (append, A, B, C, D).

**4.5 `testdata/agentcore-deploy/contract.json`:**

- A: row `model provider`, cells `call` and `file` become
  `"error provider_unsupported unless bedrock or external_model"`; in the last
  row's `option` text, insert `, --external-model` after
  `--max-session-lifetime`; `constants.cliOptionLabels` gains
  `"external_model": "--external-model"`; `messages.security` gains
  `externalModel` after `readableSecrets`; `messages.errors.model_required`
  text becomes «no se sabe qué modelo de Bedrock usa el agente: fija
  [model] id o pásalo con --bedrock-model».
- C: four rows after `env / --env` (`secret_env / --secret-env`,
  `secret_env_from / --secret-env-from`, `create_secrets_from /
  --create-secrets-from`, `check_env / --check-env`, all `applies` /
  `applies`); `messages.security` gains `secretEnv` after `envCredentials`;
  `constants` gains `secretEnv` (variable name, spec version, reserved names
  and prefixes) after `export`.
- `plan-vectors.json`, `apply-vectors.json`: M5.

**4.6 `_agent/_project_domain.py` / `agent/project.ts`:**

- A: `_read_model`, `_model_to_dict`; constant `MODEL_ID_REQUIRED_PRESETS`
  right after `CUSTOM_GATEWAY_PRESET`.
- B: `_read_runtime_extras`, `_runtime_to_dict`; field
  `graph: str | None = None` after `versions` (TS `graph?: string` after
  `versions`); its validation appended at the end of `__post_init__`.
- C: fields `secret_env` and `secret_env_from` after `graph`; validation
  appended after B's; in `_project_kwargs` the line
  `kwargs.update(_read_secret_env(root))` right after
  `kwargs.update(_read_env(root.table("env")))`; new `_read_secret_env`
  right after `_read_env`; in `to_dict`, after `("env", ...)`, the entries
  `("secret_env", _secret_env_to_dict(self))` and
  `("secret_env_from", _secret_env_from_to_dict(self))`.
- `agent/project-copy.ts`: B then C append their option names.

**4.7 `_agent/_detect_report.py` / `agent/detect-report.ts`:**

- Final `DetectOverrides` field order: `framework, language, entrypoint,
  protocol, model_id, model_provider, model_base_url, graph` (A adds three,
  B adds one). TS: `modelId?, modelProvider?, modelBaseUrl?, graph?`.
- A: `DetectionReport.model_envs` appended last, emitted only when not empty.
- D: `APP_PROTOCOLS` appends `"langgraph-api"`; `DetectionApp.assistant_id`
  appended last, emitted only when set.

**4.8 `_agent/_detect_domain.py` / `agent/detect-domain.ts`:** A edits only
the call that detects the provider (passing the overrides); B edits only
`_entry_for` and the `options` it fills; D edits only the
`elif overrides.protocol is not None` branch (new branch before it for
`langgraph-api`).

**4.9 `_agent/_detect_project.py` / `agent/detect-project.ts`:** A owns
`_gateway_table`, `_model_tables` and the `_MODEL_*` constants; B owns
`_complete_runtime` (it keeps `graph` from the file and does not fill
`entrypoint` when the file sets `graph`).

**4.10 `_agent/_custom.py` / `agent/custom.ts`:** A edits only
`wiring_envs` (omit `RAYITO_MODEL_ID` when the id is `None`) and does not
touch `_app_options` or `_bridge_config` (a `None` id travels as `null`). D
owns `HttpApp`, `HttpAppProtocol`, `HTTP_APP_PROTOCOLS`, `HTTP_APP_KEYS`,
`to_mapping`, `hides_own_routes`, `_environment_options` and the key tuple
in `_app_options` (final: `("request_field", "response_field", "port_env",
"assistant_id")`).

**4.11 `_agent/_runner/agent_host/drivers/deepagents.py`:** A edits only
`build_context` (`model_id: str | None`; model built only when not `None`)
and may add a helper right after it; B edits only `execute` and adds
`_messages_run`, `_structured_run`, `input_kind`, `validate_input` right
after `execute`, keeping the `build_context(...)` call expression verbatim.

**4.12 `_agent/_assets/node_bridge.mjs`:** A edits only `factoryContext`;
B edits only `runLanggraph` and adds helpers right after it.

**4.13 `_agent/_runner/agent_host/__main__.py`:** C only (one call between
`load_host_config` and `create_driver`). Listed here because D's ready wait
must not be implemented there: D reads its wait in `drivers/http_app.py`.

**4.14 `_agent/_deploy_plan.py` / `agent/deploy-plan.ts`:** C appends
`create_secrets_from: str | None = None` and `check_env: bool = False` after
`warm_timeout_seconds` (TS `createSecretsFrom`, `checkEnv` after
`warmTimeoutMs`). `_agent/_deploy_facade.py` / `agent/deploy.ts`: C adds the
`env_missing` warning and `--create-secrets-from` step; A passes
`external_model` through to the AgentCore plan; edits in separate functions.

**4.15 `_agentcore/_models.py` / `agentcore/models.ts`:** A appends
`AgentCoreOptions.external_model: bool = False` after `tags` (TS
`externalModel?: boolean`; `ResolvedAgentCoreOptions.externalModel:
boolean`); `AgentCoreDeployPlan`: A appends `external_model: bool = False`
after `target`, C appends `secret_env_names: tuple[str, ...] = ()` after it.

**4.16 `_agentcore/_domain.py` / `agentcore/domain.ts`** (final forms):

```python
EMPTY_ENVIRONMENT: Final[Mapping[str, str]] = MappingProxyType({})
SECURITY_EXTERNAL_MODEL: Final = "externalModel"
SECURITY_SECRET_ENV: Final = "secretEnv"

def runtime_environment(
    project: AgentProject,
    region: str,
    *,
    model_environment: Mapping[str, str] = EMPTY_ENVIRONMENT,
    secret_environment: Mapping[str, str] = EMPTY_ENVIRONMENT,
) -> dict[str, str]:
    environment = {
        **project.env,
        **model_environment,
        **secret_environment,
        **dict.fromkeys(REGION_ENV_VARIABLES, region),
    }

def security_keys(
    project: AgentProject,
    models: Sequence[str],
    secrets: Sequence[str],
    *,
    external_model: bool = False,
    secret_env: bool = False,
) -> tuple[str, ...]:
    keys = [SECURITY_NO_GATEWAY, SECURITY_MODELS if models else SECURITY_NO_MODELS]
    if secrets:
        keys.append(SECURITY_SECRETS)
    if external_model:
        keys.append(SECURITY_EXTERNAL_MODEL)
    if secret_env:
        keys.append(SECURITY_SECRET_ENV)
    keys.extend((SECURITY_PUBLIC_NETWORK, SECURITY_ISOLATION))
```

`EMPTY_ENVIRONMENT` goes right after `REGION_ENV_VARIABLES` with that exact
text in both A and C (an identical change merges cleanly);
`SECURITY_EXTERNAL_MODEL` (A) and `SECURITY_SECRET_ENV` (C) go after
`SECURITY_ENV_CREDENTIALS`, in that order. A owns `check_provider` (gains
`agentcore: AgentCoreOptions`), `bedrock_models_of`, the new
`external_model_environment`, and the `model_required` check in
`build_plan`; C owns `secret_resource_arns` call-site union and the new
`secret_env_environment`. TypeScript mirrors with `EMPTY_ENVIRONMENT`,
`SECURITY_EXTERNAL_MODEL`, `SECURITY_SECRET_ENV`, options objects
`{ modelEnvironment, secretEnvironment }` and `{ externalModel, secretEnv }`.

**4.17 `_agentcore/_messages.py` / `agentcore/messages.ts`** mirror
`contract.json` (4.5) in the same order.

**4.18 CLI, Python `cli/agent.py`:**

- `detect_command` and `deploy_command`: after the `protocol` parameter, A
  adds `model_id`, `model_provider`, `model_base_url`; then B adds `graph`.
- `deploy_command`: after `create_secret`, C adds `secret_env`,
  `secret_env_from`, `create_secrets_from`, `check_env`; after
  `max_session_lifetime`, A adds `external_model`; A adds
  `EXTERNAL_MODEL_FLAG: external_model` as the last entry of the
  `require_agentcore_target` map.
- Final form: `detect_overrides_of(framework, language, entry, protocol,
  model_id=None, model_provider=None, model_base_url=None, graph=None)`.
- `invoke_command`: B only.
- Help constants: A's in `cli/_agentcore.py` (`EXTERNAL_MODEL_HELP`,
  `EXTERNAL_MODEL_FLAG` after `MAX_SESSION_LIFETIME_*`) and in `cli/agent.py`
  after `DETECT_FLAGS_HELP` (`MODEL_ID_HELP`, `MODEL_PROVIDER_HELP`,
  `MODEL_BASE_URL_HELP`); then B's `GRAPH_HELP`, `INPUT_HELP`,
  `INPUT_FILE_HELP`; then C's `SECRET_ENV_HELP`, `SECRET_ENV_FROM_HELP`,
  `CREATE_SECRETS_FROM_HELP`, `CHECK_ENV_HELP`. `DETECT_FLAGS_HELP` does not
  change. Texts: `contract.json.cliFlags[].help`.
- `cli/_agent_deploy.py::apply_project_flags`: C appends the secret env
  application after the `allow_out` handling. `cli/_agentcore.py` /
  `_agentcore_output.py`: A then C append their plan lines and JSON keys
  (`externalModel`, `secretEnvNames`).

**4.19 CLI, TypeScript:** `cli/commands/agent.ts` `detectOverrideOptions`
appends `--model-id`, `--model-provider`, `--model-base-url` (A) then
`--graph` (B), and `detectOverridesOf` the same fields; `cli/ops-ports.ts`:
`AgentDetectOverrides` appends `modelId`, `modelProvider`, `modelBaseUrl`
(A), `graph` (B); `AgentDeployFlags` appends `secretEnv`, `secretEnvFrom`
(C); `AgentDeploySettings` appends `createSecretsFrom`, `checkEnv` (C);
`cli/agentcore-options.ts`: A adds `externalModelOption` after
`maxSessionLifetimeOption`; `cli/agent-project-flags.ts`: C appends to
`applyProjectFlags` after the `allowOut` block; invoke command: B only.

**4.20 Public exports** (`__init__.py` `__all__`, `src/index.ts`; M8): C adds
`SecretEnvRef`, `SecretEnvSource`; B adds the TS types `AgentInputRequest`,
`JsonObject` (if not already exported).

**4.21 `scripts/check_sdk_parity.py`:** B appends the intentional difference
for the `invoke` / `stream` overloads (M5).

**4.22 CDK** (`cdk/_agent.py`, `cdk/agent.ts`,
`testdata/agentcore-cdk/contract.json` `sdk.*.propNames`): A appends
`external_model` / `externalModel` after `export_outputs` / `exportOutputs`.
C needs no prop (secret env comes from the project) but owns the lines that
add `RAYITO_SECRET_ENV` and the unioned ARNs in the construct, in the
function that builds the runtime environment, after A's.

**4.23 Documentation** (Spanish and `.en.md` in the same commit):

| File | A | B | C | D |
|---|---|---|---|---|
| `guias/despliega-tu-agente` | new `### Modelo elegido por tu código { #modelo-desde-tu-codigo }` before `### Qué se resuelve solo y qué no` | new `### Varios grafos y entrada estructurada { #varios-grafos-y-entrada-estructurada }` before `## 4. Exporta a un contenedor` | one paragraph and link at the end of `## Cómo llega la credencial al modelo` | new `### LangGraph Platform API (experimental) { #langgraph-platform-api }` before `### El contrato del servicio` |
| `guias/desplegar-en-agentcore` | new `### Modelos fuera de Bedrock { #modelos-fuera-de-bedrock }` before `### Etiquetas para organizaciones que las exigen`; paragraph appended to `## Seguridad en AgentCore` | paragraph at the end of `### Sesiones` (input / output) | new `### Secretos como variables de entorno { #secretos-como-entorno }` before `## Qué opciones de MicroVMs no aplican`; paragraph appended to `## Seguridad en AgentCore` after A's | — |
| `guias/desplegar-con-cdk` | `externalModel` prop row | — | sentence on `[secret_env]` | — |
| `referencia/errores` | `model_required` row text in `## AgentCoreException…` | detail codes appended to `## AgentException…` | host codes appended after B's | `graph_interrupted` appended after C's |
| `cli` | flags appended to `## rayito agent` and `### agent con --target agentcore` | `invoke` flags | `deploy` flags | `--protocol` value |
| `security` | paragraph appended to `## AgentCore: sin pasarela de secretos (T36)` | — | new `## Secretos como entorno (T38) { #secretos-como-entorno }` after the T37 section | — |
| `optional-features` | — | — | one row (cost) | — |
| `SECURITY.md` | sentence appended to the T36 row/section | — | T38 after T37 | — |
| `CHANGELOG.md` (both packages) | M5 under `## [Unreleased]`: subsections `### Añadido`, `### Cambiado`, `### Seguridad` in that order; bullets grouped A, B, C, D | | | |

**4.24 `openspec/changes/agnostic-agent-deploy/tasks.md`:** each branch ticks
only its section (M6).

## Risks / Trade-offs

- [Heuristic model-name detection misses an indirect configuration] → the
  overrides and the project file fix it; the guide says it is best-effort.
- [`AgentModel.id` widened to optional breaks a strict type checker in user
  code that reads it] → runtime values and every constructor call are
  unchanged; stated under «Cambiado».
- [An external model on AgentCore egresses to the Internet unfiltered] →
  opt-in only, security line in the plan and docs.
- [Secrets in the environment are readable by all agent code] → stated in
  the plan, `security.md` and T38; the gateway stays the recommendation for
  LLM keys on MicroVM.
- [Host and SDK version skew on `input`] → old hosts answer `400` and the SDK
  explains `host_too_old`; old SDKs ignore `output`.
- [`langgraph dev` is a development server with slow cold start] →
  experimental, opt-in, its own ready wait measured in acceptance (Q189).
- [Parallel branches drift from each other] → M1-M8, the shared vectors and
  the contract file; the planner owns every change to the contract.

## Migration Plan

No migration: without the new keys, flags or options every SDK produces the
same documents, plans, images, environments and calls as 0.15. A project file
that uses a new key fails on an older SDK with `unknown_key`; the guides state
the minimum version. Rollback is reverting the merge of `feat/agnostic-deploy`.

## Open Questions

None. Closed here: `AgentModel.id` is `str | None` (not a sentinel); the
TypeScript structured invoke is an overload `invoke({ input }, options?)`;
invalid input stays `AgentException(protocol_error, invalid_input)`;
`langgraph-api` has no `input_mode` and uses B's `Request.input`; the
`langgraph-api` thread of a new session is derived from the new `app_<hex>`
id with the same `uuid5`, so it continues after the first call; the external
model key on AgentCore reaches the agent only through C or the agent's own
`--readable-secret` read.

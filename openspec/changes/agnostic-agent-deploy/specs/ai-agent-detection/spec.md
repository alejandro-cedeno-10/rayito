## ADDED Requirements

### Requirement: Detection finds the model wiring in code without reading values
The rules SHALL gain `modelFromCode` in `agent-detection.json` (common regex subset, rendered into both SDKs by `scripts/gen_agent_detection.py`). When a client constructor passes a custom `base_url` (Python) or `baseURL`, also inside `configuration` (TypeScript), bounded to the constructor's own parentheses or object, an `openai` provider found by dependency SHALL be reported as `openai-compatible` instead, never as a second provider. A literal matching `officialBaseUrl` (the official OpenAI URL) SHALL NOT count. When the code names a model id literal (or `--model-id` is given), the rewrite SHALL happen only with `--model-base-url`; without it the provider SHALL stay `openai`, exactly as before this change, and the report SHALL carry the warning `custom_base_url`. Detection SHALL find model, base-URL and key variable **names** in `.env.example`, in `os.getenv` / `os.environ` / `process.env` calls, in pydantic-settings fields (`env_prefix` plus the field name, upper case) and in the keys of the `env` map of `langgraph.json`, and SHALL never read their values. When no model id literal is found and the provider's preset is not in `MODEL_ID_REQUIRED_PRESETS`, the resolved project SHALL have `[model]` without `id`, with the `*_env` names found, and the report SHALL carry the warning `model_id_from_code` and `model_envs`; for `bedrock` and `gemini` detection SHALL keep raising `not_detected` on `model.id`. `model_envs` SHALL be emitted only when not empty, so no existing case changes.

#### Scenario: OpenAI-compatible graph configured by settings
- **WHEN** both SDKs detect the case `python-langgraph-openai-compatible-env` (a `ChatOpenAI(model=settings.llm_model, base_url=settings.base_url)` with pydantic-settings and an `.env.example` listing `LLM_MODEL`, `LLM_BASE_URL` and `LLM_API_KEY` without values)
- **THEN** the provider is `openai-compatible`, the project has `id_env = "LLM_MODEL"`, `base_url_env = "LLM_BASE_URL"`, `api_key_env = "LLM_API_KEY"` and no `id`, and the report holds no value from any file

#### Scenario: existing cases are unchanged
- **WHEN** both SDKs run every case of `testdata/agent-detection/cases/` that existed before this change
- **THEN** each report equals its unchanged `expected.json`

### Requirement: The model can be fixed by flags
`DetectOverrides` SHALL gain `model_id`, `model_provider` and `model_base_url` (TypeScript `modelId`, `modelProvider`, `modelBaseUrl`), set by `--model-id`, `--model-provider` and `--model-base-url`. `model_provider` SHALL name a gateway preset of the rules, otherwise `invalid_value` on `model.provider` with the message `invalid_model_provider`; `model_base_url` SHALL become the `upstream` of the chosen `openai-compatible` or `litellm` gateway. With a project file the three flags SHALL behave like `--entry`.

#### Scenario: provider and base URL by flag
- **WHEN** both SDKs detect the case `override-model-id-and-provider`
- **THEN** the project's model and gateway equal `expected.json`

### Requirement: Detection chooses a graph of langgraph.json by name
`DetectOverrides.graph` (`--graph`) SHALL choose the graph of that name from `langgraph.json` through the pure function `select_graph` / `selectGraph`, after `--entry` and before the existing preference for `agent`. Several graphs without a choice and without `agent` SHALL raise `ambiguous` on `runtime.entrypoint` with the message `ambiguous_graph` listing the graph names; an unknown name SHALL raise `invalid_value` on `runtime.graph` (`graph_not_found`); `--graph` with `--entry` SHALL raise `conflict` on `runtime.graph` (`graph_conflict`). The report SHALL carry `options.graph` only when a graph was chosen.

#### Scenario: two graphs, one named agent
- **WHEN** both SDKs detect `langgraph-json-two-graphs-prefer-agent`
- **THEN** the `agent` graph is chosen, as before this change

#### Scenario: two graphs, none named agent
- **WHEN** both SDKs detect `error-langgraph-json-ambiguous-graphs`
- **THEN** detection raises `ambiguous` and the message names both graphs and `--graph`

### Requirement: --protocol langgraph-api serves a langgraph.json project with its own server
Only when `--protocol langgraph-api` is passed and the winning framework is `langgraph-json`, detection SHALL resolve `kind = "custom"` with an app from the launcher `langgraph-dev` on `AGENT_APP_INTERNAL_PORT`, protocol `langgraph-api` and `assistant_id` equal to the chosen graph; it SHALL warn `langgraph_dev_server`, and `langgraph_cli_missing` when `langgraph-cli` is not a dependency. Without `langgraph.json` it SHALL raise `invalid_value` on `runtime.app.protocol`. Any other `--protocol` over a framework SHALL keep the `protocol_ignored` warning, and a `langgraph.json` project without the flag SHALL keep `protocol: "framework"`. `DetectionApp.assistant_id` SHALL be emitted only when set.

#### Scenario: opt-in only
- **WHEN** both SDKs detect `python-langgraph-json` without overrides and `python-langgraph-json-langgraph-api-override` with `{"protocol": "langgraph-api"}`
- **THEN** the first report is unchanged and the second has kind `custom`, protocol `langgraph-api` and `assistant_id`

### Requirement: Missing environment names are reported on request
A pure function `required_env_names` SHALL list the variable names of `.env.example` using the existing scan, without reading values. With `--check-env`, or when the project uses `secret_env` or `secret_env_from`, deploy SHALL warn `env_missing` for each name that receives no value from `[env]`, `secret_env`, an allow-listed `secret_env_from` key, the gateway variables or the region variables. `DetectionReport` SHALL NOT change.

#### Scenario: a name without a value
- **WHEN** `.env.example` lists `DATABASE_URL` and the deploy passes `--check-env` without mapping it
- **THEN** the plan carries the `env_missing` warning for `DATABASE_URL` and no value of the file

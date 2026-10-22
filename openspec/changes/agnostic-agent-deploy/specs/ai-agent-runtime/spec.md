## ADDED Requirements

### Requirement: A runtime with a user entrypoint does not build an unrequested model
When the project has no model id and the request carries no `model`, the `strands` and `deepagents` drivers SHALL NOT build a model client and SHALL give the user's factory `ctx.model = None`; the Node bridge SHALL give `ctx.model` as `undefined`. With an id the drivers SHALL behave exactly as before. The MicroVM wiring SHALL export, besides the standard variables, `base_url_env` with the gateway URL plus `base_path`, `api_key_env` with `MODEL_CREDENTIAL_PLACEHOLDER` and `id_env` with the id when known, and SHALL omit `RAYITO_MODEL_ID` when the id is unknown. Telemetry SHALL omit `gen_ai.request.model` when the id is unknown.

#### Scenario: compiled graph without a model id
- **WHEN** the deepagents driver runs a request for an `entrypoint` that exports a compiled graph, with no model id in the config nor in the request
- **THEN** `build_model` is not called and the run completes

#### Scenario: code that reads its own configuration
- **WHEN** a project declares `base_url_env = "LLM_BASE_URL"` and `api_key_env = "LLM_API_KEY"` for an `openai-compatible` gateway
- **THEN** the agent process sees `LLM_BASE_URL` equal to the gateway URL plus `base_path` and `LLM_API_KEY` equal to the placeholder, and never the real key

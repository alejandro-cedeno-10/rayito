## Why

Rayito 0.15 deploys the agent a team already has (ADR-029) to a MicroVM
fleet or to Amazon Bedrock AgentCore Runtime (ADR-031), but four
assumptions still tie it to one shape of agent:

1. `[model] id` is mandatory. A LangGraph graph that builds
   `ChatOpenAI(model=settings.llm_model, base_url=settings.base_url)` from its
   own configuration fails detection with `model.id: not_detected`, and
   AgentCore refuses every provider that is not Bedrock.
2. The service contract `rayito.agent-service/1` only takes a `prompt`. A
   graph with its own `input_schema` (`{"ticket_id": ...}`) cannot be called,
   and a `langgraph.json` with several graphs and none named `agent` can only
   be resolved with `--entry`.
3. Variables the code reads from AWS Secrets Manager (a database URL, the key
   of an OpenAI-compatible gateway) have no path: `[env]` refuses anything
   that looks like a secret, and AgentCore has no secret gateway.
4. A project that already speaks the LangGraph Platform API cannot be served
   with its own server.

The four pieces touch the same shared files (project schema, detection rules,
CLI options, the AgentCore plan), so they are decided together (ADR-033) and
built on four parallel branches against one integration contract.

## What Changes

Everything is additive: no existing behaviour, flag, project-file key, API or
default changes, and no existing test is edited.

- **A. Model optional when not Bedrock** (`feat/agent-model-optional`).
  `[model] id` becomes optional, with three optional variable *names*
  (`id_env`, `base_url_env`, `api_key_env`). The id stays required only where
  Rayito uses it: the `bedrock` and `gemini` gateways, runtimes that build the
  model, and AgentCore with Bedrock, where `model_required` is finally raised.
  Detection recognises `base_url=` in client constructors (`openai` becomes
  `openai-compatible`) and model variable names, never values. New overrides
  `--model-id`, `--model-provider`, `--model-base-url`. On AgentCore, the
  opt-in `external_model` / `--external-model` allows a non-Bedrock model
  without granting Bedrock permissions and without ever putting the key in
  the runtime environment.
- **B. Structured input and graph selection** (`feat/agent-structured-input`).
  `--graph` / `[runtime] graph` pick a graph from `langgraph.json` without
  running code. The service contract gains `input` (a JSON object, exclusive
  with `prompt`) and `output`; the SDKs gain `invoke(..., input=...)` (a
  TypeScript overload) and `AgentResult.output`; the CLI gains `--input` and
  `--input-file`. The prompt path of message graphs is byte-for-byte the same.
- **C. Secrets Manager values as environment variables**
  (`feat/agent-secret-env`). `[secret_env]`, `[[secret_env_from]]`,
  `--secret-env`, `--secret-env-from`, plus the optional `--check-env` and
  `--create-secrets-from` (creates a secret, costs). On AgentCore the host
  reads them at startup with the runtime role (fail closed, names and values
  never logged), reusing `ReadableSecretArns`; on MicroVM the SDK reads them
  with the caller's credentials and passes them as process secrets.
- **D. `langgraph-api` protocol, experimental and opt-in**
  (`feat/agent-langgraph-api`). `[runtime.app] protocol = "langgraph-api"`
  (or `--protocol langgraph-api` over a `langgraph.json`) serves the project
  with `langgraph dev` and translates `/invocations` to
  `POST /threads/{id}/runs/wait`. No streaming, no production server, never
  auto-detected.
- **ADR-033** in `ARCHITECTURE.md` (amends ADR-031 D10 and D11 for the
  opt-in external model). Shared contract `testdata/agnostic-deploy/contract.json`
  and vector skeletons for each feature.

## Capabilities

### New Capabilities

None. Each piece extends an existing capability.

### Modified Capabilities

- `ai-agent-project`: optional model id and variable names; `[runtime] graph`;
  `[secret_env]` and `[[secret_env_from]]`.
- `ai-agent-detection`: model from code and environment names, model
  overrides, graph selection, `--protocol langgraph-api`, `env_missing`.
- `ai-agent-service`: `input` and `output` in `rayito.agent-service/1`; the
  host loads secret environment variables before the agent; the
  `langgraph-api` HTTP app protocol.
- `ai-agent-runtime`: a runtime with a user entrypoint does not build an
  unrequested model.
- `agentcore-deploy`: the plan accepts an external model on opt-in, raises
  `model_required`, and carries secret environment variables.
- `cli`: the new flags of `rayito agent detect`, `deploy` and `invoke`.

## Impact

- Public API (both SDKs, additive): `AgentModel` fields, `AgentCoreOptions.external_model`,
  `DetectOverrides` fields, `AgentProject.graph` / `secret_env` /
  `secret_env_from`, `SecretEnvRef`, `SecretEnvSource`,
  `AgentDeployOptions.create_secrets_from` / `check_env`, `invoke(input=)`,
  `AgentResult.output`, `HttpApp.assistant_id`, `HttpAppProtocol` value
  `langgraph-api`. Type-only widening: `AgentModel.id` is `str | None`
  (`id?: string`), stated under «Cambiado» in both CHANGELOGs.
- Shared data: `limits.json`, `agent-detection.json`,
  `testdata/agent-project/schema.json`, `testdata/agentcore-deploy/contract.json`,
  `testdata/cli/surface.json` (regenerated).
- Host image: new optional helper `aws_secret_env.py` (only with secret env on
  AgentCore); `drivers/http_app.py` and `drivers/deepagents.py` gain new
  branches. A full regression tier applies (runtime and host change).
- No Rust, proto, `rayd` or CloudFormation template change. The only new AWS
  call is `secretsmanager:GetSecretValue` from the runtime role (C, already
  allowed by `ReadableSecretArns`) and the optional `CreateSecret` /
  `PutSecretValue` of `--create-secrets-from`.
- Cost: off by default. Only `--create-secrets-from` creates a billable
  resource ($0.40 per secret per month); secret reads cost $0.05 per 10,000
  calls.

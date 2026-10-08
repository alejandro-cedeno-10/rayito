## Context

Research of 2026-10-07 (OpenCode `dev`, `vercel/ai`, LangChain partner
packages and the providers' docs) checked, for each provider, its auth
header, upstream and paths, which OpenCode package and LangChain class
talk to it, and whether they accept a `baseURL` towards the loopback
gateway with a placeholder key. Phase 1 covers only static API keys.

## Decisions

1. **Static presets only.** Every preset returns a plain `SecretGateway`:
   one header whose value is resolved from Secrets Manager on the host,
   a fixed `https://host` upstream and a closed `allow` list. No
   short-lived token source, no subscription OAuth, no `rayd` change.
2. **ModelProvider.** Add `openai`, `google`, `azure`. A new provider value
   exists only when the runtime needs a different client package:
   OpenRouter, Groq, Mistral, DeepSeek and LiteLLM speak plain
   `chat/completions` (`openai-compatible` + `base_path`), and xAI speaks
   the OpenAI Responses API (`openai` with the xAI preset).
3. **Paths per preset** (`testdata/agent/provider-catalogue.json`):
   - OpenAI and xAI: `POST /v1/responses`, `POST /v1/chat/completions`,
     `authorization` (`Bearer` stored in the secret).
   - Gemini: per model, `POST /v1beta/models/<m>:streamGenerateContent` and
     `:generateContent`, `x-goog-api-key`. Model ids with `/` are rejected,
     like ARNs in Bedrock; at most 16 models (32 rules).
   - Azure OpenAI v1: upstream `https://<resource>.openai.azure.com`,
     `POST /openai/v1/responses` and `/openai/v1/chat/completions`,
     `api-key`. `resource` must be a DNS label (1-63 `[a-z0-9-]`, no leading
     or trailing hyphen).
   - OpenRouter `/api/v1`, Groq `/openai/v1`, Mistral `/v1`, DeepSeek root:
     `POST <base>/chat/completions` only.
   - LiteLLM: caller's `https://host` upstream (validated by
     `SecretGateway`: https, no path, query or userinfo), `base_path`
     default `/v1` validated like `AgentModel.base_path`, allowing
     `POST <base>/chat/completions` and `POST <base>/responses`.
4. **Model allowlisting.** Only Bedrock and Gemini carry the model in the
   path. For every OpenAI-style API the model is in the JSON body, which
   the gateway streams without parsing, so `allow` cannot restrict it.
   The cost control for those is provider-side (project budgets and
   allowed models in OpenAI, credit limit per key in OpenRouter, budgets
   per virtual key in LiteLLM, deployments and quota in Azure). A body
   `model` allowlist in `rayd` is a possible later change, out of scope.
5. **Runtime mapping.**
   - OpenCode: native providers `openai`, `google`, `azure` with an
     explicit `npm` (`@ai-sdk/openai`, `@ai-sdk/google`, `@ai-sdk/azure`,
     all bundled in the pinned binary), `options.baseURL = <gateway> + /v1 |
     /v1beta | /openai/v1` (each AI SDK package appends its own operation
     path; with `baseURL` set `@ai-sdk/azure` needs no `resourceName` and
     sends no `api-version`) and `apiKey` = placeholder; `models` lists the
     main, small and subagent models because models fetch is disabled.
     `OPENCODE_EXPERIMENTAL_WEBSOCKETS=0`: the Responses WebSocket transport
     would bypass the HTTP-only gateway.
   - deepagents: `openai` → `ChatOpenAI(base_url=<gw>/v1,
     use_responses_api=True)`; `google` →
     `ChatGoogleGenerativeAI(base_url=<gw>, google_api_key=placeholder)`
     (`google-genai` adds `/v1beta`); `azure` →
     `AzureChatOpenAI(base_url=<gw>/openai/v1, api_version="v1",
     use_responses_api=True)`. `ChatOpenAI` would send the key as
     `Authorization`, which Azure reads as an Entra token, while the Azure
     client sends only `api-key`; with `chat/completions` it would rewrite
     the path to `/deployments/<model>/…`, outside the allowlist, hence
     always the Responses API. All three packages are already in the pinned
     venv, and the template smoke test imports them.
   - The wiring was measured with the pinned OpenCode binary and the pinned
     LangChain packages against a fake gateway
     (`docs/research/2026-10-provider-runtimes-spike.md`); the first
     OpenCode Azure mapping (`/openai`) produced `/openai/responses` and
     was corrected.

6. **Refused and deferred.** ChatGPT plan through OpenCode's built-in
   OAuth (Codex client id, `chatgpt.com/backend-api`, refresh token in the
   sandbox): refused. ChatGPT plan through the public "Sign in with
   ChatGPT" flow: deferred until OpenAI confirms that an OSS SDK running a
   user's agents in their own microVMs is eligible. Claude Pro/Max:
   refused (Anthropic's terms forbid routing or intermediating consumer
   credentials). GitHub Copilot and SuperGrok: refused (no terms for
   unattended fleets; would reuse OpenCode's OAuth client). They cannot
   sneak in: `provider`, `enabled_providers` and `plugin` are reserved `raw_config`
   keys, the adapter writes no `auth.json`, `auth` or `plugin` key, and no
   SDK type carries a credential.
7. **Single source of truth.** `testdata/agent/provider-catalogue.json`,
   read by `test_agent_providers.py` and `agent-providers.test.ts` (args in
   snake_case, converted to camelCase in TypeScript).
8. **Zero cost.** Building any preset creates no AWS client and loads no
   optional peer; a test in each SDK checks it.

## Risks

- Paths and headers were checked against a fake gateway, not the real
  providers; a live check against each provider with the egress closed is
  a follow-up (cheap smoke, opt-in, outside CI). Azure accepting
  `api-version=v1` on the v1 routes follows Microsoft's v1 docs and the
  AI SDK's former default, not a live call.

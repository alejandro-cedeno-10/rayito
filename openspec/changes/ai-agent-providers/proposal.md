## Why

`sbx.agent` only reaches Bedrock, the Anthropic Messages API and a generic
OpenAI `chat/completions` endpoint. Users ask for OpenAI, Gemini, Azure
OpenAI, OpenRouter, Groq, Mistral, DeepSeek, xAI and a self-hosted LiteLLM
proxy. All of them take a static API key in a single header, so the
existing secrets gateway (ADR-023) can inject it without touching `rayd`.
Two gaps block them today: OpenCode talks to OpenAI, xAI and Azure through
the Responses API (`/v1/responses`), which `openai_compatible_gateway` does
not allow, and there is no preset with the right upstream, header and
allowlist for each provider, so callers must copy them by hand.

Consumer subscriptions (ChatGPT plan through OpenCode's built-in OAuth,
Claude Pro/Max, GitHub Copilot, SuperGrok) were reviewed and are refused
or deferred: they need the refresh token inside the sandbox, reuse another
app's OAuth client, or their terms forbid automated third-party use.

## What Changes

- `ModelProvider` gains `openai` (Responses API; also xAI), `google`
  (Gemini API) and `azure` (Azure OpenAI v1). `bedrock`, `anthropic` and
  `openai-compatible` stay; OpenRouter, Groq, Mistral, DeepSeek and LiteLLM
  use `openai-compatible` with their preset `base_path`.
- Nine new gateway presets in both SDKs: `openai_gateway`,
  `gemini_gateway(models=)`, `azure_openai_gateway(resource=)`,
  `openrouter_gateway`, `groq_gateway`, `mistral_gateway`,
  `deepseek_gateway`, `xai_gateway` and `litellm_gateway(upstream=,
  base_path="/v1")` (`openaiGateway`, … in TypeScript).
- The OpenCode adapter configures the native `openai`, `google` and
  `azure` providers with `baseURL` pointing at the gateway and the
  placeholder key; the deepagents runner builds `ChatOpenAI(...,
  use_responses_api=True)` and `ChatGoogleGenerativeAI(base_url=...)`.
  deepagents with `azure` raises `UnimplementedError` until the gateway can
  strip a header.
- `gen_ai.provider.name` maps `google` to `gcp.gemini` and `azure` to
  `azure.ai.openai`.
- `testdata/agent/provider-catalogue.json` is the single source of truth
  for both SDKs' tests: presets, invalid inputs, provider mapping and the
  refused list.
- Docs: a provider guide with the catalogue, recommended provider-side
  limits and the refused providers with reasons; SECURITY.md T29 states
  that OpenAI-style APIs cannot be model-allowlisted by the gateway.

## Capabilities

### Modified Capabilities

- `ai-agent-runtime`: new model providers, new gateway presets and their
  runtime mapping.

## Impact

- Python `rayito._agent` and TypeScript `src/agent/` (domain, gateways,
  OpenCode adapter, deepagents adapter and runner, telemetry), public
  exports, tests, docs. The deepagents runner changes, so the template's
  `runner_sha256` changes.
- No `rayd`, `.proto` or AWS change. Building a preset makes no AWS call;
  applying it costs what a `SecretGateway` costs.

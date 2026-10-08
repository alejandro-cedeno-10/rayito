## 1. Catalogue and domain

- [x] 1.1 `testdata/agent/provider-catalogue.json` with presets, invalid inputs, provider mapping and the refused list
- [x] 1.2 `ModelProvider` gains `openai`, `google`, `azure` in Python and TypeScript

## 2. Gateway presets

- [x] 2.1 Python presets in `rayito._agent._gateways`, exported from `rayito`
- [x] 2.2 TypeScript presets in `src/agent/gateways.ts`, exported from `src/index.ts`
- [x] 2.3 Validation: Azure resource DNS label, Gemini model ids without `/`, LiteLLM https upstream and `base_path`

## 3. Runtime mapping

- [x] 3.1 OpenCode adapter: native `openai`, `google`, `azure` providers through the gateway (both SDKs)
- [x] 3.2 deepagents: `ChatOpenAI(use_responses_api=True)`, `ChatGoogleGenerativeAI` and `AzureChatOpenAI` (Responses API)
- [x] 3.3 `gen_ai.provider.name` for the new providers

## 4. Tests

- [x] 4.1 Python and TypeScript tests from the shared catalogue
- [x] 4.2 Zero-cost tests: building a preset creates no AWS client / loads no peer
- [x] 4.3 Refused providers cannot come in through `raw_config` or an `auth.json`

## 5. Docs

- [x] 5.1 Provider guide (Python + TypeScript tabs, "Coste y activación", refused providers)
- [x] 5.2 SECURITY.md T29: no model allowlist on OpenAI-style APIs; provider-side limits
- [x] 5.3 ADR-025 note in ARCHITECTURE.md
- [x] 5.4 CHANGELOG `[Unreleased]` in both SDKs


## 6. Runtime adapters (agent-provider-runtimes)

- [x] 6.1 Spike: the pinned OpenCode binary bundles `@ai-sdk/openai`, `@ai-sdk/google`, `@ai-sdk/azure`, `@ai-sdk/xai` and runs offline against a fake gateway
- [x] 6.2 OpenCode: explicit `npm`, Azure `baseURL` `/openai/v1`, `OPENCODE_EXPERIMENTAL_WEBSOCKETS=0`
- [x] 6.3 deepagents: `AzureChatOpenAI` for `azure`; template smoke test imports `langchain_openai` and `langchain_google_genai`
- [x] 6.4 Golden files `testdata/agent/opencode-config/{openai,google,azure,litellm}.json` and `testdata/agent/deepagents-models.json`, both SDKs

## 7. Local integration verification (agent-provider-local-e2e)

- [x] 7.1 Fake HTTPS upstream in the Docker + Floci harness (`dev/local/providers/`): one-shot CA trusted only by `rayd`, aliases for the nine catalogue hosts, minimal Chat Completions, Responses and Gemini streaming shapes, request log for the tests
- [x] 7.2 Local e2e in both SDKs: every preset with OpenCode and deepagents, egress closed; the real key header reaches the upstream, the placeholder never leaves, a non-allowed path gets 403 and the agent completes a turn; wired into the `local-e2e` workflow
- [x] 7.3 Optional real-API smoke test, skipped unless `RAYITO_SMOKE_<PRESET>_SECRET` is set, capped with `AgentLimits(max_total_tokens=...)` below 0.01 USD per provider
- [x] 7.4 Fix found by 7.2: deepagents venv ships `socksio` so httpx accepts the sandbox's `ALL_PROXY=socks5h://...`

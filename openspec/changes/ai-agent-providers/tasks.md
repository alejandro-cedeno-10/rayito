## 1. Catalogue and domain

- [x] 1.1 `testdata/agent/provider-catalogue.json` with presets, invalid inputs, provider mapping and the refused list
- [x] 1.2 `ModelProvider` gains `openai`, `google`, `azure` in Python and TypeScript

## 2. Gateway presets

- [x] 2.1 Python presets in `rayito._agent._gateways`, exported from `rayito`
- [x] 2.2 TypeScript presets in `src/agent/gateways.ts`, exported from `src/index.ts`
- [x] 2.3 Validation: Azure resource DNS label, Gemini model ids without `/`, LiteLLM https upstream and `base_path`

## 3. Runtime mapping

- [x] 3.1 OpenCode adapter: native `openai`, `google`, `azure` providers through the gateway (both SDKs)
- [x] 3.2 deepagents: `ChatOpenAI(use_responses_api=True)` and `ChatGoogleGenerativeAI`; `azure` raises `UnimplementedError`
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


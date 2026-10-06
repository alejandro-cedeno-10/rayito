## Why

The agent spike (`docs/research/2026-10-agent-spike.md`) proved by hand, with `dev/local/agent/spike.py`, that OpenCode and deepagents run inside one sandbox and reach Claude on Amazon Bedrock only through the secret gateway, with egress closed. Nothing repeatable guards those properties: the planned `sbx.agent` API is built on them (gateway as the only exit, credential unreadable, process-tree abort, limits, event shape), yet a change to `rayd`, the gateway, the guest image or a runtime pin could break them without any test failing.

## What Changes

- New `local`-marked tests in both SDKs (`clients/python/tests/local/test_local_agents.py`, `clients/typescript/tests/local/agents.local.test.ts`) that, against the agent guest of `dev/local/agent/compose.yaml` and a real Bedrock model, check per runtime (OpenCode and deepagents): tool use, session continuation, abort of the agent and its tool processes, step and timeout limits, event stream shape, the credential not readable from the sandbox, direct egress closed while the model call works, and no telemetry enabled by default.
- A shared in-sandbox driver `dev/local/agent/deepagents_check.py` for the deepagents side, so both SDK suites run the same agent code.
- `make local-agent-up` (swap the guest for the agent variant) and `make local-bedrock-key` (mint a short-lived Bedrock API key on the host and pipe it into the runner's tmpfs); the runner declares `RAYITO_LOCAL_BEDROCK_KEY_FILE`. Without the key file the new tests skip, so `make local-e2e` and the `local-e2e` workflow stay free and credential-less.
- Docs: the local testing guide explains the agent run and its cost.

No SDK, `rayd` or proto change.

## Impact

- Specs: `local-testing` (ADDED requirement).
- Cost: only when a maintainer runs `make local-bedrock-key`: a few Bedrock Haiku 4.5 calls (cents). No AWS resource is created; the key is a locally presigned URL.

## 0. Spec and decisions

- [x] 0.1 This change and ADR-029 in `ARCHITECTURE.md`, validated with the pinned
      OpenSpec CLI, before any code of the other lanes.
- [x] 0.2 Verify that the JavaScript Bedrock provider packages honour the gateway
      environment variables (see the design, "JavaScript providers").

## 1. Detection and typed configuration

- [ ] 1.1 `agent-detection.json`, `scripts/gen_agent_detection.py` with `--check`,
      generated rules in both SDKs.
- [ ] 1.2 Detection domain and file-system adapter (Python, TypeScript) and the
      shared vectors in `testdata/agent-detection/`.
- [ ] 1.3 `ScalingOptions`, project `env`, `network`, `[deploy]`, `ServeOptions`
      queue and the new file tables, with named constants in `limits.json`.

## 2. Host and install

- [ ] 2.1 `json` protocol with forwarding, bounded queue, crash-loop restart.
- [ ] 2.2 Generic Node driver for LangGraph.js, deepagents-js, Vercel AI SDK and
      Strands TypeScript.
- [ ] 2.3 Install plans (uv, pip, npm, pnpm, yarn) and pinned Python/Node builds.

## 3. Deploy orchestration

- [ ] 3.1 AWS defaults (base image, bucket, secret, token).
- [ ] 3.2 `AgentFleet` and its sync and async variants, routing, queue, scale-in,
      replacement and `connect`.
- [ ] 3.3 `deploy_agent`, `deployAgent`, `rayito agent detect|deploy` and the
      public exports.

## 4. Samples and tests

- [ ] 4.1 Fifteen sample agents in `testdata/agent-samples/`, each with its own
      lockfile and no rayito import.
- [ ] 4.2 Local end-to-end (Docker and Floci) in both SDKs.
- [ ] 4.3 AWS end-to-end in both SDKs: zero-config and custom options for every
      sample, with inventory before and after.

## 5. Docs and release

- [ ] 5.1 Guide, reference, CLI, optional features, limits and tutorial, in
      Spanish and English.
- [ ] 5.2 Changelogs, measured `AWS_API_NOTES.md` rows, release pull request.
- [ ] 5.3 AWS acceptance, review, 0.14.0.

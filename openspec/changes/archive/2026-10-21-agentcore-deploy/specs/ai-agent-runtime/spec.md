## MODIFIED Requirements

### Requirement: Samples of existing agents run without rayito code
The repository SHALL hold sixteen sample agents under `testdata/agent-samples/` that import nothing from rayito and have their own lockfile: Python LangGraph with FastAPI, LangChain `create_agent`, deepagents, Strands, Strands harness, Hermes, Flask custom HTTP and AgentCore-native; TypeScript LangGraph.js, LangChain.js `createAgent`, deepagents-js, Strands TypeScript, Strands harness TypeScript, Vercel AI SDK and Express or Hono custom HTTP; and OpenCode. A test SHALL fail when a sample imports rayito, lacks a lockfile or holds a secret-like value.

#### Scenario: no rayito import
- **WHEN** the sample check runs
- **THEN** no sample file mentions `rayito` and every sample has a lockfile

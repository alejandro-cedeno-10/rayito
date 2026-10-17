## ADDED Requirements

### Requirement: A generic Node driver hosts JavaScript agent frameworks
The host SHALL provide a `node-callable` driver that loads a JavaScript module export and runs it through one adapter per framework: LangGraph.js and deepagents-js (`invoke` and `stream` with `thread_id`), the Vercel AI SDK (`generate` and `stream`) and Strands TypeScript. `strands-ts` SHALL remain an accepted name for the Strands TypeScript driver. A driver SHALL report usage when the framework returns it and unknown usage otherwise.

#### Scenario: LangGraph.js
- **WHEN** a compiled LangGraph.js graph is the entrypoint
- **THEN** an invocation returns the final text and the session id is the thread id

#### Scenario: alias
- **WHEN** a project declares the `strands-ts` runtime
- **THEN** it runs on the Strands TypeScript adapter as before

### Requirement: JavaScript Bedrock providers reach the gateway with the standard variables
For an agent whose provider package is `@ai-sdk/amazon-bedrock`, `@langchain/aws`, `@strands-agents/sdk` or `@aws-sdk/client-bedrock-runtime`, the host SHALL set `AWS_BEARER_TOKEN_BEDROCK` to the placeholder and `AWS_ENDPOINT_URL_BEDROCK_RUNTIME` to the gateway URL, and SHALL never set the real key. A package that cannot be wired this way SHALL be reported by detection as `unsupported`.

#### Scenario: gateway variables
- **WHEN** a sample using `@ai-sdk/amazon-bedrock` is served through the local gateway
- **THEN** its request reaches the fake Bedrock route with the placeholder as bearer token

### Requirement: Samples of existing agents run without rayito code
The repository SHALL hold fifteen sample agents under `testdata/agent-samples/` that import nothing from rayito and have their own lockfile: Python LangGraph with FastAPI, LangChain `create_agent`, deepagents, Strands, Strands harness, Hermes, Flask custom HTTP and AgentCore-native; TypeScript LangGraph.js, deepagents-js, Strands TypeScript, Strands harness TypeScript, Vercel AI SDK and Express or Hono custom HTTP; and OpenCode. A test SHALL fail when a sample imports rayito, lacks a lockfile or holds a secret-like value.

#### Scenario: no rayito import
- **WHEN** the sample check runs
- **THEN** no sample file mentions `rayito` and every sample has a lockfile

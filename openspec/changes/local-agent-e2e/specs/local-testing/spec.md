## ADDED Requirements

### Requirement: sbx.agent is exercised locally against a real model through the gateway
Each SDK's local suite SHALL include tests that drive `sbx.agent` with OpenCode and deepagents in the agent guest of `dev/local/agent/compose.yaml`, inside one sandbox created with `allow_internet_access=False` and `bedrock_gateway`/`bedrockGateway`, and SHALL check for each runtime: a tool-using run and the continuation of its session, the event stream shape, `abort()` leaving neither the runtime nor its shell tool's processes alive, `max_steps` and `token_budget` ending the run with that reason and no runtime process left, the timeout ending the run with `reason="timeout"`, no telemetry variable and no credential in the agent's environment, direct egress failing while the model call succeeds, and the credential readable nowhere in the sandbox. The tests SHALL read a short-lived Bedrock API key only from the file named by `RAYITO_LOCAL_BEDROCK_KEY_FILE`, SHALL skip when it is absent, and SHALL never print the key.

#### Scenario: without the key the agent tests skip
- **WHEN** `make local-e2e` runs without `make local-bedrock-key`
- **THEN** every agent test is skipped, no call leaves the local environment and the rest of the local suite runs as before

#### Scenario: the model is reachable only through the gateway
- **WHEN** the agent tests run with a key and the agent guest
- **THEN** both runtimes complete a tool-using run through `sbx.agent` while `curl` to the Bedrock endpoint from the sandbox fails, and the key is found neither in the sandbox environment, nor in the agent's environment, nor in `/proc/1/environ`, nor in any readable file

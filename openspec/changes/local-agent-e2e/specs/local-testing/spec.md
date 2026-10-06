## ADDED Requirements

### Requirement: agent runtimes are exercised locally against a real model through the gateway
Each SDK's local suite SHALL include agent tests that run OpenCode and deepagents in the agent guest of `dev/local/agent/compose.yaml` inside one sandbox created with `allow_internet_access=False` and a `SecretGateway` to Amazon Bedrock, and SHALL check for each runtime: a prompt that uses a tool, continuation of the same session, abort and timeout leaving no tool process behind, the runtime's step limit, the event stream shape, that the credential is not readable from the sandbox, that direct egress fails while the model call succeeds, and that no telemetry is enabled by default. The tests SHALL read a short-lived Bedrock API key only from the file named by `RAYITO_LOCAL_BEDROCK_KEY_FILE`, SHALL skip when it is absent, and SHALL never print the key.

#### Scenario: without the key the agent tests skip
- **WHEN** `make local-e2e` runs without `make local-bedrock-key`
- **THEN** every agent test is skipped, no call leaves the local environment and the rest of the local suite runs as before

#### Scenario: the model is reachable only through the gateway
- **WHEN** the agent tests run with a key and the agent guest
- **THEN** both runtimes complete a tool-using prompt through the gateway URL while `curl` to the Bedrock endpoint from the sandbox fails, and the key is found neither in the sandbox environment nor in `/proc/1/environ` nor in any readable file

## ADDED Requirements

### Requirement: rayito agent gains the agnostic-deploy flags in both CLIs
Both CLIs SHALL add, with the names, types, defaults, positions and help texts of `testdata/agnostic-deploy/contract.json` `cliFlags`: `--model-id`, `--model-provider`, `--model-base-url` and `--graph` on `rayito agent detect` and `rayito agent deploy`; `--external-model` (only with `--target agentcore`, `option_requires_agentcore` otherwise), `--secret-env` and `--secret-env-from` (repeatable), `--create-secrets-from` and `--check-env` on `rayito agent deploy`; and on `rayito agent invoke` an optional `PROMPT` with `--input` and `--input-file` (`-` reads stdin), exactly one of the three being required for both targets. `--protocol` SHALL accept `langgraph-api`. Every existing flag, argument, default and help text SHALL stay as it is, and `testdata/cli/surface.json` SHALL only gain entries (and lose `required` on `PROMPT`). `scripts/check_sdk_parity.py --cli` SHALL pass.

#### Scenario: invoke with an input object
- **WHEN** a user runs `rayito agent invoke my-agent --input '{"ticket_id": "T-1"}'`
- **THEN** the request body is `{"input":{"ticket_id":"T-1"}}` and the result's `output` is printed as JSON

#### Scenario: prompt and input together
- **WHEN** a user runs `rayito agent invoke my-agent hola --input '{}'`
- **THEN** the CLI exits with the usage code before any call

#### Scenario: external model outside AgentCore
- **WHEN** a user runs `rayito agent deploy --external-model` without `--target agentcore`
- **THEN** it fails with `option_requires_agentcore` naming `--external-model`

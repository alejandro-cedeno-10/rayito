## ADDED Requirements

### Requirement: rayito agent manages a project from the command line in both CLIs
The Python and TypeScript CLIs SHALL provide `rayito agent init [DIR] --runtime R [--language] [--provider] [--model] [--region] [--force]`, `rayito agent build [--file] --bucket B [--force] [--timeout]`, `rayito agent run PROMPT [--file] [--session-id] [--model] [--stream]`, `rayito agent deploy [--file] [--timeout] [--port] [--token-file]`, `rayito agent export [--file] --target docker|agentcore --out DIR [--overwrite]`, `rayito agent invoke SANDBOX_ID PROMPT [--session-id] [--stream] [--token-file]`, `rayito agent status SANDBOX_ID [--token-file]` and `rayito agent stop SANDBOX_ID`, as shells over `AgentProject` and `AgentDeployment`, with `--file` defaulting to the current directory, defaults taken from SDK constants, `--json` output and the existing exit codes. `rayito agent template build` SHALL keep its surface. `init` SHALL write `rayito-agent.toml` and a minimal scaffold for the chosen runtime and SHALL NOT overwrite an existing file without `--force`; `init`, `export` and loading SHALL make no AWS call. `testdata/cli/surface.json` SHALL list the new commands and both CLIs SHALL match it.

#### Scenario: surface golden
- **WHEN** the Python surface test regenerates `testdata/cli/surface.json` and the TypeScript surface test compares its command tree
- **THEN** both include every `rayito agent` command above with the same options and defaults

#### Scenario: init does not overwrite
- **WHEN** `rayito agent init --runtime strands` runs in a directory that already has `rayito-agent.toml`
- **THEN** it exits with code 2, leaves the file unchanged and prints that `--force` overwrites it

#### Scenario: deploy prints the endpoint
- **WHEN** a unit test runs `rayito agent deploy --json` with `AgentProject.deploy` replaced by a spy returning a fake deployment
- **THEN** the JSON has `sandboxId`, `url` and `port`, and never the proxy headers

### Requirement: A deployment can be reopened from another process with its access token
`AgentProject.deploy()` SHALL accept the sandbox access token (`access_token` in Python, `accessToken` in TypeScript) and pass it to `Sandbox.create()` only when given. `rayito agent deploy` SHALL use `RAYITO_ACCESS_TOKEN` or a new token, SHALL write it to `--token-file` (a new file with mode `0600`, never overwritten, never printed, never in argv) before creating the sandbox, and SHALL exit with code 2 without creating anything when it has neither. `rayito agent invoke` and `rayito agent status` SHALL read the token from `--token-file` or `RAYITO_ACCESS_TOKEN` and exit with code 2 without connecting when there is none. `rayito agent stop` SHALL NOT need the token: it terminates the sandbox by id and is idempotent.

#### Scenario: deploy without a way back
- **WHEN** `rayito agent deploy` runs with no `RAYITO_ACCESS_TOKEN` and no `--token-file`
- **THEN** it exits with code 2 and `AgentProject.deploy` is never called

#### Scenario: deploy then invoke from another process
- **WHEN** `rayito agent deploy --token-file F` finishes and a second process runs `rayito agent invoke SANDBOX_ID PROMPT --token-file F`
- **THEN** the second process reaches the deployment with the saved token and `rayito agent stop SANDBOX_ID` terminates it without any token

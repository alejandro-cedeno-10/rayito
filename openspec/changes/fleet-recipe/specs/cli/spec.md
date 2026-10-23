## ADDED Requirements

### Requirement: rayito agent gains the fleet-recipe flags in both CLIs
Both CLIs SHALL add the following, with the names, defaults and help texts of `testdata/fleet-recipe/contract.json` `cliFlags`:

- on `rayito agent deploy`: `--scale-in-after SECONDS`, `--no-save-recipe`, and `--renew-gateway-keys` (with `--target agentcore`, both `--no-save-recipe` and `--renew-gateway-keys` fail with `option_requires_microvm`);
- on `rayito agent invoke`: `--renew-gateway-keys`, which only applies to a fleet name and passes `renew_gateway_keys=True` / `renewGatewayKeys: true` to `AgentFleet.connect` (without it the CLI keeps the 0.16 call); with a sandbox id it exits with the usage code before any call, and with `--target agentcore` it fails with `option_not_supported`;
- on `rayito agent stop`: `--keep-recipe`.

`rayito agent deploy` SHALL reject an `--env` whose name looks like a secret (`testdata/fleet-recipe/env-rejection.json`, including `*_SECRET`, the name segments `pass`, `passwd`, `pat`, `credential`, `credentials`, and `auth` or `bearer` only as the last segment or next to a `token`, `key` or `header` segment, case-insensitive; `AUTH_MODE`, `AUTH_URL` and `AUTHOR_NAME` are accepted) before any AWS call, with a message that names `--secret-env` and `--secret-env-from`. `testdata/cli/surface.json` SHALL only gain entries, and `scripts/check_sdk_parity.py --cli` SHALL pass.

#### Scenario: secret-looking env
- **WHEN** a user runs `rayito agent deploy . --env API_TOKEN=x`
- **THEN** the CLI fails before any call with a message that points to `--secret-env`

#### Scenario: ordinary auth names are accepted
- **WHEN** a user runs `rayito agent deploy . --env AUTH_MODE=oidc --env AUTH_URL=https://example.com`
- **THEN** both names pass, while `--env X_AUTH=x` and `--env AUTH_HEADER=x` fail before any call

#### Scenario: invoke asks for the renewal on reconnect
- **WHEN** a user runs `rayito agent invoke my-agent hola --renew-gateway-keys`
- **THEN** the CLI connects the fleet with `renew_gateway_keys` true, and the same flag with a `microvm-` sandbox id exits with the usage code without connecting

#### Scenario: scale-in flag reaches the fleet
- **WHEN** a user runs `rayito agent deploy . --scale-in-after 300`
- **THEN** the fleet's `scale_in_after_seconds` is 300 and the recipe stores `scaleInAfterSeconds` 300

### Requirement: --create-secret resolves the key by preset and keeps the exact name
`rayito agent deploy --create-secret` SHALL read the gateway key from the standard variable of the gateway's preset (`testdata/agent-deploy/secret-env.json`), falling back to the gateway name. It SHALL check and create the secret under the exact name the gateway reads, without adding the `rayito/` prefix again (`testdata/agent-deploy/create-secret.json`).

#### Scenario: gateway named model with preset bedrock
- **WHEN** the project has `[gateways.model] preset = "bedrock"` with `secret = "agents/my-agent/model"` and `AWS_BEARER_TOKEN_BEDROCK` is set
- **THEN** the secret is created once from `AWS_BEARER_TOKEN_BEDROCK` as `rayito/agents/my-agent/model` in Secrets Manager, with no `rayito/rayito/` prefix

#### Scenario: detection writes the name relative to the gateway prefix
- **WHEN** detection fills the gateway of project `my-agent` with provider `bedrock`
- **THEN** the gateway secret is `agents/my-agent/bedrock`, read by the gateway as `rayito/agents/my-agent/bedrock`, and a deploy that finds only the 0.16 secret `rayito/rayito/agents/my-agent/bedrock` reuses it instead of creating another

### Requirement: rayito setup publishes the caps image only on request
`rayito setup` in both CLIs SHALL accept `--caps-image`, which adds publishing `rayito-base-caps` to the plan; without it the step is `skip`.

#### Scenario: setup without caps image
- **WHEN** a user runs `rayito setup --dry-run`
- **THEN** the plan shows the `capsImage` step as `skip`

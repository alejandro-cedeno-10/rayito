## ADDED Requirements

### Requirement: Opt-in renewal of short-term Bedrock gateway keys
`renew_gateway_keys` / `renewGatewayKeys` (default false) on `AgentFleet`, `AgentFleet.connect` and `AgentDeployOptions` SHALL do the following, before each VM creation and each parked-slot opening, for every gateway whose preset is `bedrock`:

1. Skip the gateway when this process renewed it within `gatewayKeyRenewAfterSeconds`.
2. Otherwise read `LastChangedDate` with `DescribeSecret`, and skip when the secret changed within that window.
3. Otherwise generate a short-term key with the caller's credentials and the official token generator (expiry `gatewayKeyExpirySeconds`, in the gateway's region).
4. Write `Bearer <key>` with `PutSecretValue` (`SecretId`, `SecretString`, `ClientRequestToken`).
5. Refresh the gateways of the VMs this process holds open.

The key and the secret name SHALL NOT appear in logs, errors, `repr` or the recipe. With the option on and no `bedrock` gateway, construction SHALL raise `InvalidArgumentException`. Without the option, no generator SHALL be imported and no `DescribeSecret`/`PutSecretValue` SHALL be made. The generator SHALL be an optional dependency: Python extra `bedrock-keys`, TypeScript optional peer. A missing generator SHALL raise `InvalidArgumentException` with the install hint.

#### Scenario: renewal writes once per window
- **WHEN** the fleet creates three VMs within five minutes with renewal on and the secret last changed an hour ago
- **THEN** exactly one `PutSecretValue` is made, and each later creation makes no `DescribeSecret`

#### Scenario: renewal off by default
- **WHEN** a fleet is created without the option
- **THEN** the zero-cost trace shows no `DescribeSecret`, no `PutSecretValue` and no generator import

#### Scenario: non-bedrock gateway
- **WHEN** `renew_gateway_keys=True` and the only gateway uses preset `openai`
- **THEN** construction raises `InvalidArgumentException`

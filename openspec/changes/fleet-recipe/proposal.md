## Why

Running `rayito agent deploy` in real accounts after 0.16 exposed twelve
generic gaps. Five of them come from one missing piece: a MicroVM fleet can
only create VMs in the process that loaded the `AgentProject`.

- The deploy flags (`--env`, `--secret-env`, `--secret-env-from`,
  `--allow-out`, `--deny-out`, scaling) are not persisted, so a fleet
  reconnected in another process creates VMs without them, or cannot create
  any without `project=`.
- `rayito agent status <fleet>` without a project prints the
  `ScalingOptions()` defaults (`min 1, max 1`) as if they were real.
- A reconnected fleet that opens a warm slot parked by another process
  starts the host without the project's gateways ("the sandbox has no
  gateway 'model'"). The only workaround is `--warm 0`.
- `scale_in_after_seconds` has no CLI flag.
- The `bedrock` gateway preset only takes a static long-term key, which
  organisations that deny IAM users cannot issue.

The other seven are local defects:

- The agent host logs `httpx`/`httpcore` request lines at INFO, with URLs
  that may carry credentials.
- The host's request logger raises `AttributeError` after a malformed
  request.
- `--create-secret` looks the key variable up by gateway name instead of by
  preset.
- `--create-secret` doubles the `rayito/` secret prefix.
- `rayito setup` does not publish `rayito-base-caps`, the default base of
  agent templates.
- OpenCode zero-config ignores `AGENTS.md` and the `permission` block of
  `opencode.json`.
- The TypeScript e2e suite reuses one secret name across AgentCore and
  MicroVM tests in the same run.

## What Changes

Everything is backward compatible with 0.16. Connecting with an explicit
`project=` makes exactly the same calls as before, and no existing default
changes, except the recipe that `deploy_agent()` now saves (ADR-034 D1:
standard-tier Parameter Store has no charge, and the one KMS request falls
within the free tier).

- **Fleet recipe** (ADR-034 D1–D5). `deploy_agent()` / `deployAgent()` and
  `rayito agent deploy --target microvm` save the run spec in an SSM
  `SecureString` standard parameter `/rayito/agents/<name>/recipe`. The spec
  holds the project document with the flags applied, the image and its
  version, the scaling, the gateway presets and `renewGatewayKeys`. It never
  holds the access token or a secret value. The feature is on by default,
  and `--no-save-recipe` / `save_recipe=False` turns it off.
  `AgentFleet.connect(name)` without `project` reads the recipe, can create
  VMs, and reports the real scaling. `use_recipe=False` skips the read.
  `AgentFleet.stop()` deletes a recipe the fleet knows about, unless
  `keep_recipe=True` / `--keep-recipe` is set. `FleetStatus` gains
  `scaling_source`. Secret-looking `--env` keys are rejected with a pointer
  to `--secret-env` / `--secret-env-from`.
- **Warm slots get the gateways** (ADR-034 D6). Opening a parked slot opens
  the project's (or recipe's) gateways before starting the host, through the
  same configure path as `SandboxPool.take(gateways=)`.
- **Scale-in flag.** `--scale-in-after SECONDS` on `rayito agent deploy` in
  both CLIs. The SDK option is `AgentDeployOptions.scale_in_after_seconds` /
  `scaleInAfterMs`.
- **Opt-in gateway key renewal** (ADR-034 D7). `renew_gateway_keys` /
  `renewGatewayKeys` / `--renew-gateway-keys` is off by default. Before a VM
  is created or a slot is opened, it generates a short-term Bedrock key with
  the caller's credentials, using the official token generator (Python extra
  `bedrock-keys`, TypeScript optional peer). It writes `Bearer <key>` into
  each `bedrock` gateway secret, at most once per renewal window, and
  refreshes the gateways of the VMs that this process has open.
- **Agent host logging.** HTTP client loggers (`httpx`, `httpcore`) are
  raised to WARNING in the host, and the request logger no longer raises
  `AttributeError` after a malformed request line.
- **`--create-secret`.** It resolves the key variable by gateway preset
  (`bedrock` → `AWS_BEARER_TOKEN_BEDROCK`), with the gateway name kept as a
  fallback, and creates the secret under the exact name the gateway reads,
  without a doubled prefix.
- **`rayito setup` publishes `rayito-base-caps`.** It uses the same release
  artifact with `--os-capabilities ALL` and is listed in the plan with its
  cost. `--no-caps-image` / `caps_image=False` skips it.
- **OpenCode zero-config.** `AGENTS.md` becomes `spec.instructions` and the
  `permission` block of `opencode.json` becomes `spec.permissions` when the
  project has no `rayito-agent.toml` values for them.
- **TypeScript e2e.** Every test creates its own secret name
  (`rayito-e2e/<run>/<target>-<test>-<kind>`).

## Capabilities

### New Capabilities

None: each change extends an existing capability.

### Modified Capabilities

- `ai-agent-fleet`: recipe save, read and delete; warm slots with gateways;
  scaling source; scale-in option; key renewal hook.
- `secret-gateway`: opt-in short-term key renewal for the `bedrock` preset.
- `cli`: `--scale-in-after`, `--no-save-recipe`, `--renew-gateway-keys`,
  `agent stop --keep-recipe`, `setup --no-caps-image`, the secret-looking
  `--env` message, `agent status` without project, and `--create-secret` by
  preset without a doubled prefix.
- `account-setup`: the `capsImage` step.
- `ai-agent-service`: host logging hygiene and request-logger robustness.
- `ai-agent-detection`: OpenCode `AGENTS.md` and permissions.
- `e2e-workflow`: unique secret names per TypeScript e2e test.

## Impact

- **Python**: `rayito._agent` (fleet, deploy facade, AWS resolve, project
  domain, detection, opencode, agent host), `rayito._account_setup`,
  `rayito.cli` (agent, setup), and a new `_agent/_fleet_recipe*.py`. New
  optional dependency in extra `bedrock-keys`:
  `aws-bedrock-token-generator==1.1.0`.
- **TypeScript**: `src/agent/*`, `src/account-setup/*`,
  `src/cli/commands/{agent,setup}.ts`, and the new
  `src/agent/fleet-recipe*.ts`. New optional peers: `@aws-sdk/client-ssm`
  and `@aws/bedrock-token-generator`.
- **Shared data**: `limits.json` (recipe and renewal constants, regenerated),
  `agent-detection.json` (if OpenCode needs a new rule),
  `testdata/fleet-recipe/`, `testdata/cli/surface.json` (regenerated),
  `testdata/account-setup/`, and `testdata/agent-deploy/secret-env.json`.
- **Infra**: `infra/iam.yaml` launcher policy gains the
  `ssm:{Put,Get,Delete}Parameter` statement on
  `parameter/rayito/agents/*`. This is an infra change, so the release runs
  the full regression.
- **Docs**: Spanish and English pairs. See `design.md` «Integration
  contract» §5.
- **AWS**: new calls are documented in `AWS_API_NOTES.md` «Receta de la
  flota y claves de corta duración de Bedrock». The open questions
  Q191–Q194 are for acceptance.

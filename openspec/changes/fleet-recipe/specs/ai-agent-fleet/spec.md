## ADDED Requirements

### Requirement: deploy_agent saves the fleet recipe by default
`deploy_agent()`, `deploy_agent_async()` and `deployAgent()` SHALL, after a MicroVM fleet starts successfully, save the fleet recipe (`rayito.agent-recipe/1`, `testdata/fleet-recipe/recipes.json`) with one `PutParameter` call: `Name` from `agentRecipeParameterTemplate`, `Type="SecureString"`, `Tier="Standard"`, `Overwrite=true` and the fixed `Description`, and no other parameter. The recipe SHALL hold the project document after the deploy flags, the image name and version, the gateway presets, the scaling and `renewGatewayKeys`. It SHALL NOT hold the access token or any secret value. `save_recipe=False` / `saveRecipe: false` SHALL skip the call. `AgentFleet.start()` called directly and `plan_deploy()` SHALL make no SSM call. A save failure (`AccessDeniedException`, `ParameterLimitExceeded`, throttling, size) SHALL NOT fail the deploy. It SHALL emit exactly one warning code from `recipe_access_denied`, `recipe_not_saved` or `recipe_too_large`, which never contains the parameter path or value.

#### Scenario: recipe saved after a deploy
- **WHEN** `deploy_agent("./my-agent")` succeeds with `--env LOG_LEVEL=debug` and `--secret-env-from app/config`
- **THEN** one `PutParameter` writes `/rayito/agents/my-agent/recipe` whose project document has `LOG_LEVEL` and the secret name `app/config`, and no secret value

#### Scenario: opt-out
- **WHEN** `deploy_agent(..., options=AgentDeployOptions(save_recipe=False))` succeeds
- **THEN** no SSM operation is called

#### Scenario: oversized recipe
- **WHEN** the compressed envelope exceeds 4096 bytes
- **THEN** the deploy succeeds, the warning is `recipe_too_large`, and a previous recipe of that name is deleted

### Requirement: The recipe fits a standard parameter
The recipe value SHALL be compact JSON with sorted keys when it is at most `agentRecipeMaxBytes` bytes. Otherwise it SHALL be the envelope `{"schema","encoding":"gzip+base64","data"}`. Readers SHALL accept both forms and SHALL treat any other schema or encoding as no recipe, with warning `recipe_unreadable`. Both SDKs SHALL produce byte-identical values for the vectors in `testdata/fleet-recipe/recipes.json`.

#### Scenario: same bytes in both SDKs
- **WHEN** both SDKs encode the vector `small-bedrock`
- **THEN** both produce the exact value of the vector

### Requirement: Reconnecting by name reads the recipe
`AgentFleet.connect(name)` without `project` and with `use_recipe` true (the default) SHALL call `GetParameter` with `WithDecryption=true` once. When the recipe exists, the fleet SHALL rebuild the project, image version, scaling and renewal option from it and SHALL be able to create VMs. An explicit `scaling=` or `renew_gateway_keys=` SHALL win over the recipe. With `project=` or `use_recipe=False`, the fleet SHALL make no SSM call. `ParameterNotFound` SHALL give the 0.16 behaviour. `AccessDeniedException` SHALL give the 0.16 behaviour plus warning `recipe_access_denied`. Any other error SHALL propagate as the SDK's mapped exception.

#### Scenario: connect without project creates VMs
- **WHEN** a second process calls `AgentFleet.connect("my-agent", access_token=token)` and the fleet must grow
- **THEN** the new VM is created from the recipe's image version with the recipe's env, secret env and network

#### Scenario: explicit project unchanged
- **WHEN** `AgentFleet.connect("my-agent", project=project)` is called
- **THEN** the call trace equals 0.16's, with no SSM operation

### Requirement: Fleet status reports where the scaling came from
`FleetStatus` SHALL gain `scaling_source` (`scalingSource`) with one of `explicit`, `project`, `recipe`, `default`. With `default`, `rayito agent status <fleet>` text output SHALL say the scaling is unknown instead of printing `min`/`max`, and `--json` SHALL include `scalingSource`.

#### Scenario: status without project or recipe
- **WHEN** `rayito agent status my-agent` runs and no recipe exists
- **THEN** the output says `escalado desconocido` and does not print `min 1, max 1`

#### Scenario: status from the recipe
- **WHEN** the recipe has `minVms` 2 and `maxVms` 5
- **THEN** the output prints `min 2, max 5` and `scalingSource` is `recipe`

### Requirement: Stopping a fleet deletes its recipe
`AgentFleet.stop()` SHALL call `DeleteParameter` once after terminating the VMs, only when the fleet saved or read a recipe in this process, and SHALL NOT call it with `keep_recipe=True` (`keepRecipe: true`). `ParameterNotFound` SHALL count as success. `AccessDeniedException` SHALL be a warning.

#### Scenario: stop keeps the recipe on request
- **WHEN** `rayito agent stop my-agent --keep-recipe` runs
- **THEN** the VMs are terminated and no `DeleteParameter` is called

### Requirement: Warm slots open the project gateways
Opening a parked warm slot (from this process or another) SHALL open the gateways of the project or recipe in the sandbox before starting the host, through the same helper that `SandboxPool.take(gateways=)` uses. A slot that cannot get its gateways SHALL be dropped and replaced through the existing replacement path.

#### Scenario: reconnected fleet opens a parked slot
- **WHEN** a fleet deployed with `warm_vms=1` is reconnected in another process and an invocation lands on the parked slot
- **THEN** the host starts with the `model` gateway and the invocation succeeds

### Requirement: Deploy options include scale-in
`AgentDeployOptions.scale_in_after_seconds` (`scaleInAfterMs`) SHALL set `ScalingOptions.scale_in_after_seconds`, with the existing minimum, and SHALL be stored in the recipe in seconds.

#### Scenario: scale-in below minimum
- **WHEN** `scale_in_after_seconds=10` is passed
- **THEN** `InvalidArgumentException` is raised before any AWS call

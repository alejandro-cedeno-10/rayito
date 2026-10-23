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
`AgentFleet.connect(name)` without `project` and with `use_recipe` true (the default) SHALL call `GetParameter` with `WithDecryption=true` once. When the recipe exists, the fleet SHALL rebuild the project, image version and scaling from it and SHALL be able to create VMs. An explicit `scaling=` SHALL win over the recipe. The recipe SHALL NOT turn key renewal on: the fleet SHALL renew only when the caller passes `renew_gateway_keys=True` (`renewGatewayKeys: true`); when the recipe has `renewGatewayKeys` true and the caller passed nothing, the fleet SHALL NOT renew and SHALL emit warning `recipe_renewal_ignored` (code only) (`testdata/fleet-recipe/renewal.json`, `connect`). A fleet name `.`, `..` or empty SHALL have no recipe parameter (`recipe_not_saved`). With `project=` or `use_recipe=False`, the fleet SHALL make no SSM call. `ParameterNotFound` SHALL give the 0.16 behaviour. `AccessDeniedException` SHALL give the 0.16 behaviour plus warning `recipe_access_denied`. Any other error SHALL propagate as the SDK's mapped exception.

#### Scenario: connect without project creates VMs
- **WHEN** a second process calls `AgentFleet.connect("my-agent", access_token=token)` and the fleet must grow
- **THEN** the new VM is created from the recipe's image version with the recipe's env, secret env and network

#### Scenario: a renewing recipe never renews by itself
- **WHEN** the recipe has `renewGatewayKeys` true and `AgentFleet.connect("my-agent", access_token=token)` creates a VM
- **THEN** no key is renewed and the warning `recipe_renewal_ignored` is logged without the parameter path

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

## MODIFIED Requirements

### Requirement: Warm VMs and scale-in reuse existing features
Warm VMs SHALL be slots of the existing pool, started with the agent host running, and `PoolConfig` SHALL gain an optional `access_token` whose absence keeps one token per slot. VMs above `min_vms` SHALL carry a sandbox timeout of `scale_in_after_seconds` that the fleet extends with `set_timeout` on use, so an idle extra VM ends by self-termination. To make that `set_timeout` valid, a fleet whose `max_vms` is greater than `min_vms` SHALL create every VM (base, extra and replacement) and every warm slot with `on_timeout="kill"` and `max_lifetime` equal to the `create()` default for `ceil(serve.timeout_seconds)` (`timeout + 60`, at least 120 and at most 28 800 s), and a fleet whose `max_vms` equals `min_vms` SHALL create its VMs without either option, exactly as in 0.16. Before each invocation on an extra VM the fleet SHALL set its deadline to at least the request timeout, and when the last invocation in flight on that VM is released it SHALL set it back to `scale_in_after_seconds`; while another invocation is still in flight on the VM the fleet SHALL NOT shorten its deadline. Every deadline the fleet asks for SHALL be bounded by `rayd`'s cap (`max_lifetime` minus 60 s from the VM's start, or the cap the agent reported) minus 5 s. A `set_timeout` rejected because it passes that cap SHALL NOT mark the VM: the fleet SHALL adopt the cap `rayd` reports when it is earlier than its estimate and retry once. A VM that rejects `set_timeout` with any other `InvalidArgumentException` (created without a logical deadline) SHALL be logged once and never retried. The recipe SHALL NOT store the lifetime: it is derived from the stored scaling and project.

#### Scenario: claim a warm slot
- **WHEN** every running VM is busy and a warm slot exists
- **THEN** the router claims the slot instead of creating a VM

#### Scenario: pool default unchanged
- **WHEN** a pool is built without `access_token`
- **THEN** each slot has its own token as before

#### Scenario: a fleet that can grow creates VMs with a logical deadline
- **WHEN** a fleet with `ScalingOptions(min_vms=1, max_vms=2)` and the default `serve.timeout_seconds` of 3600 starts and grows by one VM
- **THEN** both `create()` calls carry `timeout=3600`, `max_lifetime=3660` and `on_timeout="kill"`, and the warm pool, if any, carries the same `max_lifetime` and `on_timeout`

#### Scenario: the extra VM accepts scale-in
- **WHEN** a unit test whose fake rejects `set_timeout` on a VM created without `max_lifetime`/`on_timeout` grows the fleet with `scale_in_after_seconds=120` and invokes once on the extra VM
- **THEN** the extra VM receives `set_timeout(120)`, `set_timeout(max(120, request timeout))` and `set_timeout(120)`, and the base VM receives none

#### Scenario: overlapping invocations keep the hold
- **WHEN** an extra VM with `serve.max_concurrency=2` and `scale_in_after_seconds=120` runs a long invocation and a short one finishes while the long one is still in flight
- **THEN** the release of the short one sends no `set_timeout(120)`, and the release of the long one does

#### Scenario: a promoted VM is bounded by rayd's cap
- **WHEN** an extra VM created with `max_lifetime=3660` is promoted to base and serves its next invocation
- **THEN** the fleet asks for at most `3660 - 60 - 5` seconds from the VM's start, which `rayd` accepts

#### Scenario: a late cap estimate is corrected
- **WHEN** `rayd` rejects a hold with `timeout beyond cap` and reports a cap earlier than the fleet's estimate
- **THEN** the fleet retries once bounded by the reported cap, logs no unmanaged warning and keeps sending `set_timeout` to that VM

#### Scenario: a single-VM fleet is unchanged
- **WHEN** a fleet with the default `ScalingOptions()` starts
- **THEN** its `create()` call has neither `max_lifetime` nor `on_timeout`

#### Scenario: a VM created without a deadline
- **WHEN** a reconnected fleet uses an extra VM whose `set_timeout` raises `InvalidArgumentException`
- **THEN** the fleet logs one warning for that VM and sends no further `set_timeout` to it


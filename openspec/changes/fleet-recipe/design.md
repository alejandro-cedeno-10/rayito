## Context

Rayito 0.16 deploys an agent to a MicroVM fleet (`deploy_agent()`,
`rayito agent deploy`) or to AgentCore. A fleet can create VMs only in a
process that holds the `AgentProject`. Real deployments showed five gaps
that follow from this: deploy flags are not persisted, `agent status`
reports a fake scaling, a warm slot opened by a reconnected fleet lacks the
gateways, there is no scale-in flag, and the `bedrock` gateway accepts only
a static key. They also showed seven local defects in the agent host, the
secret creation path, `rayito setup`, OpenCode zero-config and the
TypeScript e2e suite. ADR-034 records the architectural decisions. This
document closes every decision and fixes the integration contract for the
five parallel work groups.

## Goals / Non-Goals

**Goals**

- Reconnecting with only a fleet name and access token can create VMs that
  match the deploy, and `status()` reports the real scaling.
- Warm slots always get the project's gateways.
- An opt-in way to run the `bedrock` gateway without a long-term key.
- Fix the seven local defects with tests in both SDKs.
- Stay 100% backward compatible with 0.16 for every caller that passes
  `project=` or does not use the new options.

**Non-Goals**

- Recipes for the AgentCore target. AgentCore already stores its
  configuration in the runtime.
- Persisting the access token anywhere.
- A SigV4 mode in `rayd` for Bedrock.
- Advanced-tier parameters, parameter policies or customer KMS keys for the
  recipe.
- Auto-redeploy or CI integration.

## Decisions

### D1. Recipe content and storage (ADR-034 D1, D4, D5)

- The parameter name is `limits.json` `agentRecipeParameterTemplate` =
  `"/rayito/agents/{name}/recipe"`, where `{name}` is the fleet name
  (`AgentFleet.name`).
- Before formatting, the name is checked against the SSM rule
  `^[A-Za-z0-9_.-]+$`. A fleet name that fails the check skips the recipe
  with warning `recipe_not_saved`. Fleet names already satisfy it, and a
  test pins that.
- `PutParameter` sends only `Name`, `Value`, `Type="SecureString"`,
  `Tier="Standard"`, `Overwrite=true` and a fixed `Description`
  (`agentRecipeDescription`). It never sends `KeyId`, `Tags`, `Policies` or
  `AllowedPattern`.
- The value is the recipe document (schema in «Integration contract» §3).
  It is serialised as compact JSON with sorted keys (`separators=(",",
  ":")`) and encoded as UTF-8.
- If the bytes exceed `agentRecipeMaxBytes` (4096), the value becomes the
  envelope `{"schema":"rayito.agent-recipe/1","encoding":"gzip+base64","data":"<b64>"}`.
  The `data` field holds gzip (mtime 0, level 9) of the same JSON. If the
  envelope also exceeds the limit, the recipe is not saved: the SDK warns
  `recipe_too_large` and deletes any previous recipe of that name.
- Readers accept both forms. A reader rejects any other `schema` or
  `encoding` value with warning `recipe_unreadable`, and then behaves as if
  the fleet had no recipe.
- `project` is the document that `AgentProject` already serialises (the
  `rayito-agent.toml` schema as a dict), taken after
  `apply_project_flags`/CLI overrides. Before saving, the env is
  re-validated with the `AgentProject.env` rule (`_secret_like_env_name` /
  `SECRET_VALUE_PREFIXES`). A failure raises `InvalidArgumentException`
  before anything is written, but this cannot happen after a valid project
  was built.
- The recipe is saved only by `deploy_agent()` / `deploy_agent_async()` /
  `deployAgent()`, after the fleet starts successfully and only for the
  MicroVM target. `AgentFleet.start()` called directly saves nothing.
  `--dry-run` / `plan_deploy()` save nothing and list the step as
  `recipe: save` (or `recipe: off`).
- All SSM failures while saving are warnings and never fail the deploy:
  `recipe_access_denied`, `recipe_not_saved`, `recipe_too_large`. Each
  warning is a short code in the log and in `--json` `warnings`. The
  parameter path and value are never logged.
- The CLI rejects `--env NAME=...` when `NAME` looks like a secret, using
  `SECRET_LIKE_KEYS` plus `secret` (constant `ENV_FLAG_SECRET_LIKE_KEYS`,
  so `*_SECRET` is caught too), the segments `ENV_FLAG_SECRET_SEGMENTS`
  (`pass`, `passwd`, `pat`, `credential`, `credentials`), and
  `ENV_FLAG_AUTH_SEGMENTS` (`auth`, `bearer`) only as the last segment or
  next to one of `ENV_FLAG_AUTH_COMPANIONS` (`token`, `key`, `header`), so
  `AUTH_MODE` or `AUTH_URL` pass. `AgentProject.env` keeps its 0.16 rule
  unchanged for backward compatibility. The error is `InvalidArgumentException` with the message
  `--env no admite secretos (NAME parece uno): usa --secret-env NAME=<secreto> o --secret-env-from <secreto>`.
  It is raised before any AWS call.

### D2. Reading the recipe (ADR-034 D2)

- `AgentFleet.connect(name, *, use_recipe=True, ...)` and
  `AgentFleet.connect(name, { useRecipe: true, ... })` call `GetParameter`
  once with `WithDecryption=true`, only when `project` is `None` and
  `use_recipe` is true. With `project=` there is no SSM call (0.16 trace).
- On success, the fleet rebuilds:
  - `AgentProject` from `recipe.project`, through the same reader the
    project file uses;
  - the image version (`ProjectService(image_version=recipe.image.version)`);
  - `ScalingOptions` from `recipe.scaling`, unless the caller passed
    `scaling=`, which wins;
  - never `renew_gateway_keys`: only the caller's explicit option turns
    renewal on; a recipe with it and a silent caller gives the warning
    `recipe_renewal_ignored`;
  - the project's `gateway_presets` from `recipe.gatewayPresets`.
- Outcomes: `ParameterNotFound` means there is no recipe, and the fleet
  behaves exactly as in 0.16. `AccessDeniedException` gives one warning
  (`recipe_access_denied`) and then the 0.16 behaviour. Any other
  `ClientError` propagates as the SDK's mapped exception, so a real AWS
  failure is never hidden.
- `FleetStatus.scaling_source` (Python) / `scalingSource` (TS) takes one of
  four values:
  - `"explicit"`: `scaling=` was passed;
  - `"project"`;
  - `"recipe"`;
  - `"default"`: neither project nor recipe.
- With `default`, the CLI text prints
  `members: N (escalado desconocido: sin proyecto ni receta)` instead of
  `(min 1, max 1)`, and `--json` keeps `minVms`/`maxVms` and adds
  `scalingSource`.

### D3. Deleting the recipe (ADR-034 D3)

- `AgentFleet.stop(*, keep_recipe=False)` / `stop({ keepRecipe: false })`
  calls `DeleteParameter` after terminating the VMs, only if the fleet
  saved or read a recipe in this process. `ParameterNotFound` counts as
  success. `AccessDeniedException` gives a warning, never an error.
- `rayito agent stop <fleet> --keep-recipe` exposes it. The CLI `stop`
  connects without a project, so it reads the recipe first and then knows
  that it exists.

### D4. Warm slots with gateways (ADR-034 D6)

- The fix goes into `_open_or_connect` → `_serving`: when the sandbox has no
  ready marker (a parked slot) and the fleet has a project, the gateways are
  opened before `service.serve(sandbox)`.
- The project comes from `project=` or from the recipe.
- The gateways are opened with
  `apply_gateways(sandbox, gateways, timeout=...)`, which builds
  `plan_features(FeatureOptions(gateways=...)).configure_sections` and calls
  `sandbox._apply_configure_sections(..., terminate_on_failure=False)`. The
  flag is false because a failure drops and replaces the member through the
  existing `_replace` path.
- `SandboxPool.take(gateways=)` is refactored to call the same helper, so
  only one code path opens gateways (sync, async and TS).
- Python places the helper in `rayito/_pool_base.py` (pure plan) and in
  `sandbox_sync/pool.py` / `sandbox_async/pool.py` (I/O). TypeScript places
  it in `src/pool/`.

### D5. Scale-in flag

- Python: `AgentDeployOptions.scale_in_after_seconds: float | None = None`.
  TypeScript: `scaleInAfterMs?: number`. CLI (both):
  `--scale-in-after SECONDS` (integer seconds, ≥
  `AGENT_SCALE_IN_AFTER_SECONDS_MIN`), with help
  `Segundos sin uso tras los que se termina un VM por encima de --min-vms (por defecto, 900).`
- `scaling_of()` (Python CLI) and its TS twin take the new value. A value
  below the minimum is `InvalidArgumentException` from `ScalingOptions`,
  unchanged.

### D6. Gateway key renewal (ADR-034 D7)

- Options (off by default):
  - Python: `renew_gateway_keys: bool = False` on `AgentFleet(...)`,
    `AgentFleet.connect(...)`, `AsyncAgentFleet` (same) and
    `AgentDeployOptions`.
  - TypeScript: `renewGatewayKeys?: boolean`.
  - CLI: `--renew-gateway-keys` on `rayito agent deploy` (MicroVM only;
    `option_requires_microvm` with `--target agentcore`), help
    `Renueva la clave de corta duración de Bedrock de las pasarelas antes de crear cada VM (apagado por defecto).`
    Also `--renew-gateway-keys` on `rayito agent invoke` (fleet name only;
    a sandbox id is a usage error, `--target agentcore` is
    `option_not_supported`), so a CLI reconnect can opt in.
- Eligible gateways are those whose preset is `bedrock` (from
  `AgentProject.gateway_presets`, see contract §2). With the option on and
  no `bedrock` gateway, construction raises `InvalidArgumentException`
  ("renew_gateway_keys necesita una pasarela con preset bedrock"). A
  `bedrock` gateway whose secret is also read by a gateway with a
  non-Bedrock upstream also raises `InvalidArgumentException`.
- Algorithm per eligible gateway, before `_create_member` and before
  opening a parked slot:
  1. Skip the gateway if this process renewed it less than
     `gatewayKeyRenewAfterSeconds` ago (in-memory clock).
  2. Otherwise, call `DescribeSecret(SecretId=<name>)` and skip if
     `LastChangedDate` is newer than `now - gatewayKeyRenewAfterSeconds`.
  3. Otherwise, generate the token with the caller's credentials in the
     gateway's Bedrock region, with expiry `gatewayKeyExpirySeconds`
     (43200). Python uses `provide_token(region=region,
     aws_credentials_provider=<a botocore CredentialProvider over the
     fleet's boto3 session>, expiry=timedelta(seconds=gatewayKeyExpirySeconds))`.
     TypeScript uses `getToken({ credentials, region, expiresInSeconds })`.
  4. Call `PutSecretValue(SecretId, SecretString="Bearer "+token, ClientRequestToken=uuid4)`.
  5. Call `gateways.refresh()` on every member this process holds open.
- The region is the region of the gateway's upstream (the bedrock preset
  stores it as `[gateways.<n>] region`). The renewal domain computes it in
  `gateway_region(gateway)` from the upstream host
  `bedrock-runtime.<region>.amazonaws.com`, where the region must match an
  AWS region pattern (`[a-z]{2}(-[a-z]+)+-[0-9]+`); any other host is not
  Bedrock.
- Never logged and never included in `repr`, errors or the recipe: the
  token and the secret name. Errors are mapped by code: `AccessDenied*`
  becomes `SecretException` code `permission_denied` naming the gateway,
  not the secret.
- The import is lazy. Python uses the extra `bedrock-keys` →
  `aws-bedrock-token-generator==1.1.0` and raises a missing-extra
  `InvalidArgumentException` with the install hint. TypeScript uses
  `loadOptionalPeer("@aws/bedrock-token-generator")` with peer range
  `1.1.0`, declared in `peerDependenciesMeta`.
- Cost block: SigV4 signing is local (free), `DescribeSecret` costs
  $0.05/10k calls, and `PutSecretValue` writes at most once per window.

### D7. Agent host logging and robustness

- `agent_host/__main__.py serve()`: after `basicConfig`, set
  `logging.getLogger(name).setLevel(logging.WARNING)` for each name in the
  constant `QUIET_HTTP_LOGGERS = ("httpx", "httpcore")` (in `serve.py`,
  exported for the test).
- The host does not add an opt-out. A user who wants those logs configures
  logging in their own code after start, as documented.
- `Handler.log_request`: use `getattr(self, "command", "-")` and
  `getattr(self, "path", "")` (the base class calls it from `send_error`
  before `parse_request` sets them). The test sends a raw malformed request
  line and asserts a `400` with no traceback in the captured log.
- Regenerate the TS embedded assets with
  `python scripts/gen_agent_template_assets.py`.

### D8. `--create-secret` by preset, without a doubled prefix

- `GATEWAY_SECRET_ENV_VARS` stays keyed by preset (the vector file is
  already keyed by provider). `secret_value_from_env(gateway, header,
  environ, *, preset=None)` and `_missing_*_message` look up
  `preset or gateway`. That order keeps 0.16 working where the gateway name
  equals the preset.
- `ensure_model_secrets` passes `project.gateway_presets.get(gateway)`.
- `AgentProject.gateway_presets` (new, contract §2) is filled when a
  project is read from a file or a detection report.
- The existence check and creation use the full secret name that the
  gateway reads at runtime. The gateway reads its secret through the shared
  `SecretCache`, whose prefix is `rayito/`, so gateway secret names are
  relative to `rayito/` (0.16 behaviour, kept: `--secret model=<name>` and
  hand-written projects do not change). The doubled prefix came from
  detection, which wrote `AGENT_SECRET_NAME_TEMPLATE`
  (`rayito/agents/{project}/{provider}`, a full name meant for
  `secret_env`) into the gateway. Fix: detection writes
  `GATEWAY_SECRET_NAME_TEMPLATE` (the same template without `rayito/`), and
  `_deploy_facade`/`deploy.ts` build the store with
  `GATEWAY_SECRET_PREFIX` (`rayito/`, explicit, the prefix the gateway reads
  with). `SecretStore(prefix="")` would have broken the runtime lookup and
  every relative `--secret` name, so it is not used.
- 0.16 compatibility: if the conventional name `agents/<project>/<gateway>`
  is missing but the 0.16 name (`rayito/` + that name, i.e.
  `rayito/rayito/agents/...` in Secrets Manager) exists, the deploy reuses
  it (one extra `DescribeSecret`, only when the first is missing) and
  creates nothing. Any other name is never reinterpreted.
- Vector: `testdata/agent-deploy/create-secret.json` (new), with project
  gateway name, preset and expected secret name and variable.

### D9. `rayito setup` publishes `rayito-base-caps`

- `RESOURCE_ORDER` becomes `("bucket", "iamStack", "baseImage",
  "capsImage")`. A new step `capsImage` uses name `rayito-base-caps`
  (constant `CAPS_IMAGE_NAME`, equal to `DEFAULT_AGENT_TEMPLATE_BASE`) and
  the same actions (`publish`/`reuse`/`wait`).
- It reuses the release zip already downloaded for `baseImage` (one
  download) and publishes with `additionalOsCapabilities=["ALL"]`, the
  existing `--os-capabilities ALL` path documented in §4.
- Off by default, because it is one more billed image version
  ($0.08/GB-month, §12) and everything that costs money is opt-in.
  `--caps-image` / `SetupRequest.caps_image=True` / `capsImage: true` adds
  the step; by default it is shown as `skip`, a new
  action listed in `ALLOWED_ACTIONS["capsImage"]` (`SetupAction` gains `"skip"`).
- Plan vectors in `testdata/account-setup/plan-vectors.json` gain the step.
  Existing vectors gain a `capsImage` input (the same observation as
  `image`), the `capsImage` row and, when it publishes, its cost line
  (`messages.cost.capsImage`, plus the zip line if only the caps image
  publishes); nothing else changes. `capsImageRequested: true` is `--caps-image`.
  The `--json` result gains `capsImageArn` and `capsImageVersion`.

### D10. OpenCode zero-config reads `AGENTS.md` and permissions

- When detection resolves framework `opencode` and the project has no
  explicit `[agent] instructions`/`instructions_file` or
  `[agent.permissions]`, the project builder does two things:
  - It sets `[agent] instructions_file = "AGENTS.md"` when a non-empty
    `AGENTS.md` is at the project root, so the existing reader (UTF-8,
    confined to the project directory) loads it into `spec.instructions`.
    `limits.json` has no instruction size limit, and none is added.
  - It maps `opencode.json` `permission` into `AgentPermissions`: string
    values `allow`/`ask`/`deny` per tool, and `bash` object patterns to the
    existing pattern rules. `AgentPermissions` has no `ask` (the run is
    unattended), so `ask` is applied as `deny`. Unknown keys are ignored.
    Both produce the detection warning `opencode_permission_ignored`.
  - The report carries the proposed `[agent]` table in a field that
    `to_dict()`/`toObject()` leave out (`agent_table` / `agentTable`), and
    `detected_document` fills only the keys the project file does not set,
    and only when the runtime kind is the detected one.
- `opencode.json` and `opencode.jsonc` are read with a new comment and
  trailing-comma stripper (`strip_jsonc` / `stripJsonc`, same output in both
  SDKs); no such reader existed. The rules (file names, permission key, `ask`
  action, warning reasons) are data under `zeroConfig` in
  `agent-detection.json`.
- The detection report records evidence `file:AGENTS.md` and the rule
  `config:permission`. The report never includes the instruction text.
- New detection case: `testdata/agent-detection/cases/opencode-agents-md/`.

### D11. TypeScript e2e secret names

- `secretName(kind)` becomes `secretName(scope, kind)` and returns
  `rayito-e2e/${RUN_ID}/${scope}-${kind}-${suffix}`, where `scope` is the
  test target (`agentcore`, `microvm`) plus the test id, and `suffix` is a
  4-byte random hex string per call site. Each test deletes its own
  secrets in `afterAll`/`finally`.
- `e2e-${RUN_ID}/ts-llm` follows the same rule.

### D12. Scale-in is enforced (ADR-034 D10)

Since 0.14 the fleet calls `set_timeout(scale_in_after_seconds)` on VMs
above `min_vms`, but it created every VM with `Sandbox.create(timeout=...)`
only. Without `max_lifetime` or `on_timeout` the VM has no logical deadline,
`rayd` answers `SetTimeout` with `FAILED_PRECONDITION lifecycle_unmanaged`,
and the fleet swallowed the error with the warning «no se pudo acotar la
vida de un VM sobrante». Scale-in never happened, in 0.14 to 0.16 and in the
0.17 flag. Decisions:

- **Which VMs get a logical deadline.** `fleet_lifetime(project, scaling)` /
  `fleetLifetime()` returns `None`/`undefined` when `max_vms == min_vms`: no
  extra VM can exist, and the create arguments stay byte for byte those of
  0.16 (the default single VM included). When `max_vms > min_vms` every VM
  the fleet creates (base, extra, replacement, `scale()`) and every warm slot
  carries `on_timeout="kill"` and `max_lifetime = default_max_lifetime_for(T)`
  (`defaultMaxLifetimeMs`), with `T = ceil(serve.timeout_seconds)`: `T + 60`,
  at least 120 and at most 28 800 s. A VM can move between base and extra
  (`mark_extras` relabels by position), so base VMs need the deadline too.
  Warm slots only exist when `max_vms > min_vms` (`warm_vms <= max_vms -
  min_vms`), so the pool always gets it.
- **No new AWS parameter.** `max_lifetime` is `maximumDurationInSeconds` and
  `on_timeout` travels in the `lifecycle` block of the `runHookPayload`
  (ADR-011, `AWS_API_NOTES.md` §5, Q58, Q63). The SDK already sends both for
  `Sandbox.create(max_lifetime=..., on_timeout=...)`.
- **Base VMs.** The logical deadline is `T` from creation, the same life
  0.16 gave them through `maximumDurationInSeconds = T`. `rayd` exits with
  124 at the deadline (Q63, about 15 s to `TERMINATED`). The platform cap is
  60 s later and only bounds a resume-grace reopen.
- **Extra VMs.** At creation and when first opened:
  `set_timeout(scale_in_after_seconds)`. Before each invocation:
  `set_timeout(max(scale_in_after_seconds, request_timeout))` (`hold_seconds`
  / `holdMs`), so a VM whose idle deadline is close is not killed in the
  middle of a request. When the last invocation in flight on the VM is
  released (`inflight == 0` after the release, checked under the fleet
  lock): `set_timeout(scale_in_after_seconds)` again. While another
  invocation is still running on it (`max_concurrency > 1`, or a routed call
  waiting in the host queue) the hold stays, because an `EXACT` shortening
  would cut that request off. The release shortens the deadline whether or
  not the invocation returned an inner session. A VM promoted to base
  (`limited` and no longer `extra`) gets everything up to its cap back on its
  next invocation. While it stays idle it keeps its idle deadline, and if it
  ends, `reconcile()` refills `min_vms`.
- **Bounded by `rayd`'s cap.** `rayd` sets its cap at
  `/run + max_lifetime - 60 s` (`CAP_MARGIN` in `rayd-core`,
  `LIFECYCLE_CAP_MARGIN_SECONDS`) and rejects any deadline past it
  (`INVALID_ARGUMENT timeout beyond cap`), so the platform cap is not the
  bound. The fleet's `cap` is `rayd`'s: `SandboxInfo.lifecycle.cap` when the
  agent reported it, otherwise `rayd_cap(platform_expires_at)` /
  `raydCap(platformExpiresAt)` (the platform cap minus 60 s) for a VM it
  opened or took from the pool, `started_at + max_lifetime - 60 s` for a VM
  it listed, and `now + max_lifetime - 60 s` taken before `create()` for one
  it created. `/run` lands a few seconds after `startedAt`, so these are
  never later than the real cap. The fleet asks for
  `min(desired, cap - now - 5 s)` (`lifetime_seconds` / `lifetimeMs`, whole
  seconds rounded up, clock-skew margin `CONNECT_CAP_SAFETY_MS` /
  `REOPEN_CAP_SLACK_MS`) and sends nothing when less than 1 s is left.
- **A late estimate is corrected, not flagged.** The estimate can still be
  late (a listed VM created with a smaller `serve.timeout_seconds`, or a
  local clock behind AWS). The SDKs raise the beyond-cap rejection as
  `LifetimeBeyondCapException` / `LifetimeBeyondCapError`, a private
  subclass of `InvalidArgumentException` / `InvalidArgumentError` that
  carries the `cap_unix_ms` `rayd` reported. The fleet then adopts that cap
  when it is earlier than its estimate and retries once; otherwise it sends
  nothing. A beyond-cap rejection never marks the VM.
- **VMs without a deadline.** A VM created by 0.16, or by a 0.17 fleet that
  could not grow, rejects `set_timeout` with `InvalidArgumentException` /
  `InvalidArgumentError` (`lifecycle_unmanaged`), not the beyond-cap
  subclass. The fleet logs `UNMANAGED_LIFETIME_MESSAGE` once
  for that VM and never retries it. Such a VM is not scaled in. `stop()`
  or a redeploy replaces it.
- **Recipe.** The recipe schema does not change. The lifetime is derived
  from the stored scaling and the stored project (`serve.timeout_seconds`),
  so a fleet reconnected from its recipe creates VMs with the same arguments
  as the deploy. A 0.16 SDK reading a 0.17 fleet ignores the deadline and
  behaves as before.
- **`PoolConfig`.** It gains `max_lifetime: int | None = None` and
  `on_timeout: "kill" | None = None` (`maxLifetimeMs`, `onTimeout`), added
  at the end so positional construction keeps working. They are validated
  like `create()` (`on_timeout="pause"` is rejected: the pool already parks
  its slots) and forwarded in the launch arguments only when set, so a pool
  that does not set them launches exactly as before.
- **Tests.** Shared vectors in `testdata/agent-fleet/lifetime.json`
  (`createArgs`, `listedCap`, `sandboxCap`, `lifetimeSeconds`,
  `promotedRestore`, `correctedCap`, `holdSeconds`). The fleet fakes in both
  SDKs reject `set_timeout` on a VM created without `max_lifetime`/
  `on_timeout`, and a deadline past `start + max_lifetime - 60 s` with the
  cap, as `rayd` does, so the extra-VM test fails on 0.16 code and the
  promotion test fails with a platform-cap bound. Fleet tests assert the
  create arguments, the accepted `set_timeout` calls (create, hold,
  release), the single-VM fleet without them, the warned-once rejection,
  the promotion, two overlapping invocations on one extra VM (no shortening
  while one is in flight) and the correction of a late cap estimate.

## Integration contract

### 1. Groups, branches and merge order

The work happens in one worktree per group. Each group branches from
`feat/sdk-017` and its PR targets `feat/sdk-017`. Only `feat/sdk-017`
opens the single PR to `main`, and intermediate branches are deleted after
their merge.

| Group | Branch | Items | Merge order |
|---|---|---|---|
| G1 | `feat/sdk-017-host-logging` | 1, 7 | 1 |
| G2 | `feat/sdk-017-secrets-setup` | 2, 3, 4 | 2 |
| G3 | `feat/sdk-017-opencode` | 5 | 3 |
| G4 | `feat/sdk-017-e2e-secrets` | 6 | 4 |
| G5 | `feat/sdk-017-fleet-recipe` | 8, 9, 10, 11, 12 | 5 (after G2) |

G5 depends on G2's `AgentProject.gateway_presets` (§2). G5 codes against
the signature below from day one. If G2 has not merged yet, G5 adds the
field in a commit that G2's version supersedes, and the conflict is
resolved by keeping G2's.

Merge rules:

- **M1.** Each group owns its files exclusively (§4).
- **M2.** In a shared file, a group adds only what §2–§3 fix.
- **M3.** Generated files are regenerated after each merge, never merged by
  hand: `_limits.py`, `limits.ts`, detection rules, `host-assets.gen.ts`,
  `testdata/cli/surface.json`.
- **M4.** CHANGELOG conflicts keep both sides in group order.
- **M5.** Docs conflicts in `cli.md`/`cli.en.md` keep both sections.
- **M6.** No group edits another group's tests.

### 2. Shared names (normative; mirrored in `testdata/fleet-recipe/contract.json`)

| Name | Python | TypeScript | CLI | Owner |
|---|---|---|---|---|
| Gateway presets | `AgentProject.gateway_presets: Mapping[str, str]` (read-only, default empty, filled on read, excluded from `to_dict()` output so existing vectors stay byte-identical) | `AgentProject.gatewayPresets: Readonly<Record<string, string>>` | — | G2 |
| Save recipe | `AgentDeployOptions.save_recipe: bool = True` | `saveRecipe?: boolean` (default `true`) | `--save-recipe/--no-save-recipe` (Python), `--no-save-recipe` (TS) | G5 |
| Use recipe | `AgentFleet.connect(..., use_recipe: bool = True)` | `useRecipe?: boolean` | — | G5 |
| Keep recipe | `AgentFleet.stop(*, keep_recipe: bool = False)` | `stop({ keepRecipe?: boolean })` | `rayito agent stop --keep-recipe` | G5 |
| Scaling source | `FleetStatus.scaling_source: Literal["explicit","project","recipe","default"]` | `scalingSource` | `--json` `scalingSource` | G5 |
| Scale-in | `AgentDeployOptions.scale_in_after_seconds: float \| None` | `scaleInAfterMs?: number` | `--scale-in-after SECONDS` | G5 |
| Renew keys | `renew_gateway_keys: bool = False` | `renewGatewayKeys?: boolean` | `--renew-gateway-keys` | G5 |
| Python extra | `bedrock-keys` = `aws-bedrock-token-generator==1.1.0` | peer `@aws/bedrock-token-generator` `1.1.0`, peer `@aws-sdk/client-ssm` | — | G5 |
| Setup caps | `SetupRequest.caps_image: bool = False` | `SetupRequest.capsImage?: boolean` (false) | `rayito setup --caps-image` | G2 |
| Quiet loggers | `QUIET_HTTP_LOGGERS` in `agent_host/serve.py` | (embedded asset) | — | G1 |
| Gateway helper | `apply_gateways(sandbox, gateways, *, timeout)` | `applyGateways(sandbox, gateways, { timeoutMs })` | — | G5 |

New `limits.json` keys (owner G5, regenerated with `scripts/gen_limits.py`):
`agentRecipeParameterTemplate` (`"/rayito/agents/{name}/recipe"`),
`agentRecipeSchema` (`"rayito.agent-recipe/1"`), `agentRecipeMaxBytes`
(4096), `agentRecipeDescription` (`"Receta de una flota de agentes de
Rayito"`), `gatewayKeyRenewAfterSeconds` (900), and
`gatewayKeyExpirySeconds` (43200).

Warning codes (closed list, both SDKs): `recipe_access_denied`,
`recipe_not_saved`, `recipe_renewal_ignored`, `recipe_too_large`,
`recipe_unreadable`.

### 3. Recipe document (`rayito.agent-recipe/1`)

```json
{
  "schema": "rayito.agent-recipe/1",
  "name": "my-agent",
  "savedBy": "python/0.17.0",
  "image": { "name": "my-agent", "version": "3" },
  "project": { "...": "AgentProject document after deploy flags" },
  "gatewayPresets": { "model": "bedrock" },
  "scaling": {
    "minVms": 1, "maxVms": 3, "warmVms": 1,
    "sessions": "shared", "scaleInAfterSeconds": 900
  },
  "renewGatewayKeys": false
}
```

- The keys are exactly these and are camelCase in both SDKs. The scaling
  time is in seconds in both SDKs (TypeScript converts to ms).
- `savedBy` is `python/<version>` or `typescript/<version>`.
- `image.version` is a string. Its absence means "latest active".
- A reader ignores unknown keys inside `project`, but only where the
  project reader already does.
- Vectors in `testdata/fleet-recipe/recipes.json` cover the round trip,
  the envelope and rejection. `testdata/fleet-recipe/renewal.json` covers
  the renewal decisions, and `testdata/fleet-recipe/env-rejection.json`
  covers secret-looking `--env` names. Both SDKs test against the same
  files.

### 4. File ownership (exclusive)

**G1 — agent host logging (items 1, 7)**

- Python:
  - `clients/python/src/rayito/_agent/_runner/agent_host/__main__.py`
  - `clients/python/src/rayito/_agent/_runner/agent_host/serve.py`
- TypeScript (generated): `clients/typescript/src/agent/assets/host-assets.gen.ts`.
- Tests:
  - new `clients/python/tests/unit/test_agent_host_logging.py`;
  - extend `test_agent_host_serve.py` with a malformed request-line case;
  - the existing TS asset-sync test must pass after regeneration.
- Docs:
  - `docs/site/docs/security.md`/`.en.md` (logging hygiene: HTTP client
    loggers);
  - `docs/site/docs/operacion/solucion-de-problemas.md`/`.en.md`.

**G2 — secret creation and setup (items 2, 3, 4)**

- Python:
  - `_agent/_aws_resolve.py`
  - `_agent/_deploy_facade.py` (only the `SecretStore` construction)
  - `_agent/_project_domain.py` (only `gateway_presets`)
  - `_agent/_project_file.py`
  - `_agent/_detect_project.py` (only filling presets)
  - `_account_setup/{_domain,_service,_ports,_aws}.py`
  - `cli/setup.py`, `cli/_setup_output.py`, `cli/_setup_publisher.py`
- TypeScript:
  - `src/agent/aws-resolve.ts`
  - `src/agent/deploy.ts` (only the store)
  - `src/agent/project.ts`, `src/agent/project-file.ts`,
    `src/agent/project-reader.ts` (only `gatewayPresets`)
  - `src/account-setup/{domain,service,ports,artifact-stack}.ts`
  - `src/cli/commands/setup.ts`, `src/cli/setup-output.ts`,
    `src/cli/setup-adapters.ts`
- Data:
  - `testdata/agent-deploy/secret-env.json` (comment only)
  - new `testdata/agent-deploy/create-secret.json`
  - `testdata/account-setup/{plan-vectors,cli-cases}.json`
- Tests:
  - Python: new `test_create_secret_preset.py`, plus extensions of
    `test_account_setup_domain.py`, `test_account_setup_service.py` and
    `test_account_setup_adapters.py`;
  - TypeScript: new `create-secret-preset.test.ts`, plus extensions of
    `account-setup.test.ts`.
- Docs:
  - `cli.md`/`.en.md`, sections `setup` and `agent deploy --create-secret`;
  - `primeros-pasos/configurar-aws.md`/`.en.md`;
  - `guias/despliega-tu-agente.md`/`.en.md` (secret name);
  - `cost.md`/`.en.md` (setup row).

**G3 — OpenCode zero-config (item 5)**

- Python:
  - `_agent/_opencode.py`
  - `_agent/_detect_project.py`, only the OpenCode branch; G2 touches only
    the presets, so a conflict keeps both
  - `_agent/_detect_providers.py` (if needed)
- TypeScript:
  - `src/agent/opencode.ts`
  - `src/agent/detect-project.ts` (OpenCode branch)
  - `src/agent/detect-providers.ts`
- Data:
  - `agent-detection.json`, then regenerate
  - new case `testdata/agent-detection/cases/opencode-agents-md/`
- Tests:
  - Python: `test_agent_opencode.py` (extend), new
    `test_opencode_zero_config_files.py`;
  - TypeScript: `agent-opencode.test.ts` (extend), new
    `opencode-zero-config-files.test.ts`.
- Docs:
  - `guias/agente-proveedores.md`/`.en.md` (OpenCode section);
  - `funciones-opcionales/templates-de-agente.md`/`.en.md`.

**G4 — TS e2e secret names (item 6)**

- `clients/typescript/tests/e2e/agnostic-deploy-support.ts`
- `clients/typescript/tests/e2e/agnostic-deploy.e2e.test.ts`
- `clients/typescript/tests/e2e/agent-project-support.ts`
- any other `tests/e2e/*.ts` that builds a secret name

There are no docs. It adds a unit test,
`clients/typescript/tests/unit/e2e-secret-names.test.ts`, that imports
`secretName` and asserts that names are unique per call and per scope.

**G5 — fleet recipe, warm gateways, scale-in, renewal, status (items 8–12)**

- Python:
  - new `_agent/_fleet_recipe.py` (pure domain: document, encoding,
    validation)
  - new `_agent/_fleet_recipe_aws.py` (SSM adapter)
  - new `_agent/_gateway_keys.py` (renewal domain + adapter)
  - `_agent/_fleet.py`, `_agent/_fleet_async.py`, `_agent/_fleet_base.py`
  - `_agent/_deploy_facade.py`, except the `SecretStore` line owned by G2
  - `_agent/_deploy_plan.py`
  - `_pool_base.py`, `sandbox_sync/pool.py`, `sandbox_async/pool.py` (only
    `apply_gateways`)
  - `cli/agent.py` (deploy flags, status, stop)
  - `cli/_agent_deploy.py`
  - `pyproject.toml` (extra `bedrock-keys`)
- TypeScript:
  - new `src/agent/fleet-recipe.ts`, `src/agent/fleet-recipe-aws.ts`,
    `src/agent/gateway-keys.ts`
  - `src/agent/fleet.ts`, `src/agent/fleet-base.ts`,
    `src/agent/deploy.ts` (except the store), `src/agent/deploy-plan.ts`
  - `src/pool/*` (only `applyGateways`)
  - `src/cli/commands/agent.ts`, `src/cli/agent-deploy.ts`
  - `package.json` (peers), `src/index.ts` (exports)
  - `scripts/cost-declarations/fleet-recipe.json` and
    `scripts/cost-declarations/gateway-key-renewal.json`
- Shared:
  - `limits.json`
  - `infra/iam.yaml` (launcher statement `AgentRecipe`)
  - `testdata/fleet-recipe/*`
  - `scripts/check_sdk_parity.py` allow-list, only if needed
- Tests:
  - Python, new: `test_fleet_recipe.py` (vectors), `test_fleet_recipe_aws.py`
    (allowed operations and parameters, error mapping),
    `test_fleet_connect_recipe.py`, `test_fleet_warm_gateways.py`,
    `test_gateway_key_renewal.py`, `test_cli_agent_recipe.py`;
  - Python, extended: `test_m15_zero_cost.py` and the fleet zero-cost trace
    (no SSM call with `project=` or `save_recipe=False`).
  - TypeScript, new: `fleet-recipe.test.ts`, `fleet-recipe-aws.test.ts`,
    `fleet-connect-recipe.test.ts`, `fleet-warm-gateways.test.ts`,
    `gateway-key-renewal.test.ts`, `cli-agent-recipe.test.ts`;
  - TypeScript, extended: `zero-cost-trace.test.ts`.
  - e2e (both, env-gated `RAYITO_E2E_*`): deploy with `--warm 1`, connect by
    name from a second process, invoke through a warm slot, status, stop.
- Docs:
  - `guias/flota-de-agentes.md`/`.en.md` (recipe, reconnect, status,
    scale-in, warm slots)
  - `funciones-opcionales/pasarela-de-secretos.md`/`.en.md` (key renewal,
    «Coste y activación»)
  - `optional-features.md`/`.en.md` (two rows)
  - `cost.md`/`.en.md`
  - `operacion/iam.md`/`.en.md`
  - `cli.md`/`.en.md` (`agent deploy` flags, `agent status`, `agent stop`)
  - `concepts.md`/`.en.md` (one paragraph)

**Integration (after all merges, on `feat/sdk-017`)**

- `CHANGELOG.md` files (each group adds its own bullets; integration
  checks them).
- `docs/site/docs/novedades/0.17.0.md`/`.en.md`, plus `nav` in `mkdocs.yml`
  with `nav_translations`.
- `MILESTONES.md`.
- Regenerate all generated files.
- Full gates.

### 5. Gates per group

Every group runs the gates of the languages it touched
(`.claude/skills/rayito-engineering/reference/gates.md`):

- Python: `pytest tests/unit`, `ruff check`, `ruff format --check`,
  `mypy src tests`.
- TypeScript: `pnpm lint`, `pnpm typecheck`, `pnpm build`, `pnpm test`,
  `pnpm pack:check`.
- Docs: `mkdocs --strict`, `check_docs_i18n.py`, `check_docs_examples.py`.
- Root: `check_hygiene.py`, `check_pins.py`, `gen_limits.py --check`,
  `gen_agent_detection.py --check`, `check_sdk_parity.py` and `--cli`.
- OpenSpec:
  `pnpm dlx @fission-ai/openspec@1.10.0 validate fleet-recipe --strict --no-interactive`.

No `npm`/`npx`.

## Risks / Trade-offs

- The recipe is on by default, so a deploy makes three new calls
  (`PutParameter` plus one KMS request inside the free tier). This is
  mitigated by the opt-out, the plan line and zero-cost tests that pin "no
  SSM call" without `deploy_agent()`.
- Recipe readers need `ssm:GetParameter`. Without it, the fleet degrades to
  0.16 behaviour with one warning.
- Parameter Store's 30-second wait after `DeleteParameter` can make an
  immediate stop+deploy skip the recipe. The deploy succeeds with warning
  `recipe_not_saved`. This is documented.
- Short-term keys are bounded by the caller's session, so VMs opened by
  other processes keep an older key until refreshed. This is documented
  with `scale_in_after_seconds` and measured in Q193.
- `capsImage` adds one image (≈ $0.08/GB-month) to `setup`. It is listed in
  the plan, and only `--caps-image` adds it.

## Migration Plan

No migration. 0.16 project files, flags and APIs work unchanged. Fleets
deployed with 0.16 have no recipe and reconnect as before until the next
`agent deploy`. A 0.16 SDK ignores recipes.

## Open Questions

None blocking. Q191–Q194 in `AWS_API_NOTES.md` are measured in acceptance.

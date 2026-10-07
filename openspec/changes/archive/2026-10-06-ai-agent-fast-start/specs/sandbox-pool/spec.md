## MODIFIED Requirements

### Requirement: PoolConfig is the immutable launch configuration shared by every slot
The Python SDK SHALL export a frozen dataclass `PoolConfig(size, template=None, template_version=None, timeout=28800, idle=IdlePolicy(), envs=None, metadata=None, cpu_time_limit=None, execution_role_arn=None, ingress=None, egress=None, logging="disabled", min_remaining_seconds=3600, fill_concurrency=4, sweep_interval_seconds=30.0, ready_timeout=90.0, index=None, warmup=(), allow_internet_access=True, network=None)` whose fields map one-to-one onto the `Sandbox.create()` kwargs of the same name; every slot of a pool SHALL be launched with these values, so `envs`, `metadata`, `cpu_time_limit`, the idle policy, the wall and the connectors are per pool, never per take. `__post_init__` SHALL raise `InvalidArgumentException` naming the field when `size` is outside `1..=64`, `timeout` fails `validate_timeout`, `idle` is `None` or has `auto_resume=False` or `max_idle_seconds >= timeout`, `min_remaining_seconds` is outside `60..=timeout-60`, `fill_concurrency` is outside `1..=8`, `sweep_interval_seconds < 5`, `ready_timeout <= 0`, `envs`/`metadata`/`cpu_time_limit` fail the `_payload` validators, `warmup` is not a sequence of `WarmupStep` with a non-blank `cmd` and a positive `timeout_seconds`, `allow_internet_access` is not a `bool`, or `network` fails the same validation as `Sandbox.create(network=...)`. `network` and `allow_internet_access=False` SHALL appear in the launch kwargs only when set, so a pool that does not set them launches exactly as before. TypeScript `PoolConfig` SHALL accept the same fields as `warmup`, `allowInternetAccess` and `network`. `PoolConfig` SHALL have no `access_token` field (tokens are per slot) and no `allowed_ports` field (`get_host(port)` mints per port after the take).

#### Scenario: idle policy without auto-resume is rejected
- **WHEN** a unit test builds `PoolConfig(size=2, idle=IdlePolicy(auto_resume=False))`
- **THEN** `InvalidArgumentException` is raised and its message contains `idle` and `auto_resume`

#### Scenario: size cap
- **WHEN** a unit test builds `PoolConfig(size=65)` and `PoolConfig(size=0)`
- **THEN** both raise `InvalidArgumentException` naming `size` and the range `1..=64`

#### Scenario: defaults park for the whole wall
- **WHEN** a unit test builds `PoolConfig(size=1)`
- **THEN** `timeout == 28800`, `min_remaining_seconds == 3600`, `idle == IdlePolicy()` and the launch kwargs derived from it resolve `suspended_duration_seconds` to `28800 - 300`

#### Scenario: egress is per pool and only forwarded when set
- **WHEN** a unit test derives the launch kwargs of `PoolConfig(size=1)` and of `PoolConfig(size=1, allow_internet_access=False)`
- **THEN** the first has neither `network` nor `allow_internet_access` and the second has `allow_internet_access=False`

#### Scenario: an invalid warm-up step is rejected
- **WHEN** a unit test builds `PoolConfig(size=1, warmup="opencode --version")` or `PoolConfig(size=1, warmup=[WarmupStep(cmd="  ")])`
- **THEN** both raise `InvalidArgumentException` naming `warmup`

### Requirement: A slot is warmed with create(), parked with pause() and kept as data
`SandboxPool` SHALL warm a slot by calling `Sandbox.create()` with the pool's launch kwargs, a fresh `generate_access_token()` for that slot and `keep_on_failure=False` (readiness is therefore `agent_ready and kernel_ready`), then settle the kernel, then run `PoolConfig.warmup` in order (a foreground step with `commands.run(timeout=step.timeout_seconds, max_output_bytes=0)`, whose non-zero exit or timeout fails the warm-up like any other warm-up failure: the VM is terminated, the record deleted, `failed` incremented and the fill backoff applied; a background step with `commands.run(background=True, timeout=None, max_output_bytes=0)` and its handle disconnected so the process keeps running into the snapshot), then `pause(wait=True)` (`suspend-microvm` through the shared 2 TPS bucket, `get-microvm` polled until `SUSPENDED`), then `close()` on the local handle. A `SlotRecord(sandbox_id, access_token, endpoint, template, template_version, started_at, maximum_duration_seconds, idle, execution_role_arn, ingress, egress, region, state, parked_at)` SHALL be saved to the backend with `state="warming"` immediately after `run-microvm` returns and re-saved with `state="ready"` and `parked_at` after the park; the pool SHALL hold no live `Sandbox` handle, channel, refresher or JWE for a parked slot. `SlotRecord.__repr__` SHALL redact `access_token`.

#### Scenario: warm-up sequence against the fakes
- **WHEN** a unit test starts a `SandboxPool(PoolConfig(size=2, template=IMAGE_ARN))` over the in-memory fake control plane and the fake `rayd`
- **THEN** for each slot the fake logged `run_microvm`, `create_auth_token`, `Health` until `kernel_ready`, `get_microvm`, `suspend_microvm`, `get_microvm` until `SUSPENDED`; the two records have different `access_token`s; each launch's `runHookPayload` carries `token_sha256` equal to `access_token_sha256` of its slot's token; both records are `ready` with `parked_at` set; no fake channel remains open

#### Scenario: record saved before the park
- **WHEN** the fake control plane makes `suspend_microvm` raise on the first warm-up
- **THEN** the backend held that slot's record with `state="warming"` before the failure, the record is deleted afterwards, the VM was terminated and `stats().failed == 1`

#### Scenario: warm-up steps run after settle and before the park
- **WHEN** a unit test starts a pool of one slot with `warmup=(foreground, background)` and a spy on `commands.run`
- **THEN** both steps ran after the settle cell and before any `suspend_microvm`, the background step used `background=True`, no timeout and `max_output_bytes=0` and its handle was disconnected, and the slot is parked `ready`

#### Scenario: a failing warm-up step fails the warm-up
- **WHEN** the foreground step raises `CommandExitException`
- **THEN** no `suspend_microvm` happened for that slot, the VM was terminated, `stats().failed >= 1` and `stats().ready == 0`

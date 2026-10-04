## MODIFIED Requirements

### Requirement: reincarnate replaces the VM and keeps the files
`Sandbox.reincarnate(*, exclude: Sequence[str] = (), persist_timeout: float = 600) -> Sandbox` (async: `-> AsyncSandbox`) SHALL require `sbx.persist` and the launch options recorded by `create()` (a `connect()` handle SHALL raise `InvalidArgumentException` naming `create(persist=)`), SHALL run `checkpoint_files(exclude=exclude, timeout=persist_timeout)`, then `Sandbox.create(**recorded options, persist=sbx.persist, persist_timeout=persist_timeout)` (same template ARN and version, `timeout`, `idle`, `envs`, `metadata`, `cpu_time_limit`, `execution_role_arn`, `allowed_ports`, `ingress`, `egress`, `logging`, control plane, transport, `ready_timeout`, `request_timeout`, `reconnect_timeout`, `keep_on_failure`, and the explicit `access_token` if one was given), then `kill()` the old VM (`SandboxNotFoundException` suppressed) and `close()` the old handle, returning the new sandbox. If `create` fails the old sandbox SHALL be left running and the exception SHALL propagate with a note that the checkpoint under `sbx.persist.uri` is complete. Docstrings and `persistence.md` SHALL state that kernel variables, processes and PTYs do not survive (ADR-007). TypeScript: `reincarnate({ exclude, persistTimeoutMs }) → Sandbox` with the same ordering. `rayito.e2b` SHALL NOT gain `persist`. The recorded options SHALL include every 0.6 option that `create()` turns into a `ConfigureSandbox` section (`mounts=`, `events=`, `telemetry=`, `gateways=`, kept in `LaunchOptions.features` without `size=`, which is already part of the resolved template ARN), and the successor's `create()` SHALL re-plan and re-apply them through the same single `Configure` path as any `create()` (`plan_features` → `planned_sections` → `_apply_configure_sections`; TypeScript `planFeatures` → `plannedSections` → `#applyConfigureSections`) with the successor's own launch facts: `events=` SHALL derive `k_sbx` from the new `sandbox_id`, `mounts=` SHALL wait until every mount is `mounted` before `reincarnate()` returns, `telemetry=` SHALL use the successor's image ARN, version and guest memory, and every `gateways=` header SHALL be resolved again. A sandbox created without any of them SHALL send no `Configure` on reincarnation.

#### Scenario: ordering against the fakes
- **WHEN** `new = sbx.reincarnate()` runs for a sandbox created with `persist=S3Prefix("b", name="n")` and `metadata={"k": "v"}`
- **THEN** the recorded sequence is `Checkpoint` on the old endpoint, `run-microvm` with the same image ARN and metadata, `Restore` on the new endpoint with `key_prefix "rayito/n"`, `terminate-microvm` of the old id; `new.sandbox_id != sbx.sandbox_id`, `new.persist == sbx.persist`, `new.metadata == {"k": "v"}`, and `sbx` is closed

#### Scenario: create failure keeps the old sandbox
- **WHEN** the fake plane makes `run-microvm` raise during `reincarnate()`
- **THEN** the exception propagates, the fake recorded the `Checkpoint` and no `terminate-microvm`, and `sbx.is_running()` is still `True`

#### Scenario: connect handles cannot reincarnate
- **WHEN** `Sandbox.connect(id, access_token=..., persist=S3Prefix("b", name="n")).reincarnate()` is called
- **THEN** `InvalidArgumentException` is raised before any RPC

#### Scenario: every Configure section is replayed with the successor's facts
- **WHEN** `new = sbx.reincarnate()` (sync, async and TypeScript) runs for a sandbox created with `persist=`, `mounts={"/mnt/data": S3Mount(...)}`, `telemetry=TelemetryExport()` and `events=` (a deployed `LifecycleEvents`), against an agent whose mounts answer `PENDING` and settle on the second `ConfigureStatus`
- **THEN** the fake agent receives exactly two `Configure` requests, each with `s3_mounts`, `telemetry_export` and `lifecycle_events`; the second one carries `lifecycle_events.sandbox_id == new.sandbox_id` and `sandbox_key == derive_sandbox_key(stack_key, new.sandbox_id)` (different from the original's), and `ConfigureStatus` was polled twice per sandbox before `reincarnate()` returned

#### Scenario: no 0.6 options, no Configure
- **WHEN** a sandbox created with only `persist=` is reincarnated
- **THEN** no `Configure` request reaches the agent

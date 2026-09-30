## ADDED Requirements

### Requirement: EfsVolume value type
The Python SDK SHALL export `rayito.EfsVolume(file_system_id: str, access_point_id: str, name: str | None = None, region: str | None = None, read_only: bool = False, mount_target_ip: str | None = None)`, a frozen dataclass that validates the ids with the `efs` service-model patterns and the IPv4 address at construction and raises `InvalidArgumentException` otherwise. The TypeScript SDK SHALL export an `EfsVolume` class with the same fields in camelCase and `InvalidArgumentError`.

#### Scenario: validation
- **WHEN** the unit test constructs `EfsVolume("fs-1", "fsap-0123456789abcdef0")` and `EfsVolume("fs-0123456789abcdef0", "fsap-0123456789abcdef0", mount_target_ip="10.0.0.300")`
- **THEN** both raise `InvalidArgumentException` and a volume with valid ids and `mount_target_ip="10.0.1.25"` is created

### Requirement: VolumeStore manages access points with the caller's credentials
`rayito.VolumeStore(file_system_id, region=None, session=None, root="/rayito-volumes")` SHALL provide `create(name) -> EfsVolume`, `get(name) -> EfsVolume`, `list() -> list[EfsVolume]`, `destroy(name) -> bool` and `purge(name, template, execution_role_arn, egress) -> bool`, using only the EFS operations recorded in `AWS_API_NOTES.md` §19. `create` SHALL reject names that are not 1–63 characters of letters, digits and hyphens, SHALL fail with `VolumeAlreadyExistsException` when an access point tagged `rayito:volume=<name>` exists on the file system, and SHALL create the access point with `RootDirectory.Path` `<root>/<name>`, `CreationInfo` owner 1000:1000 permissions `0750` and `PosixUser` 1000:1000. `destroy` SHALL delete the access point and SHALL document that the directory's data remains until `purge`, which mounts the access point in an ephemeral sandbox, deletes its content and then deletes the access point. TypeScript SHALL mirror the surface with AWS SDK v3.

#### Scenario: create against a fake EFS client
- **WHEN** `store.create("agente-7")` runs against a fake client with no access points
- **THEN** the fake received one `CreateAccessPoint` whose root path is `/rayito-volumes/agente-7`, whose POSIX user is 1000:1000 and whose tags include `rayito:volume=agente-7`, and a second `create("agente-7")` raises `VolumeAlreadyExistsException`

### Requirement: create(volumes=) mounts or fails closed
`Sandbox.create` and `AsyncSandbox.create` SHALL accept `volumes: Mapping[str, EfsVolume] | None = None` and `volume_timeout: float = 60`. With `volumes` and no `execution_role_arn`, or with no egress connector in `egress`, they SHALL raise `InvalidArgumentException` before any AWS call. They SHALL resolve a missing `mount_target_ip` with `DescribeMountTargets` on the caller's credentials, call `VolumeService.Mount` after readiness, and wait until every volume is `MOUNTED`; if the agent reports `volumes_supported=false` or answers `UNIMPLEMENTED` they SHALL terminate the VM (unless `keep_on_failure`) and raise `UnimplementedError(feature="volumes")`; on `FAILED` or on the timeout they SHALL do the same and raise `VolumeMountException(code=<class>)`. `sbx.volumes` SHALL expose the published `VolumeStatus` per path, and after `resume()` the SDK SHALL warn once per resume generation when `volume_state_lost` is set. Taking a sandbox from a pool with `volumes` SHALL mount through the same RPC. TypeScript: `create({ volumes, volumeTimeoutMs = 60_000 })`, `sbx.volumes`, `VolumeMountError`.

#### Scenario: preconditions
- **WHEN** the unit test calls `Sandbox.create(template, volumes={"/mnt/v": vol}, control_plane=fake_plane)` without `execution_role_arn`, and again with a role but no egress connector
- **THEN** both raise `InvalidArgumentException` and the fake plane recorded no `run-microvm`

#### Scenario: agent without volume support
- **WHEN** the fake agent's `Health` has `volumes_supported=false`
- **THEN** `create()` raises `UnimplementedError`, the fake plane recorded one `terminate-microvm`, and with `keep_on_failure=True` it recorded none

### Requirement: E2B shim volumes
`rayito.e2b.Volume` and `AsyncVolume` SHALL implement `create(name)`, `connect(volume_id)`, `list()`, `get_info(volume_id)` and `destroy(volume_id) -> bool` over `VolumeStore`, configured by the bound `E2B(...)` client or `RAYITO_EFS_FILE_SYSTEM_ID`, with `volume_id` equal to the access-point id and `name` from its tag. `Sandbox.create(volume_mounts={path: Volume | name})` SHALL resolve names through the store and delegate to `create(volumes=)` with the connector from the bound client or `RAYITO_EFS_CONNECTOR_ARN` and the role from `RAYITO_EFS_ROLE_ARN`, and `get_info().volume_mounts` SHALL list `{"name", "path"}` for each mounted volume. `volume.read_file`, `write_file`, `make_dir`, `list` on a path and `remove` SHALL raise `UnimplementedError` whose reason says there is no data plane outside a sandbox. The TypeScript shim SHALL mirror this with `volumeMounts`.

#### Scenario: shim mapping
- **WHEN** the unit test creates `Volume.create("datos")` against the fake store and then `Sandbox.create(volume_mounts={"/mnt/d": "datos"})` against the fakes
- **THEN** the native `create` received `volumes={"/mnt/d": EfsVolume(..., name="datos")}`, `get_info().volume_mounts == [{"name": "datos", "path": "/mnt/d"}]`, and `volume.read_file("/x")` raises `UnimplementedError`

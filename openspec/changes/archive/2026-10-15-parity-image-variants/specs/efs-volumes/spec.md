## MODIFIED Requirements

### Requirement: volumes= is validated before any AWS call
`plan_volumes`/`planVolumes` SHALL reject, with `InvalidArgumentException`/`InvalidArgumentError` and no AWS call, an empty mapping, more than `EFS_VOLUMES_MAX_PER_SANDBOX` (4, `limits.json`) volumes, a value that is not an `EfsVolume`, and a mount-path set that violates the shared mount-path rule (absolute, canonical, under `/mnt/` or `/home/user/`, no overlap). It SHALL then raise `UnimplementedError` naming the `base-caps-efs` requirement when the image variant is known and is not `base-caps-efs` (or it with a size suffix). Because a MicroVM accepts a single egress connector (`AWS_API_NOTES.md` §16 Q131), it SHALL then reject with `InvalidArgumentException`/`InvalidArgumentError`, naming the alternative of reaching the internet through the customer's VPC (NAT or transit gateway), an `egress` that is absent or empty, that contains `INTERNET_EGRESS` (by managed name or ARN), or that holds more than one connector; and it SHALL then reject a missing `execution_role_arn`/`executionRoleArn`, since `amazon-efs-utils` signs the tunnel with the execution role's IMDS credentials.

#### Scenario: an empty mapping is rejected before any AWS call
- **WHEN** `Sandbox.create(..., volumes={})` is called
- **THEN** `InvalidArgumentException`/`InvalidArgumentError` is raised and no `run-microvm`/`RunMicrovm` call is made

#### Scenario: a non-caps image variant is rejected with the caps-specific message
- **WHEN** `Sandbox.create("rayito-base", volumes={"/mnt/v": EfsVolume(...)})` is called (a known variant other than `base-caps-efs`)
- **THEN** `UnimplementedError` naming the `base-caps-efs` requirement is raised before any AWS call

#### Scenario: the caps image without efs-utils is rejected for volumes
- **WHEN** `Sandbox.create("rayito-base-caps", volumes={"/mnt/v": EfsVolume(...)})` is called
- **THEN** `UnimplementedError` naming the `base-caps-efs` requirement is raised before any AWS call

#### Scenario: volumes combined with internet egress is rejected before launch
- **WHEN** `Sandbox.create("rayito-base-caps-efs", volumes={...}, egress=[<connector>, "INTERNET_EGRESS"])` is called, or `egress` is omitted
- **THEN** `InvalidArgumentException`/`InvalidArgumentError` is raised naming the VPC NAT/transit-gateway alternative, and no `run-microvm`/`RunMicrovm` call is made

#### Scenario: a volume without an execution role is rejected before launch
- **WHEN** `Sandbox.create("rayito-base-caps-efs", volumes={...}, egress=[<connector>])` is called without `execution_role_arn`
- **THEN** `InvalidArgumentException`/`InvalidArgumentError` is raised and no `run-microvm`/`RunMicrovm` call is made

### Requirement: the E2B shim's Volume resource is capability-gated the same way as the native SDK
`rayito.e2b.Volume`/`AsyncVolume` (TypeScript: the `e2b` module's `Volume`) `create`/`connect`/`list`/`get_info`/`destroy` SHALL delegate to a `VolumeStore` configured on the `E2B` client. The store SHALL be bound on the client only (Python: the sync `VolumeStore`; anything else is `InvalidArgumentException`). `volume_id`/`volumeId` SHALL be the volume's logical name, the same identifier `connect`/`get_info`/`destroy` take. `Sandbox.create(volume_mounts={...})` SHALL, after the `mcp`/`iam` rejections and before any AWS call, require the bound store, an `E2B(volume_connector_arn=...)`/`volumeConnectorArn` (the single egress connector of that sandbox, replacing `INTERNET_EGRESS`), and no explicit `allow_internet_access=True`, and validate paths and the efs variant; it SHALL then map a `Volume` with its access point id without any AWS call, resolve a plain name with `VolumeStore.get`, and launch through the native `volumes=` path. Content operations (`read_file`/`write_file`/`make_dir`/`list_files`/`remove`/`update_metadata`) on a `Volume` SHALL raise `UnimplementedError("volume.content")`, since no data plane exists outside a MicroVM.

#### Scenario: shim CRUD delegates to VolumeStore
- **WHEN** `e2b.Volume.create("datos-7")` is called on an `E2B` client configured with a `VolumeStore`
- **THEN** it performs the same `CreateAccessPoint` call `VolumeStore.create` would

#### Scenario: shim content operations are unimplemented
- **WHEN** `volume.read_file(...)` (or any other content operation) is called on any `Volume`
- **THEN** `UnimplementedError("volume.content")` is raised, never a partial read

#### Scenario: the E2B volume id round-trips
- **WHEN** `vol = Volume.create("ws")` and then `Volume.destroy(vol.volume_id)`
- **THEN** the access point `create` made is deleted and `destroy` returns `True`

#### Scenario: volume_mounts launches through the volume connector only
- **WHEN** `Sandbox.create("rayito-base-caps-efs", volume_mounts={"/mnt/v": "ws"}, execution_role_arn=...)` runs on a client bound to a `VolumeStore` and a `volume_connector_arn`
- **THEN** the native `create` receives `egress=[<that connector>]` and `volumes={"/mnt/v": <ws's EfsVolume>}`, and never `INTERNET_EGRESS`

#### Scenario: volume_mounts without a connector never calls AWS
- **WHEN** the client has a `VolumeStore` but no `volume_connector_arn`
- **THEN** `InvalidArgumentException`/`InvalidArgumentError` naming `volume_connector_arn` is raised and no AWS call is made

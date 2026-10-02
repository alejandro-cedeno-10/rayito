## ADDED Requirements

### Requirement: volumes= and VolumeStore are off by default
Importing `rayito`/`rayito` (TS) SHALL NOT construct an `efs` client. Neither SHALL constructing a `VolumeStore`/`AsyncVolumeStore`/TypeScript `VolumeStore` with no method called on it. `Sandbox.create()`/`AsyncSandbox.create()` with `volumes` absent or `None`/`undefined` SHALL make exactly the same boto3/AWS-SDK-v3 calls and gRPC calls as a build with no `m15-efs-volumes` code at all.

#### Scenario: constructing a VolumeStore makes no AWS call
- **WHEN** `VolumeStore(file_system_id="fs-...")` (or the TypeScript/async equivalent) is constructed and never used
- **THEN** no `efs` client is built and no network call is made

#### Scenario: the zero-cost golden trace is unaffected
- **WHEN** the scripted session (`create → commands.run → files.write → pause → resume → commands.run → kill → list`) runs with `volumes` absent
- **THEN** the recorded boto3/AWS-SDK-v3 and gRPC sequences equal the checked-in zero-cost fixture exactly

### Requirement: volumes= validates its shape before any AWS call and always raises UnimplementedError
`require_volume_support`/`requireVolumeSupport` SHALL reject, with `InvalidArgumentException`/`InvalidArgumentError` and no AWS call, an empty mapping, a value that is not an `EfsVolume`, and a mount-path set that violates the shared mount-path rule (absolute, canonical, under `/mnt/` or `/home/user/`, no overlap, at most 4 combined with `mounts=`). Given a well-formed mapping, it SHALL raise `UnimplementedError` naming the pending EFS-1..EFS-20 measurement campaign, after first raising `UnimplementedError` naming the `base-caps` requirement when the image variant is known and is not `base-caps` or a derived size suffix.

#### Scenario: an empty mapping is rejected before any AWS call
- **WHEN** `Sandbox.create(..., volumes={})` is called
- **THEN** `InvalidArgumentException`/`InvalidArgumentError` is raised and no `run-microvm`/`RunMicrovm` call is made

#### Scenario: a non-caps image variant is rejected with the caps-specific message
- **WHEN** `Sandbox.create("rayito-base", volumes={"/mnt/v": EfsVolume(...)})` is called (a known, non-caps variant)
- **THEN** `UnimplementedError` naming the `base-caps` requirement is raised before any AWS call

#### Scenario: a well-formed request on the caps variant still raises, naming the measurement campaign
- **WHEN** `Sandbox.create("rayito-base-caps", volumes={"/mnt/v": EfsVolume(...)})` is called
- **THEN** `UnimplementedError` is raised naming `m15-efs-volumes` and the EFS-1..EFS-20 campaign, and no `run-microvm`/`RunMicrovm` call is made

### Requirement: VolumeStore CRUD is real and uses only the documented EFS parameters
`VolumeStore.create` SHALL call `CreateAccessPoint` with `PosixUser={Uid: 1000, Gid: 1000}`, `RootDirectory.Path` under `/rayito-volumes/`, a `ClientToken` derived deterministically from the volume name, and a `Tags` entry naming the volume; it SHALL be idempotent (two `create()` calls with the same name return the same access point). `get`/`list` SHALL use `DescribeAccessPoints` filtered to the store's `FileSystemId`. `destroy` SHALL use `DeleteAccessPoint` and SHALL NOT delete the directory's contents. No operation SHALL use an EFS API parameter absent from `AWS_API_NOTES.md` §22.

#### Scenario: create is idempotent by name
- **WHEN** `store.create("datos-7")` is called twice with the same `VolumeStore`
- **THEN** both calls return the same `access_point_id`/`accessPointId` and only one access point exists

#### Scenario: destroy never deletes the directory's data
- **WHEN** `store.destroy("datos-7")` succeeds
- **THEN** only the access point is deleted; the underlying directory and its contents are left in place

### Requirement: the efs-volumes OptionalStack component is real and never deploys implicitly
The `efs-volumes` `StackComponent` SHALL be `supported`. `OptionalStacks.deploy("efs-volumes")`/`destroy("efs-volumes")` SHALL only run when called explicitly (by the SDK or `rayito stack`); no `Sandbox.create()` or `VolumeStore` call SHALL trigger a stack operation. `infra/efs-volumes.yaml` SHALL create mount-target ingress limited to its own connector's security group and SHALL always retain the file system and its data on stack deletion and on replacement, with no parameter able to change which file-system resource exists.

#### Scenario: deploying efs-volumes is always explicit
- **WHEN** a sandbox is created with `volumes=` unset, or `VolumeStore` is used
- **THEN** no `CreateStack`/`UpdateStack`/`DeleteStack` call for the `efs-volumes` component is made

#### Scenario: destroy always retains the data
- **WHEN** `rayito stack destroy efs-volumes` runs
- **THEN** the file system and its data survive stack deletion; deleting them is a separate, explicit `aws efs delete-file-system`

#### Scenario: a redeploy never swaps the file system
- **WHEN** `rayito stack deploy efs-volumes` runs again on an existing stack, with or without parameters (e.g. to add `SubnetId2`)
- **THEN** the same `AWS::EFS::FileSystem` resource stays in place with the same `FileSystemId`; no parameter selects, conditions or replaces it

#### Scenario: AllowWrite is reachable
- **WHEN** `deploy("efs-volumes", parameters={"AllowWrite": "false"})` or `--param AllowWrite=false` is used
- **THEN** the component accepts it and `RayitoEfsVolumeClient` grants `ClientMount` only

### Requirement: the E2B shim's Volume resource is capability-gated the same way as the native SDK
`rayito.e2b.Volume`/`AsyncVolume` (TypeScript: the `e2b` module's `Volume`) `create`/`connect`/`list`/`get_info`/`destroy` SHALL delegate to a `VolumeStore` configured on the `E2B` client. The store SHALL be bound on the client only (Python: the sync `VolumeStore`; anything else is `InvalidArgumentException`). `volume_id`/`volumeId` SHALL be the volume's logical name, the same identifier `connect`/`get_info`/`destroy` take. `Sandbox.create(volume_mounts={...})` SHALL go through the same I/O-free gate as `volumes=` (paths, caps, then `UnimplementedError`) after the `mcp`/`iam` rejections and before any AWS call, never resolving a name while no mounter exists. Content operations (`read_file`/`write_file`/`make_dir`/`list_files`/`remove`/`update_metadata`) on a `Volume` SHALL raise `UnimplementedError("volume.content")`, since no data plane exists outside a MicroVM.

#### Scenario: shim CRUD delegates to VolumeStore
- **WHEN** `e2b.Volume.create("datos-7")` is called on an `E2B` client configured with a `VolumeStore`
- **THEN** it performs the same `CreateAccessPoint` call `VolumeStore.create` would

#### Scenario: shim content operations are unimplemented
- **WHEN** `volume.read_file(...)` (or any other content operation) is called on any `Volume`
- **THEN** `UnimplementedError("volume.content")` is raised, never a partial read

#### Scenario: the E2B volume id round-trips
- **WHEN** `vol = Volume.create("ws")` and then `Volume.destroy(vol.volume_id)`
- **THEN** the access point `create` made is deleted and `destroy` returns `True`

#### Scenario: volume_mounts never calls AWS
- **WHEN** `Sandbox.create(volume_mounts={"/mnt/v": "ws"})` runs on a client bound to a `VolumeStore`
- **THEN** no `DescribeAccessPoints` (or any other AWS call) is made and `UnimplementedError` is raised

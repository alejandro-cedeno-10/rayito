## ADDED Requirements

### Requirement: volumes= and VolumeStore are off by default
Importing `rayito`/`rayito` (TS) SHALL NOT construct an `efs` client. Neither SHALL constructing a `VolumeStore`/`AsyncVolumeStore`/TypeScript `VolumeStore` with no method called on it. `Sandbox.create()`/`AsyncSandbox.create()` with `volumes` absent or `None`/`undefined` SHALL make exactly the same boto3/AWS-SDK-v3 calls and gRPC calls as a build with no `m15-efs-volumes` code at all.

#### Scenario: constructing a VolumeStore makes no AWS call
- **WHEN** `VolumeStore(file_system_id="fs-...")` (or the TypeScript/async equivalent) is constructed and never used
- **THEN** no `efs` client is built and no network call is made

#### Scenario: the zero-cost golden trace is unaffected
- **WHEN** the scripted session (`create → commands.run → files.write → pause → resume → commands.run → kill → list`) runs with `volumes` absent
- **THEN** the recorded boto3/AWS-SDK-v3 and gRPC sequences equal the checked-in zero-cost fixture exactly

### Requirement: volumes= is validated before any AWS call
`plan_volumes`/`planVolumes` SHALL reject, with `InvalidArgumentException`/`InvalidArgumentError` and no AWS call, an empty mapping, more than `EFS_VOLUMES_MAX_PER_SANDBOX` (4, `limits.json`) volumes, a value that is not an `EfsVolume`, and a mount-path set that violates the shared mount-path rule (absolute, canonical, under `/mnt/` or `/home/user/`, no overlap). It SHALL then raise `UnimplementedError` naming the `base-caps` requirement when the image variant is known and is neither `base-caps`, `base-caps-efs` nor one of them with a size suffix. Because a MicroVM accepts a single egress connector (`AWS_API_NOTES.md` §16 Q131), it SHALL then reject with `InvalidArgumentException`/`InvalidArgumentError`, naming the alternative of reaching the internet through the customer's VPC (NAT or transit gateway), an `egress` that is absent or empty, that contains `INTERNET_EGRESS` (by managed name or ARN), or that holds more than one connector; and it SHALL then reject a missing `execution_role_arn`/`executionRoleArn`, since `amazon-efs-utils` signs the tunnel with the execution role's IMDS credentials.

#### Scenario: an empty mapping is rejected before any AWS call
- **WHEN** `Sandbox.create(..., volumes={})` is called
- **THEN** `InvalidArgumentException`/`InvalidArgumentError` is raised and no `run-microvm`/`RunMicrovm` call is made

#### Scenario: a non-caps image variant is rejected with the caps-specific message
- **WHEN** `Sandbox.create("rayito-base", volumes={"/mnt/v": EfsVolume(...)})` is called (a known, non-caps variant)
- **THEN** `UnimplementedError` naming the `base-caps` requirement is raised before any AWS call

#### Scenario: volumes combined with internet egress is rejected before launch
- **WHEN** `Sandbox.create("rayito-base-caps-efs", volumes={...}, egress=[<connector>, "INTERNET_EGRESS"])` is called, or `egress` is omitted
- **THEN** `InvalidArgumentException`/`InvalidArgumentError` is raised naming the VPC NAT/transit-gateway alternative, and no `run-microvm`/`RunMicrovm` call is made

#### Scenario: a volume without an execution role is rejected before launch
- **WHEN** `Sandbox.create("rayito-base-caps-efs", volumes={...}, egress=[<connector>])` is called without `execution_role_arn`
- **THEN** `InvalidArgumentException`/`InvalidArgumentError` naming `execution_role_arn` is raised and no AWS call is made

### Requirement: create(volumes=) mounts through the single post-ready Configure
For a validated `volumes=`, `create()` SHALL, before `run-microvm`, resolve the mount target IP of every volume that carries none with exactly one `DescribeMountTargets(FileSystemId=...)` per file system and per `create()` (caller credentials), choosing the first `available` mount target by `AvailabilityZoneId`, and SHALL fail with `VolumeException`/`VolumeError` and launch nothing when a file system has none; volumes that all carry an IP SHALL build no `efs` client. After readiness it SHALL send the `efs_volumes` section in the same single `ConfigureSandbox` call as every other 0.6 section, with a call deadline of at least `VOLUME_APPLY_TIMEOUT_SECONDS`/`VOLUME_APPLY_TIMEOUT_MS` (rayd mounts inside the call, one volume at a time, up to its 15 s helper timeout each), and SHALL return only once every volume is `MOUNTED` (an `APPLIED` section; a `PENDING` one is polled with the same bound). If `Health.features.efs_volumes` is false it SHALL raise `UnimplementedError` naming the opt-in `amazon-efs-utils` image, and if the section is `FAILED`/`INVALID` or a volume ends `FAILED` or unmounted at the deadline it SHALL raise `VolumeMountException`/`VolumeMountError` with a closed `code`; in every failure case it SHALL terminate the sandbox unless `keep_on_failure`. `reincarnate()` SHALL send the section again (resolving the IPs again) for the successor. `sbx.volumes`/`volumes()` SHALL read each path's live state with one `ConfigureStatus` per read.

#### Scenario: the volumes travel in the one Configure with their resolved IPs
- **WHEN** `create(volumes={"/mnt/a": vol_fs1, "/mnt/b": vol_fs1_ro, "/mnt/c": vol_with_ip}, mounts={...})` succeeds
- **THEN** one `DescribeMountTargets` for the first file system was made before `run-microvm`, and the single `Configure` carries both `s3_mounts` and `efs_volumes`, with `/mnt/c`'s own IP and the resolved IP for the others, under a deadline of at least 65 s

#### Scenario: a failed mount terminates the sandbox
- **WHEN** rayd answers the `efs_volumes` section `FAILED` with `iam_denied`
- **THEN** `create()` raises `VolumeMountException(code="iam_denied")`/`VolumeMountError` and the MicroVM is terminated (kept with `keep_on_failure`)

#### Scenario: an image without amazon-efs-utils is refused after launch
- **WHEN** the agent reports `features.efs_volumes = false`
- **THEN** no `Configure` is sent, the MicroVM is terminated and `UnimplementedError` names the opt-in image

#### Scenario: reincarnate replays the volumes
- **WHEN** `reincarnate()` runs on a sandbox created with `volumes=`
- **THEN** the successor's `Configure` carries the same `efs_volumes` mounts

### Requirement: an opt-in image variant carries amazon-efs-utils
`rayito image zip --with-efs` (`image_zip.py --with-efs`) SHALL add a marker that makes the Dockerfile's conditional layer install the pinned `amazon-efs-utils` NEVRA and re-link `/usr/bin/python3` to Python 3.12 (the RPM re-points it to 3.9 and rayd's IMDS probe runs it by absolute path); without the marker the layer SHALL do nothing, so the default images do not change. `rayito image publish --with-efs` SHALL require `--os-capabilities ALL` and an artifact carrying the marker (and refuse a marked artifact without the flag) before any AWS call, defaulting the image name to `rayito-base-caps-efs`. `rayd` SHALL pass `AWS_REGION` to the mount helper, so the image needs no region in `efs-utils.conf`.

#### Scenario: the default image is unchanged
- **WHEN** an artifact is zipped without `--with-efs`
- **THEN** it carries no EFS marker, the conditional layer installs nothing and `Health.features.efs_volumes` stays false on that image

#### Scenario: publish refuses a mismatched artifact
- **WHEN** `rayito image publish --with-efs` is given an artifact without the marker, or no `--os-capabilities ALL`
- **THEN** it fails before any AWS call

### Requirement: rayd's EFS adapter owns everything a mount leaves behind
`rayd` SHALL report `efs_volumes` support only when it has `CAP_SYS_ADMIN`, `nfs4` in `/proc/filesystems`, `mount` on `PATH` and both `amazon-efs-utils` binaries; otherwise the section SHALL answer `UNSUPPORTED` and the slot SHALL NOT join the lifecycle hooks. A mount SHALL run the helper through the process-wide child registry onto a root-only staging directory and bind the result onto the requested path without following any symlink (a symlinked component is `invalid_path`). Unmounting a volume, including at `/terminate`, SHALL stop the `efs-proxy` processes that volume's mount started (identified by pid and start time) and no other. At `/suspend` the slot SHALL flush each mounted volume within its share of the suspend budget and mark a volume that did not finish `DEGRADED` with `flush_timeout`. At `/resume` the slot SHALL remount a volume whose tunnel credentials expired (or are within the refresh margin) without probing it, probe the others with a bounded child process and remount those that fail, wait at most a fixed budget inside the participant cap, and let slower remounts finish in the background with their state reported by `ConfigureStatus` (`REMOUNTING`, then `MOUNTED` or `DEGRADED` with the failure class); a remount SHALL NOT write into a volume entry that a later `Configure` dropped or changed.

#### Scenario: mount and unmount cycles leak no efs-proxy
- **WHEN** a volume is mounted and unmounted twenty times
- **THEN** no `efs-proxy` started by those mounts is left running, and a proxy that was already running before is untouched

#### Scenario: a pause past the credentials' expiry remounts the volume
- **WHEN** `/resume` runs for a volume whose recorded credentials expiry has passed
- **THEN** the volume is unmounted and mounted again without a probe, and its status returns to `MOUNTED` with the new expiry

#### Scenario: an unflushed volume is reported degraded
- **WHEN** a volume's flush does not finish within the participant's share at `/suspend`
- **THEN** its status is `DEGRADED` with `last_error_class` `flush_timeout`

#### Scenario: a symlinked mountpoint is refused
- **WHEN** a component of the requested mount path is a symlink at mount time
- **THEN** nothing is mounted over the symlink's target and the volume fails with `invalid_path`

### Requirement: VolumeStore CRUD is real and uses only the documented EFS parameters
`VolumeStore.create` SHALL call `CreateAccessPoint` with `PosixUser={Uid: 1000, Gid: 1000}`, `RootDirectory.Path` under `/rayito-volumes/`, a `ClientToken` derived deterministically from the volume name, and a `Tags` entry naming the volume; it SHALL be idempotent (two `create()` calls with the same name return the same access point). `get`/`list` SHALL use `DescribeAccessPoints` filtered to the store's `FileSystemId`. `destroy` SHALL use `DeleteAccessPoint` and SHALL NOT delete the directory's contents. Because `DescribeAccessPoints` is eventually consistent (`AWS_API_NOTES.md` §16 Q125), a `create()` answered `AccessPointAlreadyExists` SHALL retry `get` for a bounded budget before raising, and a `destroy()` whose `DeleteAccessPoint` answers `AccessPointNotFound` SHALL return `False`. No operation SHALL use an EFS API parameter absent from `AWS_API_NOTES.md` §22.

#### Scenario: create is idempotent by name
- **WHEN** `store.create("datos-7")` is called twice with the same `VolumeStore`
- **THEN** both calls return the same `access_point_id`/`accessPointId` and only one access point exists

#### Scenario: create tolerates a listing that lags behind
- **WHEN** `store.create("datos-7")` is answered `AccessPointAlreadyExists` while `DescribeAccessPoints` does not list that access point yet
- **THEN** it polls `get` until the access point is listed and returns it, raising `VolumeNotFoundException`/`VolumeNotFoundError` only once the bounded budget is spent

#### Scenario: destroy of an already-deleted access point still listed returns false
- **WHEN** `store.destroy("datos-7")` finds the volume in a stale listing and `DeleteAccessPoint` answers `AccessPointNotFound`
- **THEN** it returns `False`/`false` instead of raising

#### Scenario: destroy never deletes the directory's data
- **WHEN** `store.destroy("datos-7")` succeeds
- **THEN** only the access point is deleted; the underlying directory and its contents are left in place

### Requirement: the efs-volumes OptionalStack component is real and never deploys implicitly
The `efs-volumes` `StackComponent` SHALL be `supported`. `OptionalStacks.deploy("efs-volumes")`/`destroy("efs-volumes")` SHALL only run when called explicitly (by the SDK or `rayito stack`); no `Sandbox.create()` or `VolumeStore` call SHALL trigger a stack operation. `infra/efs-volumes.yaml` SHALL create mount-target ingress limited to its own connector's security group and SHALL always retain the file system and its data on stack deletion and on replacement, with no parameter able to change which file-system resource exists.

#### Scenario: deploying efs-volumes is always explicit
- **WHEN** a sandbox is created with `volumes=` unset, or `VolumeStore` is used
- **THEN** no `CreateStack`/`UpdateStack`/`DeleteStack` call for the `efs-volumes` component is made

#### Scenario: destroy retains the data unless deletion is asked for explicitly
- **WHEN** `rayito stack destroy efs-volumes` (or `EfsVolumes.destroy()`) runs
- **THEN** the file system and its data survive stack deletion; deleting them takes an explicit `EfsVolumes.destroy(delete_file_system=True)` or `EfsVolumes.delete_file_system(<id>)`

#### Scenario: a redeploy never swaps the file system
- **WHEN** `rayito stack deploy efs-volumes` runs again on an existing stack, with or without parameters (e.g. to add a subnet to `SubnetIds`)
- **THEN** the same `AWS::EFS::FileSystem` resource stays in place with the same `FileSystemId`; no parameter selects, conditions or replaces it

#### Scenario: AllowWrite is reachable
- **WHEN** `deploy("efs-volumes", parameters={"AllowWrite": "false"})` or `--param AllowWrite=false` is used
- **THEN** the component accepts it and `RayitoEfsVolumeClient` grants `ClientMount` only

### Requirement: the E2B shim's Volume resource is capability-gated the same way as the native SDK
`rayito.e2b.Volume`/`AsyncVolume` (TypeScript: the `e2b` module's `Volume`) `create`/`connect`/`list`/`get_info`/`destroy` SHALL delegate to a `VolumeStore` configured on the `E2B` client. The store SHALL be bound on the client only (Python: the sync `VolumeStore`; anything else is `InvalidArgumentException`). `volume_id`/`volumeId` SHALL be the volume's logical name, the same identifier `connect`/`get_info`/`destroy` take. `Sandbox.create(volume_mounts={...})` SHALL, after the `mcp`/`iam` rejections and before any AWS call, require the bound store, an `E2B(volume_connector_arn=...)`/`volumeConnectorArn` (the single egress connector of that sandbox, replacing `INTERNET_EGRESS`), and no explicit `allow_internet_access=True`, and validate paths and caps; it SHALL then map a `Volume` with its access point id without any AWS call, resolve a plain name with `VolumeStore.get`, and launch through the native `volumes=` path. Content operations (`read_file`/`write_file`/`make_dir`/`list_files`/`remove`/`update_metadata`) on a `Volume` SHALL raise `UnimplementedError("volume.content")`, since no data plane exists outside a MicroVM.

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

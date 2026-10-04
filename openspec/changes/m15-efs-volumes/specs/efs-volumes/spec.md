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
`require_volume_support`/`requireVolumeSupport` SHALL reject, with `InvalidArgumentException`/`InvalidArgumentError` and no AWS call, an empty mapping, a value that is not an `EfsVolume`, and a mount-path set that violates the shared mount-path rule (absolute, canonical, under `/mnt/` or `/home/user/`, no overlap, at most 4 combined with `mounts=`). It SHALL then raise `UnimplementedError` naming the `base-caps` requirement when the image variant is known and is not `base-caps` or a derived size suffix. Because a MicroVM accepts a single egress connector (`AWS_API_NOTES.md` §16 Q131), it SHALL then reject with `InvalidArgumentException`/`InvalidArgumentError`, naming the alternative of reaching the internet through the customer's VPC (NAT or transit gateway), an `egress` that is absent or empty, that contains `INTERNET_EGRESS` (by managed name or ARN), or that holds more than one connector. Given a well-formed request with exactly one own connector, it SHALL raise `UnimplementedError` stating that mounting needs an image with `amazon-efs-utils`, which no published image carries yet.

#### Scenario: an empty mapping is rejected before any AWS call
- **WHEN** `Sandbox.create(..., volumes={})` is called
- **THEN** `InvalidArgumentException`/`InvalidArgumentError` is raised and no `run-microvm`/`RunMicrovm` call is made

#### Scenario: a non-caps image variant is rejected with the caps-specific message
- **WHEN** `Sandbox.create("rayito-base", volumes={"/mnt/v": EfsVolume(...)})` is called (a known, non-caps variant)
- **THEN** `UnimplementedError` naming the `base-caps` requirement is raised before any AWS call

#### Scenario: volumes combined with internet egress is rejected before launch
- **WHEN** `Sandbox.create("rayito-base-caps", volumes={...}, egress=[<connector>, "INTERNET_EGRESS"])` is called, or `egress` is omitted
- **THEN** `InvalidArgumentException`/`InvalidArgumentError` is raised naming the VPC NAT/transit-gateway alternative, and no `run-microvm`/`RunMicrovm` call is made

#### Scenario: a well-formed request on the caps variant still raises
- **WHEN** `Sandbox.create("rayito-base-caps", volumes={"/mnt/v": EfsVolume(...)}, egress=[<connector>])` is called
- **THEN** `UnimplementedError` is raised naming the missing `amazon-efs-utils` image, and no `run-microvm`/`RunMicrovm` call is made

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

### Requirement: the efs-volumes stack deploys only new resources inside an existing VPC
`infra/efs-volumes.yaml` SHALL take `VpcId` (`AWS::EC2::VPC::Id`) and `SubnetIds` (`List<AWS::EC2::Subnet::Id>`, one to three subnets, one mount target per subnet) and SHALL create only new resources: the encrypted file system, its mount targets, a mount-target security group whose only ingress is TCP 2049 from a dedicated client security group (also created, used by the connector, whose only egress is TCP 2049 to the mount-target group), the VPC egress `AWS::Lambda::NetworkConnector` for MicroVMs, its operator role and the `RayitoEfsVolumeClient` policy. It SHALL NOT declare a VPC, subnet, route, route table, NACL, gateway or endpoint, and every security-group rule it declares SHALL belong to one of its own two groups. The file-system policy SHALL deny any request without `aws:SecureTransport`, without an access point or not coming through a mount target, and SHALL grant nothing. `RayitoEfsVolumeClient` SHALL grant `elasticfilesystem:ClientMount` (and `ClientWrite` unless `AllowWrite=false`) only on this file system, conditioned on the access point being one of `AccessPointArns` (or `ReadOnlyAccessPointArns`) when given or an access point of the stack's account and Region otherwise, SHALL explicitly deny `elasticfilesystem:ClientWrite` on every access point in `ReadOnlyAccessPointArns` whatever `AllowWrite` says, and SHALL never grant `ClientRootAccess`.

#### Scenario: a deploy never changes the caller's network
- **WHEN** the stack is deployed into an existing VPC and then destroyed
- **THEN** the VPC's route tables, NACLs and pre-existing security groups are identical before and after

#### Scenario: the mount targets accept NFS only from the client group
- **WHEN** the stack is deployed
- **THEN** the mount-target security group has exactly one ingress rule, TCP 2049 from the client security group, and no CIDR rule

#### Scenario: a read-only access point cannot be written even through the proxy tunnel
- **WHEN** the stack is deployed with `AllowWrite=true` and an access point in `ReadOnlyAccessPointArns`
- **THEN** `RayitoEfsVolumeClient` carries an explicit `Deny` of `elasticfilesystem:ClientWrite` conditioned on that access point

#### Scenario: the client policy can be narrowed to exact access points
- **WHEN** it is deployed with `AccessPointArns` set
- **THEN** `RayitoEfsVolumeClient` allows `ClientMount`/`ClientWrite` only for those access point ARNs

### Requirement: EfsVolumes checks the existing VPC read-only and is the explicit, reversible way to deploy it
`EfsVolumes`/`AsyncEfsVolumes` (TypeScript `EfsVolumes`) SHALL make no AWS call on construction. `check(vpc_id, subnet_ids)` SHALL first validate the request without I/O (VPC and subnet id formats, one to three distinct subnets) and then call only `ec2:DescribeVpcs`, `DescribeVpcAttribute`, `DescribeSubnets` and `DescribeRouteTables`, returning an `EfsNetworkReport` whose findings are `FAIL` for a missing or unavailable VPC or subnet, a subnet of another VPC, two subnets in one AZ or fewer than two free IPs in a subnet; `WARN` for a single AZ or a VPC without DNS support/hostnames; and an informational `internet-egress` finding stating that a MicroVM accepts one egress connector, so a sandbox with a volume cannot also use `INTERNET_EGRESS` and reaches the internet only through the VPC (its NAT or transit gateway and a connector that allows it). The report SHALL carry the component's `CostStatement`. `deploy()` SHALL run `check()` and SHALL NOT create anything when any finding is `FAIL`. `destroy(delete_file_system=True)` SHALL, after the stack is gone, wait for no mount target to remain, delete the file system's access points and the file system. `delete_file_system(id)` SHALL refuse a file system that does not carry the template's literal `rayito=efs-volumes` tag. `rayito doctor --efs-vpc-id ... --efs-subnet-ids ...` SHALL add an `efs-network` check built from the same evaluation; without those options the check SHALL NOT run and no EC2 call SHALL be made.

#### Scenario: check is read-only
- **WHEN** `EfsVolumes(...).check(vpc_id=..., subnet_ids=[...])` runs
- **THEN** only `Describe*` EC2 operations are called and nothing is created

#### Scenario: deploy refuses a VPC that fails the check
- **WHEN** `deploy()` is called with two subnets in the same AZ
- **THEN** `InvalidArgumentException`/`InvalidArgumentError` is raised and no `CreateStack`/`UpdateStack` call is made

#### Scenario: destroy can remove everything deploy created
- **WHEN** `destroy(delete_file_system=True)` runs on a deployed stack
- **THEN** the stack, every access point of its file system and the file system itself are deleted

#### Scenario: delete_file_system never deletes a foreign file system
- **WHEN** `delete_file_system(id)` is called on a file system without the `rayito=efs-volumes` tag
- **THEN** `VolumeException`/`VolumeError` is raised and nothing is deleted

### Requirement: the measurement script never creates or records the caller's VPC
`scripts/measure/efs_volumes.py run` SHALL take the existing VPC and subnets only from `--vpc-id`/`--subnet-ids` or `RAYITO_E2E_VPC_ID`/`RAYITO_E2E_SUBNET_IDS`, SHALL refuse to run without them before any AWS call, SHALL run `EfsVolumes.check` before deploying and stop without creating anything on a `FAIL`, and SHALL never create, modify, record or delete a VPC, subnet, route table or NACL. Every resource it creates SHALL carry the run's tags, and `cleanup` SHALL delete them (client-policy attachment, stack, retained file system) in dependency order.

#### Scenario: run without a network refuses before any AWS call
- **WHEN** `run` is invoked without `--vpc-id`/`--subnet-ids` and without the environment variables
- **THEN** it exits with status 2 and makes no AWS call

#### Scenario: cleanup never touches the network
- **WHEN** `cleanup --run-id <id>` runs
- **THEN** it deletes only the client-policy attachment, the stack and the retained file system, never a VPC or subnet

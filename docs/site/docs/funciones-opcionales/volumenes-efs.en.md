# EFS volumes (experimental)

<small>Since 0.7.0 ([What's new](../novedades/0.7.0.md#volumenes-efs)).</small>

A shared file system (NFS) **in your account**, mounted inside the
guest by `rayd`: the analogue of E2B's `Volume`. Unlike
`persist=` (an S3 copy restored/saved on `create`/`pause`), an EFS
volume is shared **live** between several sandboxes while they are
alive.

!!! warning "Experimental, and only with the optional image that brings `amazon-efs-utils`"
    `Sandbox.create(volumes=...)` really mounts, but only on an image
    that brings `amazon-efs-utils` and runs with `additionalOsCapabilities`
    `ALL`: the optional image `rayito-base-caps-efs`
    ([Image with amazon-efs-utils](#imagen-con-amazon-efs-utils)). The default
    images do not change and on them `Health.features.efs_volumes`
    is `false`: `create(volumes=...)` terminates the sandbox and raises
    `UnimplementedError`. The full study is in
    [`docs/research/2026-10-efs-persistence.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/docs/research/2026-10-efs-persistence.md).

!!! tip "Already have a VPC?"
    [EFS volumes in your VPC](volumenes-efs-vpc.md) explains how to check it
    without creating anything (`EfsVolumes.check`, `rayito doctor --efs-vpc-id`),
    deploy only new resources inside it and delete them all.

!!! info "Cost and activation"
    - **Default**: off. Without `VolumeStore(...)` or `volumes=`, Rayito
      builds no `efs` client and makes no AWS call.
    - **Turns on**: `VolumeStore(file_system_id=...)` for the volume CRUD
      (access points); `Sandbox.create(volumes={...})` to
      mount them, with `execution_role_arn=`, a single connector in `egress=` and
      the optional image `rayito-base-caps-efs`.
    - **Resources and AWS calls**: `VolumeStore.create` = `CreateAccessPoint`;
      `get`/`list` = `DescribeAccessPoints`; `destroy` = `DeleteAccessPoint`
      (AWS_API_NOTES.md §22). `create(volumes=...)` also makes one
      `DescribeMountTargets` per file system, before launching, if an
      `EfsVolume` carries no `mount_target_ip`. The file system, its mount targets, the
      NFS security group and the dedicated egress connector are created by
      `rayito stack deploy efs-volumes` (`infra/efs-volumes.yaml`), separately
      — `VolumeStore` never creates them.
    - **Approximate cost** (us-east-1, checked 2026-09-11, [EFS
      pricing](https://aws.amazon.com/efs/pricing/)): $0.30/GB-month the first
      month (Standard), $0.016/GB-month after 30 days (IA, with the template's
      lifecycle policy); $0.03/GB read and $0.06/GB written with
      Elastic Throughput. Access points have no listed charge of their own.
      An empty file system costs $0. The image with
      `amazon-efs-utils` takes ≈ 198 MB more of installed code
      (`AWS_API_NOTES.md` §16 Q122); traffic to a mount target in another
      AZ is billed by EC2 as cross-AZ transfer.
    - **IAM**: `elasticfilesystem:CreateAccessPoint/DescribeAccessPoints/
      DeleteAccessPoint` (and `DescribeMountTargets` to mount without
      `mount_target_ip`) on the file system (credentials of whoever
      calls the SDK). Inside the guest, the execution role needs
      `elasticfilesystem:ClientMount` (+ `ClientWrite` if the volume allows
      writing) conditioned on the `AccessPointArn`, never `ClientRootAccess`;
      a read-only volume goes in `read_only_access_point_arns`, which
      denies it `ClientWrite` (see [Truly read-only](#solo-lectura-de-verdad)).
    - **How to turn it off**: do not instantiate `VolumeStore` or pass `volumes=`.
      To stop paying: `VolumeStore.destroy(name)` deletes the access
      point (the directory's data is not deleted: see
      [Differences from E2B](#diferencias-con-e2b)); `rayito stack destroy
      efs-volumes` deletes the connector, the mount targets, the security
      groups and the IAM, but **always keeps** the file system
      and its data (`DeletionPolicy: Retain`, with no parameter that changes it:
      redeploying the stack can never delete or replace the data). To
      really delete them: `EfsVolumes(...).destroy(delete_file_system=True)`,
      or `EfsVolumes(...).delete_file_system("fs-…")` after a
      `rayito stack destroy` (see
      [EFS volumes in your VPC](volumenes-efs-vpc.md#3-borralo)).

## When to use it { #cuando-usarlo }

- Several sandboxes need to read and write the **same** directory at the
  same time, or a sandbox needs to see live what another one wrote — something
  `persist=` (checkpoint/restore) does not give: two sandboxes with `persist=` do not
  share anything live, and what is written between checkpoints is lost if the VM
  dies.
- **When not to**: a per-sandbox `HOME` that does not need to be shared live
  (there `persist=` is enough, it is cheaper and already works); a repository you
  only need once (clone it with `commands.run("git clone ...")`).

## `VolumeStore`: volume CRUD { #volumestore-crud-de-volumenes }

The CRUD works on an EFS file system that already exists
(deployed with `EfsVolumes.deploy`/`rayito stack deploy efs-volumes`, see
[EFS volumes in your VPC](volumenes-efs-vpc.md), or one of your own). Each
volume is an *access point* with `RootDirectory.Path =
/rayito-volumes/<name>` and a forced POSIX user 1000:1000.

=== "Python"

    ```python
    from rayito import VolumeStore

    store = VolumeStore(file_system_id="fs-0123abcd", region="us-east-1")  # (1)!
    vol = store.create("agent-data-7")  # (2)!
    store.get("agent-data-7")
    store.list()
    store.destroy("agent-data-7")  # (3)!
    ```

    1. Building it does not call AWS: the boto3 `efs` client is created on the
       first method.
    2. `CreateAccessPoint`, idempotent by name (`ClientToken` is a
       hash of `file_system_id`+name).
    3. `DeleteAccessPoint`; the directory itself is not deleted.

=== "Python (async)"

    ```python
    import asyncio

    from rayito import AsyncVolumeStore


    async def main() -> None:
        store = AsyncVolumeStore(file_system_id="fs-0123abcd", region="us-east-1")
        vol = await store.create("agent-data-7")
        print(vol.access_point_id)
        await store.destroy("agent-data-7")


    asyncio.run(main())
    ```

=== "TypeScript"

    ```ts
    import { VolumeStore } from "rayito"; // + npm i @aws-sdk/client-efs

    const store = new VolumeStore({ fileSystemId: "fs-0123abcd", region: "us-east-1" });
    const vol = await store.create("agent-data-7");
    await store.get("agent-data-7");
    await store.list();
    await store.destroy("agent-data-7");
    ```

!!! note "`list` and `get` take a few seconds to catch up"
    `DescribeAccessPoints` is eventually consistent (measured: up to
    ~11 s to list a new volume and ~8 s to stop listing a deleted
    one). `create` always returns the freshly created volume and, if the
    name already existed, waits up to 30 s for it to show up in the listing;
    `destroy` of a volume the listing still shows but that no longer exists
    returns `False`. A `get`/`list` right after `create` or `destroy`
    may see the previous state.

## `Sandbox.create(volumes=)`: mount volumes { #sandboxcreatevolumes-montar-volumenes }

=== "Python"

    ```python
    from rayito import Sandbox, VolumeStore

    store = VolumeStore(file_system_id="fs-0123abcd", region="us-east-1")
    data = store.create("agent-data-7")
    with Sandbox.create(
        "rayito-base-caps-efs",
        execution_role_arn="arn:aws:iam::123456789012:role/rayito-execution",
        egress=["arn:aws:lambda:us-east-1:123456789012:network-connector:rayito-efs"],
        volumes={"/mnt/data": data},
    ) as sbx:
        sbx.commands.run("echo hello > /mnt/data/greeting.txt")
        print(sbx.volumes["/mnt/data"].state)  # "mounted"
    ```

=== "TypeScript"

    ```ts
    import { Sandbox, VolumeStore } from "rayito";

    const store = new VolumeStore({ fileSystemId: "fs-0123abcd", region: "us-east-1" });
    const data = await store.create("agent-data-7");
    const sbx = await Sandbox.create({
      template: "rayito-base-caps-efs",
      executionRoleArn: "arn:aws:iam::123456789012:role/rayito-execution",
      egress: ["arn:aws:lambda:us-east-1:123456789012:network-connector:rayito-efs"],
      volumes: { "/mnt/data": data },
    });
    await sbx.commands.run("echo hello > /mnt/data/greeting.txt");
    console.log((await sbx.volumes()).get("/mnt/data")?.state); // "mounted"
    await sbx.kill();
    ```

What `create()` does, in order:

1. **Before launching, no network**: validates the request (1 to 4 volumes,
   paths under `/mnt/` or `/home/user/` without overlapping, `EfsVolume` values,
   `base-caps-efs` variant if the image name reveals it, exactly **one**
   own connector in `egress=` and `execution_role_arn=`). Any failure is
   `InvalidArgumentException`/`InvalidArgumentError` and nothing is launched.
2. **Before launching, with EFS**: if a volume carries no `mount_target_ip`,
   one `DescribeMountTargets` per file system (credentials of whoever
   calls; two volumes of the same file system share the
   response) and it picks the first `available` mount target by
   `AvailabilityZoneId`. With none available, `VolumeException`/
   `VolumeError` before launching.
3. **After readiness**: sends the `efs_volumes` section in the **same**
   `ConfigureSandbox` as `mounts=`, `gateways=`, `telemetry=` and `events=`.
   `rayd` mounts the volumes one by one within that call, whose
   deadline is at most 65 s (4 volumes × the helper's 15 s, plus margin);
   `create()` only returns with all of them `mounted`.
4. **If something fails**: terminates the sandbox (unless `keep_on_failure=True`) and
   raises `VolumeMountException`/`VolumeMountError` with `code` (`network`,
   `iam_denied`, `not_found`, `tls`, `helper_missing`, `timeout`,
   `invalid_path` or `unknown`), or `UnimplementedError` if the image does not
   bring `amazon-efs-utils`.

Each `volumes=` value is an `EfsVolume`: the one `VolumeStore.create/get`
returns, or one you build yourself with
`EfsVolume(file_system_id=, access_point_id=, read_only=, mount_target_ip=)`
(TypeScript: `new EfsVolume({ fileSystemId, accessPointId, readOnly,
mountTargetIp })`) if you already know the access point. With `mount_target_ip`,
`create()` does not call `DescribeMountTargets`.

`sbx.volumes` (synchronous Python), `await sbx.volumes()` (async and
TypeScript) reads the live state of each path (`requested`, `mounting`,
`mounted`, `degraded`, `remounting`, `unmounted` or `failed`, with
`last_error_class`/`lastErrorClass` if it failed). `reincarnate()` sends the section again (and resolves
the IPs) in the successor.

## Image with amazon-efs-utils { #imagen-con-amazon-efs-utils }

`volumes=` only mounts on an image that brings EFS's *mount helper*
(`mount.efs` and `efs-proxy`, from AL2023's `amazon-efs-utils` package) and that
runs with `additionalOsCapabilities: ["ALL"]` (`CAP_SYS_ADMIN`). None of the
default images has it: `rayito-base`, `-slim`, `-caps` and `-poly` do not
change, and on them `Health.features.efs_volumes` stays `false` (with
those official names, and with their sizes, `create(volumes=...)` raises
`UnimplementedError` before launching, so nothing is started or billed;
only an image of your own or an ARN is launched and decided afterwards by
`Health.features.efs_volumes`; see [How the SDK recognizes the
variant](../images.md#como-reconoce-el-sdk-la-variante)). The image with efs-utils is a separate variant,
`rayito-base-caps-efs`, which you publish yourself:

```bash
make image-publish-caps-efs BUCKET=amzn-s3-demo-bucket
# or, step by step:
rayito image zip image image/rayito-image-efs.zip --sidecar kernel-sidecar --with-efs
rayito image publish --artifact image/rayito-image-efs.zip --with-efs \
    --os-capabilities ALL --bucket amzn-s3-demo-bucket --base-image-version 1
```

`--with-efs` adds the `kernel-sidecar/efs_variant` marker to the zip; the
`Dockerfile`'s conditional layer then installs `amazon-efs-utils-3.1.3`
(pinned by version), relinks `/usr/bin/python3` to Python 3.12 (the
package brings Python 3.9 and repoints it; `rayd` uses that path) and checks that
`mount.efs` and `efs-proxy` exist. The region is not baked in: `rayd` passes it to the
helper on each mount (the MicroVM's `AWS_REGION`), so the same image
works in any region where you publish it. `rayito-base-caps-efs` counts
as a caps variant for `mounts=` and `telemetry=` just like `rayito-base-caps`.

!!! info "Image cost"
    Measured on real AWS (`AWS_API_NOTES.md` §16 Q122, AL2023 ARM64): 37 more
    packages (among them `nfs-utils`, `stunnel`, Python 3.9 and `systemd`, which nobody
    starts), **+~198 MB** of `codeInstallSizeInBytes` (image
    storage), the memory snapshot **does not change** and the build takes **≈ 10 s**
    more. Without `--with-efs` nothing is installed and no cost changes.

## Internet and volumes { #internet-y-volumenes }

A MicroVM supports **a single egress connector** (measured:
`egressNetworkConnectors=[INTERNET_EGRESS, <VPC connector>]` is an AWS
`ValidationException`, `AWS_API_NOTES.md` §16 Q131), and a volume
needs your VPC's connector to reach the mount target. That is why the SDK
rejects, **before launching anything** and with `InvalidArgumentException`/
`InvalidArgumentError`, a `volumes=` that:

- carries no `egress=` (the sandbox would inherit `INTERNET_EGRESS` from the image and
  would never reach the mount target);
- includes `INTERNET_EGRESS` (by name or by ARN);
- carries more than one connector.

If a sandbox with a volume needs the internet, it has to go out **through your
VPC**: a NAT or a transit gateway in the connector's subnets and a
connector whose security group allows that egress (the `efs-volumes` stack's
only lets NFS out). `EfsVolumes.check()` tells you how many of
your subnets have that route; see
[Internet egress](volumenes-efs-vpc.md#salida-a-internet).

## What `rayd` does with each volume { #lo-que-hace-rayd-con-cada-volumen }

This is what the mount adapter does on an image with
`amazon-efs-utils`, and what the 2026-10-04 acceptance runs measured
(`AWS_API_NOTES.md` §16 Q128–Q139):

- **Mount**: `mount -t efs -o tls,iam,accesspoint=…` with the execution
  role's credentials (without `systemd`: p50 313 ms, p95 589 ms), on a
  `rayd` directory the sandbox user cannot reach, and then
  linked to your path without ever following a symbolic link.
- **Unmount**: each mount starts its own `efs-proxy` and `umount` does not
  stop it; `rayd` terminates each volume's when unmounting it and when the
  sandbox ends, so processes do not pile up.
- **Pause and resume**: after pauses of 60 s and 10 min the volume stays
  the same. If the pause **crosses the expiry of the role's credentials**
  the tunnel was signed with (≈ 1 h), the volume would answer
  `Permission denied`: `rayd` detects it on resume and **remounts it**
  (measured after 70 min: `mounted` again in 0.7–1.3 s,
  `AWS_API_NOTES.md` §16 Q139) (or, if it does not know when they expire, it tests the volume with a bounded
  `stat` and remounts it if it fails). If the remount does not finish within the
  `/resume` hook's budget, it continues in the background and the volume's
  state goes through `remounting` until `mounted` (or `degraded` with the cause).
  Files the code had open on the volume before a
  remount error out when used: open them again.
- **Suspension**: before suspending, `rayd` flushes each volume's
  writes with a deadline (within the `/suspend` budget, at most
  5 s).

!!! danger "Unflushed writes + unreachable mount target = lost data"
    If on pause there are writes that have not yet reached EFS and the mount target
    does not respond (network down, security group changed), the flush does not
    finish in time, `rayd` answers `/suspend` within its deadline and **AWS
    terminates the MicroVM**: what was not flushed is lost and `pause()` raises
    `SandboxNotFoundException` (measured, `AWS_API_NOTES.md` §16 Q130). Nothing
    inside the guest can prevent it. If data matters, close it with
    `fsync` (or `sync`) before `pause()`; with the mount target reachable and
    no pending writes, the pause works.

## Truly read-only { #solo-lectura-de-verdad }

`EfsVolume(read_only=True)` mounts with `ro`, but **that alone does not prevent
writing**: `efs-proxy` listens on a local port and the sandbox user
(uid 1000) can connect to it and speak NFS through the already
authenticated tunnel, with the role's IAM permissions (measured, `AWS_API_NOTES.md` §16
Q133; the guest kernel does not allow filtering by user, Q48). What makes
a volume read-only is the **role's policy**: deploy the
stack with `read_only_access_point_arns`, which denies `ClientWrite` to those
access points even if the rest allow writing (or with `allow_write=False`
if no volume should allow it).

=== "Python"

    ```python
    from rayito import EfsVolumes

    efs = EfsVolumes(region="us-east-1")
    efs.deploy(
        vpc_id="vpc-0123456789abcdef0",
        subnet_ids=["subnet-0123456789abcdef0"],
        read_only_access_point_arns=[
            "arn:aws:elasticfilesystem:us-east-1:123456789012:access-point/fsap-0123456789abcdef0"
        ],
    )
    ```

=== "TypeScript"

    ```ts
    import { EfsVolumes } from "rayito";

    const efs = new EfsVolumes({ region: "us-east-1" });
    await efs.deploy({
      vpcId: "vpc-0123456789abcdef0",
      subnetIds: ["subnet-0123456789abcdef0"],
      readOnlyAccessPointArns: [
        "arn:aws:elasticfilesystem:us-east-1:123456789012:access-point/fsap-0123456789abcdef0",
      ],
    });
    ```

## E2B shim: `E2B(volume_store=...)` { #shim-e2b-e2bvolume_store }

`client.Volume`/`client.AsyncVolume` (`rayito.e2b`) are `UnimplementedError`
until the client binds a `VolumeStore` to them (only in the client's
constructor, never per call; in Python, the synchronous `VolumeStore`: an
`AsyncVolumeStore` is `InvalidArgumentException`). Once bound, the CRUD is the
same as above, with E2B's names (`Volume.create/connect/list/
get_info/destroy`). `volume_id`/`volumeId` is the volume's **name**, the
same identifier `connect`/`get_info`/`destroy` receive; the
`AccessPointId` goes separately, in `access_point_id`/`accessPointId`.
`Sandbox.create(volume_mounts={path: Volume|name})` mounts those volumes
through the same path as `volumes=` if the client also carries
`volume_connector_arn=`/`volumeConnectorArn` (the stack's `ConnectorArn`):
since a MicroVM only supports one egress connector, that sandbox goes out **only**
through that connector, never through `INTERNET_EGRESS`, and an explicit
`allow_internet_access=True` is `InvalidArgumentException`. A
`Volume` is mounted with its `access_point_id` without calling AWS; a text
name is looked up with `VolumeStore.get` (`DescribeAccessPoints`). It also
needs `execution_role_arn=` and the image with `amazon-efs-utils`. Without
`volume_connector_arn` it fails before any AWS call.

=== "Python"

    ```python
    from rayito import VolumeStore
    from rayito.e2b import E2B

    client = E2B(
        volume_store=VolumeStore(file_system_id="fs-0123abcd"),
        volume_connector_arn="arn:aws:lambda:us-east-1:123456789012:network-connector:rayito-efs",
    )
    vol = client.Volume.create("agent-data-7")  # without volume_store: UnimplementedError
    sbx = client.Sandbox.create(
        "rayito-base-caps-efs",
        execution_role_arn="arn:aws:iam::123456789012:role/rayito-execution",
        volume_mounts={"/mnt/data": vol},
    )
    sbx.kill()
    client.Volume.destroy(vol.volume_id)
    ```

=== "TypeScript"

    ```ts
    import { VolumeStore } from "rayito";
    import { E2B } from "rayito/e2b";

    const client = new E2B({
      volumeStore: new VolumeStore({ fileSystemId: "fs-0123abcd" }),
      volumeConnectorArn: "arn:aws:lambda:us-east-1:123456789012:network-connector:rayito-efs",
    });
    const vol = await client.Volume.create("agent-data-7");
    const sbx = await client.Sandbox.create("rayito-base-caps-efs", {
      executionRoleArn: "arn:aws:iam::123456789012:role/rayito-execution",
      volumeMounts: { "/mnt/data": vol },
    });
    await sbx.kill();
    await client.Volume.destroy(vol.volumeId);
    ```

## Errors and troubleshooting { #errores-y-solucion-de-problemas }

| Python | TypeScript | When | What to do |
|---|---|---|---|
| `InvalidArgumentException` | `InvalidArgumentError` | `volumes=` empty, with more than 4 volumes, with a value that is not an `EfsVolume`, with overlapping paths or outside `/mnt/`·`/home/user/`, or without `execution_role_arn=` | fix the shape of the request |
| `InvalidArgumentException` | `InvalidArgumentError` | `volumes=` without `egress=`, with `INTERNET_EGRESS` or with more than one connector | pass only the stack's `ConnectorArn`; internet, through your VPC ([Internet and volumes](#internet-y-volumenes)) |
| `UnimplementedError` | `UnimplementedError` | the image does not bring `amazon-efs-utils` (`Health.features.efs_volumes` is `false`: the sandbox was already terminated) or the name reveals a variant other than `base-caps-efs` | publish and use `rayito-base-caps-efs` ([Image with amazon-efs-utils](#imagen-con-amazon-efs-utils)) |
| `VolumeMountException` (`code`) | `VolumeMountError` (`code`) | a volume failed to mount: `iam_denied` (role policy or access point outside `access_point_arns`), `network` (connector or security group), `not_found`, `tls`, `timeout`, `invalid_path` (the path is a symbolic link)… The sandbox was already terminated (unless `keep_on_failure`) | check the role, the connector or the path according to `code` |
| `VolumeException` | `VolumeError` | the file system has no `available` mount target (before launching) | deploy the stack or wait for it to finish |
| `VolumeException` | `VolumeError` | `VolumeStore.create/get/list/destroy` failed (IAM, access point limit, file system unavailable) | check the permission or the file system's state |
| `VolumeNotFoundException` | `VolumeNotFoundError` | `get`/`destroy` on a name that does not exist | list with `VolumeStore.list()` |

## Differences from E2B { #diferencias-con-e2b }

- E2B (`Volume.create/connect/list/get_info/destroy`, private beta) has
  no data plane of its own for `read_file`/`write_file`/`make_dir`/
  `list`/`remove` on a volume without a sandbox — neither does Rayito: those
  operations are `UnimplementedError` in the shim (`rayito.e2b.Volume`),
  because there is no control-plane service outside a MicroVM
  (`SPEC.md` §4).
- `Volume.destroy` in E2B does not delete the directory's data; `VolumeStore.destroy`
  does not either (only the access point). Deleting the data requires mounting the file
  system from a sandbox.
- E2B does not document a mount limit; Rayito limits to 4 between `mounts=`
  and `volumes=` together (research doc §5).

## See also { #ver-tambien }

- [Optional features](../optional-features.md)
- [Persistence (S3)](../persistence.md) — the alternative with no VPC or special image, without live sharing
- [EFS volumes in your VPC](volumenes-efs-vpc.md) — `EfsVolumes`: check, deploy and delete
- [Optional stacks](pilas-opcionales.md) — `rayito stack deploy efs-volumes`
- Template: [`infra/efs-volumes.yaml`](https://github.com/alejandro-cedeno-10/rayito/blob/main/infra/efs-volumes.yaml)

??? info "Sources and measurements"
    - Full feasibility study: [`docs/research/2026-10-efs-persistence.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/docs/research/2026-10-efs-persistence.md).
    - EFS API contract: `AWS_API_NOTES.md` §22, on
      [GitHub](https://github.com/alejandro-cedeno-10/rayito/blob/main/AWS_API_NOTES.md).
    - Design decision: ADR-018 in
      [`ARCHITECTURE.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/ARCHITECTURE.md).
    - Measurement campaign: `scripts/measure/efs_volumes.py`
      (`plan`/`run`/`report`/`cleanup`), questions EFS-1..EFS-20; results
      in `AWS_API_NOTES.md` §16 Q122–Q134.
    - Threat and residual risks: `SECURITY.md` T21.

# EFS volumes in your VPC (experimental)

<small>Since 0.7.0, experimental ([What's new](../novedades/0.7.0.md#volumenes-efs)).</small>

The fast and safe way to set up
[EFS volumes](volumenes-efs.md) in a **VPC that already exists**: the
common case, because many accounts cannot create VPCs (an organization
policy often denies `ec2:CreateVpc`). `EfsVolumes` checks the VPC
without touching anything, deploys only **new** resources inside it and
deletes them all when you ask it to.

!!! warning "Experimental"
    The file system, the security groups, the connector, the volume CRUD
    (`VolumeStore`) and mounting with `Sandbox.create(volumes=...)`
    are real, but mounting requires the optional image with `amazon-efs-utils`
    (`rayito-base-caps-efs`, see
    [Image with amazon-efs-utils](volumenes-efs.md#imagen-con-amazon-efs-utils)
    and [Measured on real AWS](#medido-en-aws-real)).

## In five minutes { #en-cinco-minutos }

You need AWS credentials with permission for CloudFormation and EC2 in the
account, the id of an existing VPC and one to three private subnets of it,
each in a different AZ. With that:

1. **Check** the VPC without creating anything (`check()` or `rayito doctor
   --efs-vpc-id ... --efs-subnet-ids ...`): section [1](#1-comprueba-la-vpc-sin-crear-nada).
2. **Deploy** the stack (`deploy()` or `rayito stack deploy efs-volumes
   --param VpcId=... --param SubnetIds=...`): about 5 minutes, section
   [2](#2-despliega).
3. **Attach** the `CallerPolicyArn` output to the execution role you will
   launch the sandboxes with, and save `ConnectorArn`.
4. **Create** a volume with `efs.volume_store().create("name")`.
5. **Mount**: publish the image with `amazon-efs-utils` (`make
   image-publish-caps-efs` or `rayito image publish --with-efs
   --os-capabilities ALL`) and launch with it, `execution_role_arn=`,
   `egress=[ConnectorArn]` (only that connector: a sandbox with a volume does not use
   `INTERNET_EGRESS`, see [Internet egress](#salida-a-internet)) and
   `volumes={...}` ([Mount volumes](volumenes-efs.md#sandboxcreatevolumes-montar-volumenes)).

To remove everything: `destroy(delete_file_system=True)`, section
[3](#3-borralo).

!!! info "Cost and activation"
    - **Default**: off. Without an explicit call to
      `EfsVolumes.deploy(...)` (or `rayito stack deploy efs-volumes`) nothing is
      created; building `EfsVolumes` does not call AWS.
    - **Turns on**: `EfsVolumes(...).deploy(vpc_id=..., subnet_ids=[...])`
      in Python or `new EfsVolumes(...).deploy({ vpcId, subnetIds })` in
      TypeScript (peer `@aws-sdk/client-ec2` for the check and
      `@aws-sdk/client-efs` to delete the file system).
      `check()` and `rayito doctor --efs-vpc-id ... --efs-subnet-ids ...` are
      read-only.
    - **Resources and AWS calls**: `check()` only reads EC2
      (`DescribeVpcs`, `DescribeVpcAttribute`, `DescribeSubnets`,
      `DescribeRouteTables`). `deploy()` creates, tagged and inside the
      given VPC: an EFS file system encrypted at rest (AWS-managed KMS
      key), one mount target per subnet, two new security
      groups, an `AWS::Lambda::NetworkConnector` for egress to the
      VPC for MicroVMs, its operator role and the
      `RayitoEfsVolumeClient` policy. It **never** modifies the VPC, its subnets,
      route tables, NACLs or any existing security group.
    - **Approximate cost** (us-east-1, checked 2026-09-11, [EFS
      pricing](https://aws.amazon.com/efs/pricing/)): $0 with the file
      system empty; $0.30/GB-month (Standard) and $0.016/GB-month after 30 days
      (IA); $0.03/GB read and $0.06/GB written (Elastic Throughput). Mount
      targets, access points and the connector's ENIs have no listed charge.
      `check()`'s `Describe*` calls are free.
    - **IAM**: whoever deploys needs CloudFormation on the stack,
      to create the resources above (`CAPABILITY_IAM`) and the `ec2:Describe*`
      of `check()`; for `delete_file_system`,
      `elasticfilesystem:DescribeFileSystems`/`DescribeMountTargets`/
      `DescribeAccessPoints`/`DeleteAccessPoint`/`DeleteFileSystem`. The
      sandbox's execution role only receives the policy from the
      `CallerPolicyArn` output: `ClientMount` (and `ClientWrite` unless
      `allow_write=False` and except in `read_only_access_point_arns`) on
      **this** file system and its access points, never
      `ClientRootAccess`.
    - **How to turn it off**: `destroy()` deletes the stack (connector, groups,
      mount targets, role and policy) and **keeps** the file system
      with its data; `destroy(delete_file_system=True)` also deletes its
      access points and the file system itself: everything
      `deploy()` created. If you deleted the stack with `rayito stack destroy
      efs-volumes` (which always keeps it), `delete_file_system("fs-…")`
      deletes the one left behind; it only accepts one with the tag
      `rayito=efs-volumes` that the template puts on it.

## 1. Check the VPC (creating nothing) { #1-comprueba-la-vpc-sin-crear-nada }

`check()` validates that the VPC and the subnets exist, that the subnets belong to
that VPC and are in different AZs (EFS allows one mount target per AZ),
that they have free IPs left (one for the mount target and at least one for
a connector ENI) and whether the VPC resolves DNS. It warns about a single AZ (no
redundancy and with billed cross-AZ traffic) and tells you how a sandbox would
get to the internet. The report includes what `deploy()` would create and its
cost.

=== "CLI"

    ```bash
    rayito doctor --efs-vpc-id vpc-0123456789abcdef0 \
      --efs-subnet-ids subnet-0123456789abcdef0
    ```

=== "Python"

    ```python
    from rayito import EfsVolumes

    efs = EfsVolumes(region="us-east-1")  # (1)!
    report = efs.check(
        vpc_id="vpc-0123456789abcdef0",
        subnet_ids=["subnet-0123456789abcdef0"],
    )
    for finding in report.findings:
        print(finding.level, finding.code, finding.message)
    print("deploy would create:", report.cost.creates)
    ```

    1. Building it does not call AWS.

=== "TypeScript"

    ```ts
    import { EfsVolumes } from "rayito"; // + npm i @aws-sdk/client-ec2 @aws-sdk/client-efs

    const efs = new EfsVolumes({ region: "us-east-1" });
    const report = await efs.check({
      vpcId: "vpc-0123456789abcdef0",
      subnetIds: ["subnet-0123456789abcdef0"],
    });
    for (const finding of report.findings) {
      console.log(finding.level, finding.code, finding.message);
    }
    ```

| Finding | Level | What it means |
|---|---|---|
| `vpc-missing`, `subnet-missing` | FAIL | the VPC or a subnet does not exist in this account and region |
| `subnet-other-vpc` | FAIL | the subnet belongs to another VPC |
| `subnet-same-az` | FAIL | two subnets in the same AZ: EFS allows one mount target per AZ |
| `subnet-free-ips` | FAIL | fewer than 2 free IPs in a subnet |
| `vpc-unavailable`, `subnet-unavailable` | FAIL | they are not in `available` state |
| `vpc-dns-support`, `vpc-dns-hostnames` | WARN | without VPC DNS the file system name does not resolve (mounting by IP does work) |
| `single-az` | WARN | a single AZ |
| `internet-egress` | OK | informational: see [Internet egress](#salida-a-internet) |

## 2. Deploy { #2-despliega }

`deploy()` runs `check()` again and refuses (creating nothing) if any
finding is `FAIL`.

=== "CLI"

    ```bash
    rayito stack deploy efs-volumes \
      --param VpcId=vpc-0123456789abcdef0 \
      --param SubnetIds=subnet-0123456789abcdef0
    ```

=== "Python"

    ```python
    from rayito import EfsVolumes

    efs = EfsVolumes(region="us-east-1")
    status = efs.deploy(
        vpc_id="vpc-0123456789abcdef0",
        subnet_ids=["subnet-0123456789abcdef0"],
    )
    print(status.outputs["ConnectorArn"], status.outputs["CallerPolicyArn"])
    store = efs.volume_store()  # (1)!
    vol = store.create("agent-data-7")
    ```

    1. A `VolumeStore` over the stack's `FileSystemId`.

=== "Python (async)"

    ```python
    import asyncio

    from rayito import AsyncEfsVolumes


    async def main() -> None:
        efs = AsyncEfsVolumes(region="us-east-1")
        await efs.deploy(
            vpc_id="vpc-0123456789abcdef0",
            subnet_ids=["subnet-0123456789abcdef0"],
        )
        store = await efs.volume_store()
        await store.create("agent-data-7")


    asyncio.run(main())
    ```

=== "TypeScript"

    ```ts
    import { EfsVolumes } from "rayito";

    const efs = new EfsVolumes({ region: "us-east-1" });
    const status = await efs.deploy({
      vpcId: "vpc-0123456789abcdef0",
      subnetIds: ["subnet-0123456789abcdef0"],
    });
    console.log(status.outputs.ConnectorArn, status.outputs.CallerPolicyArn);
    const store = await efs.volumeStore();
    await store.create("agent-data-7");
    ```

`deploy()` options:

- `subnet_ids` / `subnetIds`: 1 to 3 subnets, one per AZ.
- `allow_write=False` / `allowWrite: false`: the execution role's policy
  only grants `ClientMount` (read).
- `access_point_arns` / `accessPointArns`: scopes the policy to those exact access
  points; empty (the default), to any access point of the
  account and region on **this** file system. Redeploy with the
  new list when you add a volume.
- `read_only_access_point_arns` / `readOnlyAccessPointArns`: those access
  points stay read-only even if the rest allow writing (the
  policy denies them `ClientWrite`). This is what makes a volume
  read-only: the mount's `ro` option is not enough, see
  [Truly read-only](volumenes-efs.md#solo-lectura-de-verdad). From the
  CLI: `--param ReadOnlyAccessPointArns=arn:...,arn:...`.
- `connector_name` / `connectorName`: connector name, unique in the
  account and region (`rayito-efs` by default).

Attach the `CallerPolicyArn` output to the execution role that will mount the
volumes, and pass `ConnectorArn` as the **only** connector in `egress=[...]`
when the mount exists.

### E2B shim { #shim-e2b }

The shim uses the same `VolumeStore`:

=== "Python"

    ```python
    from rayito import EfsVolumes
    from rayito.e2b import E2B

    client = E2B(volume_store=EfsVolumes(region="us-east-1").volume_store())
    vol = client.Volume.create("agent-data-7")
    ```

=== "TypeScript"

    ```ts
    import { EfsVolumes } from "rayito";
    import { E2B } from "rayito/e2b";

    const efs = new EfsVolumes({ region: "us-east-1" });
    const client = new E2B({ volumeStore: await efs.volumeStore() });
    const vol = await client.Volume.create("agent-data-7");
    ```

## What it creates and why it is safe { #que-crea-y-por-que-es-seguro }

| Resource (new) | Rule |
|---|---|
| EFS file system | encrypted at rest (AWS-managed KMS); its policy denies everything without TLS (`aws:SecureTransport`), every mount without an access point and everything that does not arrive through a mount target; it has no `Allow`, so an NFS client without IAM gets nothing |
| Mount targets (one per subnet) | in the mount target group |
| Mount target security group | only TCP 2049 inbound, and only from the client group (never a CIDR) |
| Client security group | used by the connector; its only egress is TCP 2049 to the mount target group |
| `AWS::Lambda::NetworkConnector` | egress to the VPC for MicroVMs, in your subnets, with the client group |
| Connector operator role | only the `ec2:*NetworkInterface*` actions the connector needs |
| `RayitoEfsVolumeClient` policy | `ClientMount`/`ClientWrite` on this file system and its access points, explicit `Deny` of `ClientWrite` on `ReadOnlyAccessPointArns`, never `ClientRootAccess` |

CloudFormation propagates the stack's tags
(`rayito:component=efs-volumes`, `rayito:managed-by`,
`rayito:sdk-version` and yours from `tags=`) to the resources that support
tags; the file system also carries, fixed in the template,
`rayito=efs-volumes`. The VPC, its subnets, route tables, NACLs and existing groups are never touched; the e2e
(`tests/e2e/test_m15_efs_volumes_vpc.py`) verifies it by comparing a
snapshot of all of them before and after.

## Internet egress { #salida-a-internet }

This stack's connector **only** lets NFS (2049) out to the mount
targets: it gives no internet egress. And a MicroVM supports **a single
egress connector**: asking for `INTERNET_EGRESS` and the VPC connector at the same time is an
AWS validation error (measured on 2026-10-04, `AWS_API_NOTES.md` §16
Q131). The SDK rejects it before launching anything: `Sandbox.create(volumes=...)`
without `egress=`, with `INTERNET_EGRESS` or with two connectors is
`InvalidArgumentException`/`InvalidArgumentError`, and `check()`'s
`internet-egress` finding reminds you of it. If a sandbox with a volume
needs the internet:

- it has to go out **through your VPC**: it depends on your subnets'
  default route (`check()` counts how many go to a NAT and how many to another
  gateway, such as a transit gateway) and on a connector of your own whose security
  group allows it
  ([`infra/egress-connector.yaml`](https://github.com/alejandro-cedeno-10/rayito/blob/main/infra/egress-connector.yaml));
  this stack creates none;
- with this stack's connector the sandbox does resolve DNS names (EFS's and public
  ones), but connects to nothing outside the mount targets.

## Measured on real AWS { #medido-en-aws-real }

The acceptance runs of 2026-10-04 (`AWS_API_NOTES.md` §16 Q126–Q139)
deployed this stack in an existing VPC and mounted with the
`rayito-base-caps-efs` image, first by hand and then through
`Sandbox.create(volumes=...)` in Python and TypeScript:

| What | Result |
|---|---|
| `deploy()` / `destroy(delete_file_system=True)` | ≈ 5 min; the VPC is identical after `destroy(delete_file_system=True)` |
| Connector to the mount target | `ACTIVE`; TCP 2049 in 0.1–0.5 s |
| `mount -t efs -o tls,iam,accesspoint` without systemd | 20 of 20; p50 313 ms, p95 589 ms |
| `create(volumes=...)` | a write volume and a read-only one `mounted` in 8–17 s (launch included); uid 1000 writes to one and gets `Read-only file system` on the other |
| Pause and resume with the volume mounted | data intact after 60 s and 10 min; first read ≤ 0.2 s; after 70 min (expired credentials), `rayd` remounts it on its own in `/resume`: `mounted` again in 0.7–1.3 s |
| Pause with the mount target unreachable | with no pending writes, pauses and resumes; **with pending writes, AWS terminates the MicroVM** and they are lost |
| Performance | 128 MB/s writing and 585 MB/s reading sequentially; ≈ 17 ms per small file |
| File system policy | without an access point, without TLS or without IAM: denied |
| `efs-proxy` after `umount` | `umount` does not stop it; `rayd` terminates it on unmount: 5 of 5 cycles go back to 0 processes |
| uid 1000 and `efs-proxy`'s local port | it can connect, but with `read_only_access_point_arns` a direct NFS mount through that port cannot write (`Read-only file system`); an access point outside `access_point_arns`: `iam_denied` |
| Mount point that is a symbolic link | `invalid_path`; nothing is mounted over the link's target |

## 3. Delete it { #3-borralo }

=== "Python"

    ```python
    from rayito import EfsVolumes

    efs = EfsVolumes(region="us-east-1")
    efs.destroy()  # (1)!
    efs.destroy(delete_file_system=True)  # (2)!
    efs.delete_file_system("fs-0123456789abcdef0")  # (3)!
    ```

    1. Deletes the stack and keeps the file system with its data.
    2. Also deletes its access points and the file system: everything
       `deploy()` created.
    3. After a `rayito stack destroy efs-volumes`: deletes the file
       system left behind (only one with the tag `rayito=efs-volumes`).

=== "TypeScript"

    ```ts
    import { EfsVolumes } from "rayito";

    const efs = new EfsVolumes({ region: "us-east-1" });
    await efs.destroy({ deleteFileSystem: true });
    ```

## Errors { #errores }

| Python | TypeScript | When |
|---|---|---|
| `InvalidArgumentException` | `InvalidArgumentError` | malformed ids, more than 3 subnets, an invalid access point ARN, or `deploy()` on a VPC with any `FAIL` (nothing was created) |
| `VolumeException` | `VolumeError` | `volume_store()` without a stack, or `delete_file_system` on one without the stack's tag or that does not release its mount targets |
| `StackException` | `StackError` | CloudFormation failed to deploy or delete |

## See also { #ver-tambien }

- [EFS volumes](volumenes-efs.md): `VolumeStore`, `volumes=` and the shim.
- [Optional stacks](pilas-opcionales.md): `rayito stack deploy|status|destroy`.
- Template: [`infra/efs-volumes.yaml`](https://github.com/alejandro-cedeno-10/rayito/blob/main/infra/efs-volumes.yaml)
- Contract of the EC2 and EFS calls: `AWS_API_NOTES.md` §22.

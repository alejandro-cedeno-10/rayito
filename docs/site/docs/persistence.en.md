# Persistence

A sandbox lives at most 8 h and `kill()` erases everything inside. Persistence
saves the user's `HOME` (`/home/user`) to your S3 bucket and restores it in
the next sandbox: **files** survive the 8 h wall and `kill()`; kernel
variables, processes and PTYs do not.

It is done by `rayd`, the VM's agent, **as root and with the execution
role** read through IMDSv2: the sandbox code (uid 1000) still cannot reach
IMDS on the `rayito-base-caps` image, and nothing goes through your
machine.

!!! tip "Pause or persist?"
    For waits of less than 8 h from `create()`,
    [pause the sandbox](guias/pausar-reanudar.md): it also keeps memory,
    processes and the kernel. Persistence is for what goes beyond: a
    sandbox that has to survive `kill()` or the 8 h cap, like a
    conversation with an [agent](guias/agente-en-el-sandbox.md) that can sit
    idle for more than 8 h (point 2 of
    [Which one do I use?](guias/agente-en-el-sandbox.md#que-uso)): its
    `HOME`, with the agent's sessions, comes back with the same `persist=`.

<a id="quickstart"></a>

## Quick example { #ejemplo-rapido }

You need the `rayito-base-caps` image, an execution role with access to the
bucket (`infra/iam.yaml` with `PersistenceBucket`, see [IAM and bucket](#iam-y-bucket))
and its ARN in `RAYITO_EXECUTION_ROLE_ARN`:

=== "Python"

    ```python
    import os

    from rayito import S3Prefix, Sandbox

    role = os.environ["RAYITO_EXECUTION_ROLE_ARN"]
    home = S3Prefix("amzn-s3-demo-bucket", prefix="rayito-home", name="agent-7")

    with Sandbox.create("rayito-base-caps", execution_role_arn=role, persist=home) as sbx:
        print(sbx.last_restore)  # None the first time; afterwards, what was restored
        sbx.files.write("/home/user/notes.txt", "hello")
        sbx.checkpoint_files()  # (1)!

    with Sandbox.create("rayito-base-caps", execution_role_arn=role, persist=home) as again:
        print(again.files.read("/home/user/notes.txt"))  # "hello"
        nxt = again.reincarnate()  # (2)!
        nxt.kill()
    ```

    1. `s3://amzn-s3-demo-bucket/rayito-home/agent-7/home.tar.gz`
    2. A fresh 8 h with the same `HOME`; the old sandbox is killed.

=== "Python (async)"

    ```python
    import asyncio
    import os

    from rayito import AsyncSandbox, S3Prefix


    async def main() -> None:
        role = os.environ["RAYITO_EXECUTION_ROLE_ARN"]
        home = S3Prefix("amzn-s3-demo-bucket", prefix="rayito-home", name="agent-7")
        async with await AsyncSandbox.create("rayito-base-caps", execution_role_arn=role, persist=home) as sbx:
            await sbx.files.write("/home/user/notes.txt", "hello")
            await sbx.checkpoint_files()
        async with await AsyncSandbox.create("rayito-base-caps", execution_role_arn=role, persist=home) as again:
            print(await again.files.read("/home/user/notes.txt"))  # "hello"


    asyncio.run(main())
    ```

=== "TypeScript"

    ```ts
    import { S3Prefix, Sandbox } from "rayito";

    const executionRoleArn = process.env.RAYITO_EXECUTION_ROLE_ARN ?? "";
    const persist = new S3Prefix({ bucket: "amzn-s3-demo-bucket", prefix: "rayito-home", name: "agent-7" });

    {
      await using sbx = await Sandbox.create({ template: "rayito-base-caps", executionRoleArn, persist });
      console.log(sbx.lastRestore); // undefined the first time
      await sbx.files.write("/home/user/notes.txt", "hello");
      await sbx.checkpointFiles();
    }
    await using again = await Sandbox.create({ template: "rayito-base-caps", executionRoleArn, persist });
    console.log(await again.files.read("/home/user/notes.txt")); // "hello"
    const next = await again.reincarnate(); // a fresh 8 h with the same HOME
    await next.kill();
    ```

## `S3Prefix` { #s3prefix }

`S3Prefix(bucket, prefix="rayito", name=None, region=None)` names where a
persisted `HOME` lives: `s3://bucket/prefix/name/`.

- `prefix` is the operator's base, the one that scopes the execution role's
  IAM policy: it has to match the deployment's `PersistencePrefix`
  (`infra/iam.yaml`, `rayito-home` by default) or every
  `checkpoint_files()` answers
  `PersistenceException(code="permission_denied")`. **The SDK's default
  `prefix="rayito"` is not suitable for a real deployment**: `rayito/` is
  the namespace of the image artifacts (`rayito/images/*`) and the `*` of
  an IAM policy crosses `/`, so a prefix whose first segment is `rayito`
  puts the persisted `HOME` and the zips images are built from under the
  same permission. Always pass a `prefix=` whose first segment is not
  `rayito` (the template also adds an explicit `Deny` on
  `<ArtifactBucket>/rayito/*`, but it only covers that bucket).
- `name` identifies a specific home. `Sandbox.create(persist=)` sets it to
  the `sandbox_id` if empty; for two lives to share the same home, pass it
  yourself. `connect(persist=)` and `restore_files(source=)` require it.
  `sbx.persist` returns the bound `S3Prefix` (with its `name`) and
  `S3Prefix.uri` its full `s3://`.
- `region` only if the bucket is not in the sandbox's region (by default
  the VM's `AWS_REGION`).
- It is validated on the client with the same rules `rayd` applies: bucket
  of 3 to 63 characters `[a-z0-9.-]`, prefix of up to 900 bytes with no
  leading or trailing `/`, no `//`, `.` or `..`, only S3's safe set
  (`A-Za-z0-9!_.*'()-/`).

**The prefix separates tenants when the sandbox binds it.** With
`create(persist=)`, the SDK sends in the `runHookPayload` the bucket and the
`prefix` (the base, not the `name`: the default `name` is the `sandbox_id`,
which does not exist yet at launch) and `rayd` rejects with
`PersistenceException(code="permission_denied")` any `Checkpoint` or
`Restore` outside `<bucket>/<prefix>`, before touching S3, even if the
execution role reaches the whole bucket. For tenants that do not trust each
other a `prefix` per tenant under the deployment's `PersistencePrefix` is
enough (`prefix="rayito-home/acme"`); a `name` per tenant does not separate
them. Within its base, a sandbox can read and write any `name`: a
`restore_files(source=)` from another `name` of the same base works, and
from another base it answers `permission_denied`.

Limits of the binding:

- It is set by `create` and does not change for the whole life of the
  sandbox: `connect(persist=)` only changes the client's default
  destination.
- A sandbox created **without** `persist=`, or with an SDK or a `rayd`
  earlier than 0.7.0, binds nothing: `rayd` accepts any valid destination
  and the only limit is how far its execution role reaches. Do not give a
  sandbox without `persist=` a role that reaches the persistence prefix, or
  use a role per tenant.
- The manifest's `sandbox_id` remains informational.

Under the prefix there are two objects: `home.tar.gz` (POSIX/GNU tar
compressed with gzip level 1) and `manifest.json` (the archive's sha256,
sizes, counts, `sandbox_id`, agent version, exclusion list). The manifest is
written **after** the archive: if it exists, the archive is complete.

## `checkpoint_files` and `restore_files` { #checkpoint_files-y-restore_files }

| Python | TypeScript | Default | What it does |
|---|---|---|---|
| `checkpoint_files(target=)` | `checkpointFiles({ target })` | `sbx.persist` | where to write the archive |
| `exclude=` | `exclude` | `()` | paths relative to `HOME` that are not archived (below) |
| `restore_files(source=)` | `restoreFiles({ source })` | `sbx.persist` | where to restore from |
| `timeout=` | `timeoutMs` | 600 s / 600,000 ms | deadline of the operation (`TimeoutException`) |
| `on_progress=` | `onProgress` | — | receives `CheckpointProgress` (`files_done`, `bytes_read`, `bytes_uploaded`) or `RestoreProgress` (`files_done`, `bytes_downloaded`) |
| `user=` | `user` | the sandbox user | whose `HOME` it is; `"root"` is `permission_denied` |

They return a `CheckpointResult` / `RestoreResult` with `bucket`,
`key_prefix`, `uri`, `files`, `archive_bytes`, `sha256`, `skipped` and
`duration` in seconds (TypeScript: `keyPrefix`, `archiveBytes`,
`durationMs`…), plus `bytes_read` or `bytes_written`. They also work on a
`connect(persist=)` handle or with explicit `target=`/`source=`, without
`create(persist=)`.

=== "Python"

    ```python
    import os

    from rayito import S3Prefix, Sandbox

    role = os.environ["RAYITO_EXECUTION_ROLE_ARN"]
    home = S3Prefix("amzn-s3-demo-bucket", prefix="rayito-home", name="agent-7")

    with Sandbox.create("rayito-base-caps", execution_role_arn=role, persist=home) as sbx:
        result = sbx.checkpoint_files(
            exclude=["data/raw"],
            on_progress=lambda p: print(p.files_done, p.bytes_uploaded),
        )
        print(result.uri, result.files, result.archive_bytes)
        sbx.restore_files()  # (1)!
    ```

    1. Extracts the last checkpoint again over the `HOME`.

=== "Python (async)"

    ```python
    import asyncio
    import os

    from rayito import AsyncSandbox, S3Prefix


    async def main() -> None:
        role = os.environ["RAYITO_EXECUTION_ROLE_ARN"]
        home = S3Prefix("amzn-s3-demo-bucket", prefix="rayito-home", name="agent-7")
        async with await AsyncSandbox.create("rayito-base-caps", execution_role_arn=role, persist=home) as sbx:
            result = await sbx.checkpoint_files(exclude=["data/raw"])
            print(result.uri, result.files)
            restored = await sbx.restore_files()
            print(restored.bytes_written)


    asyncio.run(main())
    ```

=== "TypeScript"

    ```ts
    import { S3Prefix, Sandbox } from "rayito";

    const executionRoleArn = process.env.RAYITO_EXECUTION_ROLE_ARN ?? "";
    const persist = new S3Prefix({ bucket: "amzn-s3-demo-bucket", prefix: "rayito-home", name: "agent-7" });

    await using sbx = await Sandbox.create({ template: "rayito-base-caps", executionRoleArn, persist });
    const result = await sbx.checkpointFiles({
      exclude: ["data/raw"],
      onProgress: (p) => console.log(p.filesDone, p.bytesUploaded),
    });
    console.log(result.uri, result.files, result.archiveBytes);
    await sbx.restoreFiles();
    ```

## What is archived and what is not { #que-se-archiva-y-que-no }

`checkpoint_files()` packs the `HOME` of the user making the request (never
`/root`, whatever `RAYITO_ALLOW_ROOT` says):

- regular files (mode `0o7777`, mtime), directories and symlinks with their
  target **as is** (they are never resolved or followed; a linked directory
  is not descended into);
- **fixed exclusion list**, always: the components `.cache`, `__pycache__`,
  `.ipynb_checkpoints` and `.rayito-tmp-*` at any depth, and
  `.local/share/jupyter/runtime`,
  `.ipython/profile_default/history.sqlite` (and its `-journal`);
- `exclude`: up to 64 paths relative to `HOME`, no globs, by whole
  components (`data/raw` excludes `data/raw/…` but not `data/raw2`);
- skipped and counted in `skipped`: sockets, FIFOs, devices, what the user
  cannot read and what is on another filesystem (a tmpfs mounted inside
  the `HOME`). Hard links travel as independent files.

The read runs under the user's identity, so a file the user cannot read is
not read by `rayd` as root. A file that changes while being archived comes
out truncated or padded with zeros up to the `lstat` size: **pause your
work before a checkpoint** if you need a consistent copy. Only one
checkpoint or restore at a time per sandbox
(`PersistenceException(code="failed_precondition")`).

## What a restore does { #que-hace-un-restore }

`restore_files()` downloads the manifest (`NotFoundException` if there is
none) and extracts the archive over the `HOME`:

- existing files are overwritten, directories are merged and a symlink
  occupying the path is deleted first (never followed);
- only regular files, directories and symlinks get in; hard links, devices,
  FIFOs and sockets are skipped; an entry with `..`, absolute, or whose
  parent leaves the `HOME` through a symlink aborts the restore
  (`invalid_argument`);
- the setuid/setgid/sticky bits are dropped (`0o777`) and everything
  created belongs to the user;
- the archive's sha256 is checked at the end (`internal`, `el checksum del
  archivo no coincide`, that is, "the archive checksum does not match").

A restore that fails halfway leaves the `HOME` **partially restored**;
recovery is `kill()` + `create(persist=)`. `create(persist=)` with a given
`name` restores automatically (`sbx.last_restore`) and, if the restore
fails for something other than "there is no checkpoint", closes and
terminates the sandbox as a readiness failure (except with
`keep_on_failure`).

## `reincarnate()`: beyond `max_lifetime` { #reincarnate-mas-alla-de-max_lifetime }

`sbx.reincarnate(exclude=(), persist_timeout=600)` (TypeScript:
`sbx.reincarnate({ exclude, persistTimeoutMs })`) does, in this order:
`checkpoint_files()` → `Sandbox.create(**same launch options,
persist=sbx.persist)` (which restores) → `kill()` of the old sandbox, and
returns the new one. The new one has a fresh 8 h, another `sandbox_id`,
another access token (unless the original was explicit) and the same
`metadata`.

The options that end up in `ConfigureSandbox` are also reapplied:
`mounts=`, `volumes=`, `events=`, `telemetry=` and `gateways=` go back into
the successor's single `Configure` by the same path as in `create()`,
resolved with the new sandbox's facts:

| Option | What the successor does |
|---|---|
| `mounts=` | mounts each bucket again and `reincarnate()` does not return until all are `mounted` |
| `volumes=` (experimental) | sends the section again and resolves the EFS mount targets' IPs again |
| `events=` | derives a new `k_sbx` key from the new `sandbox_id` (it never reuses the original's) |
| `telemetry=` | resolves the ARN and the image version and the guest's memory again |
| `gateways=` | rereads each header from Secrets Manager; it never forwards a value already read |

`size=` is not repeated: the size is already in the ARN of the image being
relaunched. If any section fails in the successor, it is terminated (except
with `keep_on_failure`) and the old sandbox stays alive, like any other
`create()` failure.

If `create()` fails, the old sandbox stays alive and the exception carries
a note with the `uri` of the already-completed checkpoint. Only on a
`create(persist=)` sandbox: a `connect()` handle does not know the launch.
`set_timeout()` (native and in `rayito.e2b`) moves the logical deadline
that `rayd` enforces, but never beyond `max_lifetime` (the platform cap,
≤ 8 h, fixed after `create()`): past that cap, `reincarnate()` remains the
only path, with the files and without the memory.

## IAM and bucket { #iam-y-bucket }

`infra/iam.yaml` accepts `PersistenceBucket` and `PersistencePrefix`; with
the bucket defined, the execution role receives exactly:

```yaml
- Effect: Allow
  Action: [s3:PutObject, s3:GetObject, s3:AbortMultipartUpload]
  Resource: arn:aws:s3:::<bucket>/<prefix>/*
- Effect: Allow
  Action: s3:ListBucket
  Resource: arn:aws:s3:::<bucket>
  Condition: { StringLike: { s3:prefix: "<prefix>/*" } }
```

No `s3:DeleteObject` (the role never deletes) and no
`s3:ListMultipartUploadParts`. `s3:ListBucket` exists only so that a
missing key answers `404 NoSuchKey` instead of `403`, which
`NotFoundException` depends on.

Operating tips: the bucket in the **same region** as the image (or pass
`region=`); SSE-S3 by default is enough (SSE-KMS requires adding
`kms:Decrypt` and `kms:GenerateDataKey` to the role: it is not in the
template); add an `AbortIncompleteMultipartUpload` lifecycle rule at 1 day
on the prefix (a VM killed mid-checkpoint can leave orphaned parts); with
an egress connector of your own (`infra/egress-connector.yaml`) S3 needs a
gateway endpoint or a NAT. `rayd` sends no encryption parameters, storage
class or ACL: the bucket's defaults apply (`AWS_API_NOTES.md` §17 lists
each operation and parameter).

## Errors { #errores }

| Situation | Python | TypeScript |
|---|---|---|
| `persist=` without `execution_role_arn`, invalid `exclude`, `S3Prefix` without `name` | `InvalidArgumentException` (before touching AWS) | `InvalidArgumentError` |
| no execution role, `AccessDenied`, expired credentials, `user="root"` | `PersistenceException(code="permission_denied")` | `PersistenceError` `permission_denied` |
| no checkpoint under the prefix (explicit `restore_files`) | `NotFoundException` | `NotFoundError` |
| another operation in progress, unknown region | `PersistenceException(code="failed_precondition")` | `failed_precondition` |
| network, S3 5xx, tar/gzip, checksum, disk full | `PersistenceException(code="internal")` | `internal` |
| stream cut midway (`/suspend`, proxy) | `PersistenceException(code="interrupted")`: repeat the operation, the SDK does not resume it | `interrupted` |
| image with a `rayd` earlier than 0.2.0 | `PersistenceException(code="unimplemented")` with the instruction to republish | `unimplemented` |
| deadline (`timeout`, 600 s by default) | `TimeoutException` | `TimeoutError` |

## Cost and measured numbers { #coste-y-numeros-medidos }

A checkpoint moves the `HOME`'s `tar.gz` from the VM to S3 (data transfer
through the `INTERNET_EGRESS` connector + `PutObject` in 8 MiB parts) and a
restore brings it back; storage and requests are S3's
([Amazon S3 pricing](https://aws.amazon.com/s3/pricing/)). Persistence is
only activated with `persist=` or with an explicit call to
`checkpoint_files()`/`restore_files()`: without them there is no call to
S3. The `rayd` binary grows because of the TLS client (`rustls` +
`aws-lc-rs`) and the S3 SDK: **4,700,984 → 12,524,384 B** (+7.8 MB,
×2.66); the clean ARM64 build goes from 106 s to 222 s on the development
machine.

Measured on 2026-09-17 against real AWS (`tests/e2e/test_m7_persistence.py`,
`rayito-base-caps` 7.0, bucket in the same region, 50 MB of random data in
21 entries; `AWS_API_NOTES.md` Q53/Q54):

| Operation | Agent | Wall (client) | Throughput |
|---|---|---|---|
| first `checkpoint_files()` (TLS/S3 code not yet paged in) | 1.50 s | 1.67 s | 31.4 MB/s |
| second `checkpoint_files()` | 1.40 s | 1.50 s | 35.0 MB/s |
| `restore_files()` inside `create(persist=)` | 0.67 s | 7.35 s with the `create` | 78.0 MB/s |
| full `reincarnate()` | — | 8.85 s | — |
| `restore_files(source=)` without a checkpoint → `NotFoundException` | — | 0.11 s | — |
| `checkpoint_files()` without an execution role → `permission_denied` | — | 1.35 s | — |

The 50 MB random archive weighs 52,479,326 B (gzip level 1 does not
compress noise; a `HOME` of text and notebooks does shrink). `imds_blocked`
stayed `true` on the caps image throughout the cycle: persistence does not
open IMDS to the sandbox code. It was not measured at 2 GB.

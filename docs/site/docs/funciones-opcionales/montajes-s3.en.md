# S3 mounts

Mounts one or more S3 buckets (or a prefix of one) as a normal folder
inside the sandbox, with [Mountpoint for Amazon S3](https://github.com/awslabs/mountpoint-s3)
(`mount-s3`). Only on `rayito-base-caps` (or a size-derived variant):
`mount(2)` needs `CAP_SYS_ADMIN`, which only that variant gives the agent,
and the daemon needs the execution role's credentials through IMDS. On any
other image, `create()` terminates the VM and raises
`UnimplementedError`.

!!! info "Cost and activation"
    - **Default**: off. Without `mounts=` (TypeScript: `mounts`) `rayd`
      does not open `/dev/fuse` or launch anything.
    - **Turns on**: `mounts={"/mnt/data": S3Mount(bucket="...", prefix="...")}`
      in `Sandbox.create()`. It needs three things: a published
      `rayito-base-caps` image with the bucket in its allowlist
      (`RAYITO_ALLOWED_MOUNT_BUCKETS`, see below), `execution_role_arn=` and
      the `s3-mounts` stack's IAM policy on that role.
    - **Resources and AWS calls**: nothing new by itself. `mount-s3`
      makes the normal S3 calls (`GetObject`/`ListObjectsV2`, and
      `PutObject`/`DeleteObject` with write access) against the bucket you
      mount, under the execution role's credentials — the same ones you
      would already pay for by calling S3 yourself.
    - **Approximate cost** (us-east-1, checked 2026-10-01,
      [S3 pricing](https://aws.amazon.com/s3/pricing/)): $0 of its own from
      Rayito; you pay the normal S3 requests and storage of the bucket you
      mount.
    - **IAM**: the managed policy `RayitoS3MountAccess`
      (`infra/s3-mounts.yaml`, `OptionalStack` component `s3-mounts`) on
      the sandbox's execution role, scoped to one bucket per stack.
    - **How to turn it off**: do not pass `mounts=`; `rayito stack destroy
      s3-mounts` removes the policy (it deletes no object or the bucket).

!!! note "Measured on real AWS"
    `Sandbox.create(mounts=)`/`create({ mounts })` and `sbx.mounts` passed
    acceptance on real AWS (S3M-1..S3M-4, `AWS_API_NOTES.md`
    Q100–Q104): read and write, `pause()`/`resume()` with the mount
    live, `allow_internet_access=False`, and the VM terminated on a mount
    failure. `mount-s3` 1.24.0 adds 72.7 MB to the image.

## When to use it { #cuando-usarlo }

- Your code already knows how to work with normal paths (`pandas.read_csv`,
  `ls`, `grep`, a library that only accepts a path) and the data lives in
  S3: without a mount, you would have to download it all first.
- You want an agent's output (files it writes) to show up directly in S3,
  without an extra upload step.
- **When not to**: you need full POSIX semantics (locks, writable `mmap`,
  very many very small files with a lot of `fsync`) — S3 is not a
  filesystem, and `mount-s3` has the same limit as any FUSE over an object
  store. For that, copy the file into the sandbox's `HOME` with
  `files.write`/`sandbox.git` instead of mounting it.

## Mount paths { #rutas-de-montaje }

The key of `mounts=`/`mounts` is the absolute path where the bucket appears
inside the guest: under `/mnt/` or `/home/user/`, not overlapping another
mount (the same rule `volumes=` uses for
[EFS volumes](volumenes-efs.md)), and at most 4 mounts in total per
sandbox, adding up `mounts=` and `volumes=`.

## Before you start: the image allowlist (mandatory) { #antes-de-empezar-el-allowlist-de-la-imagen-obligatorio }

`rayd` only mounts the buckets the image itself declares in
`RAYITO_ALLOWED_MOUNT_BUCKETS` (comma-separated). It is image
configuration, not a per-sandbox option: that way the code calling
`create()` cannot mount a bucket the person who publishes the image did not
authorize. **Empty or absent denies every bucket**: `create()` terminates
the VM and raises `MountException(code="not_allowed")` (`MountError` with
`code: "not_allowed"` in TypeScript), whose message says the bucket is not
in the image's allowlist and how to publish it with the bucket (it never
names the bucket).

```bash
rayito image publish --artifact rayito-image.zip --base-image-version 1 \
  --image-name rayito-base-caps --os-capabilities ALL \
  --env RAYITO_ALLOWED_MOUNT_BUCKETS=my-bucket,other-bucket
```

From the repository, `make image-publish-caps MOUNT_BUCKETS=my-bucket,other-bucket`
(or `image-publish-caps-efs`) adds that `--env` for you.

If you build the image with your own `Dockerfile` on top of
`rayito-base-caps`, `ENV RAYITO_ALLOWED_MOUNT_BUCKETS=...` does the same.
Changing the allowlist means publishing a new version of the image.

## Deploy the IAM policy { #desplegar-la-politica-iam }

```bash
rayito stack deploy s3-mounts --param BucketName=my-bucket \
  --param Prefixes='team7/*,runs/*' --param ReadOnly=false
```

The `RayitoS3MountAccess` policy covers a single bucket (one stack per
bucket) and, within it, only the `Prefixes` prefixes (up to 4, ending in
`*`; `*` by default is the whole bucket), also for reading, writing and
deleting objects: a sandbox that should only write `runs/42/` cannot touch
objects under another prefix. `rayito stack status s3-mounts` shows the ARN
of the policy already created; add it to the sandbox's execution role.
`rayito stack destroy s3-mounts` removes it (it never deletes the bucket or
its objects).

## Example { #ejemplo }

`create()` does not return until every mount is mounted: if one fails (or
still does not answer after 15 s), it terminates the VM and raises
`MountException` with the reason in `code` (`iam_denied`, `not_found`,
`not_allowed`, `invalid_path`, `helper_missing`, `network` or `timeout`;
`unknown` if the agent sends a class your SDK version does not know yet).
As soon as `create()` returns, the folder can be read.

=== "Python"

    ```python
    from rayito import S3Mount, Sandbox

    sbx = Sandbox.create(
        "rayito-base-caps",
        execution_role_arn="arn:aws:iam::<account>:role/my-execution-role",
        mounts={
            "/mnt/data": S3Mount(bucket="my-bucket", prefix="team7/"),  # (1)!
            "/mnt/out": S3Mount(
                bucket="my-bucket", prefix="runs/42/", read_only=False, allow_overwrite=True
            ),
        },
    )
    sbx.commands.run("python -c \"import pandas; pandas.read_csv('/mnt/data/data.csv')\"")
    print(sbx.mounts)  # {"/mnt/data": MountStatus(state="mounted"), ...}
    sbx.kill()
    ```

    1. `read_only=True` by default: read-only unless you explicitly ask
       otherwise.

=== "TypeScript"

    ```typescript
    import { Sandbox, S3Mount } from "rayito";

    const sbx = await Sandbox.create({
      template: "rayito-base-caps",
      executionRoleArn: "arn:aws:iam::<account>:role/my-execution-role",
      mounts: {
        "/mnt/data": new S3Mount({ bucket: "my-bucket", prefix: "team7/" }),
        "/mnt/out": new S3Mount({
          bucket: "my-bucket",
          prefix: "runs/42/",
          readOnly: false,
          allowOverwrite: true,
        }),
      },
    });
    await sbx.commands.run("python3 -c \"import pandas; pandas.read_csv('/mnt/data/data.csv')\"");
    console.log(await sbx.mounts()); // Map { "/mnt/data" => { state: "mounted" }, ... }
    await sbx.kill();
    ```

`mounts` also accepts a `Map` in TypeScript.

`sbx.mounts` (a property on `Sandbox`; `await sbx.mounts()` on
`AsyncSandbox` and in TypeScript) reads the live state on every call: if a
mount's daemon dies later, `rayd` relaunches it on its own and, in the
meantime, that mount shows up as `"pending"` or `"failed"`.

## Differences from E2B { #divergencias-con-e2b }

E2B has no equivalent to `mounts=` (row 111 of
[E2B parity](../e2b-parity.md)): the shim adds no new method for this.

## Security { #seguridad }

- The `mount-s3` daemon runs as the dedicated user `rayito-mount` (uid
  990), never as uid 1000 or as root after starting; its credentials
  arrive through its own IMDS access, never through argv or through the
  environment of the process `rayd` launches (`AWS_REGION`/`PATH` are the
  only variables).
- The bucket and prefix **are not secrets** (like the sandbox's
  `metadata`): a uid 1000 process can see the bucket name and the prefix on
  the daemon's command line (`/proc/<pid>/cmdline`), never a credential;
  its environment (`/proc/<pid>/environ`) is not even readable, and it
  cannot send it signals either.
- The bucket must be in the image's `RAYITO_ALLOWED_MOUNT_BUCKETS` (image
  config, not a per-sandbox option); empty or absent denies every bucket.
- The availability probe `rayd` launches as uid 1000 after each mount
  (`/usr/bin/stat <path>`) and `mount-s3` itself start with an environment
  built from scratch (only `PATH`, plus `AWS_REGION` for `mount-s3`),
  inheriting no descriptor from `rayd` and none of its ignored signals:
  `rayd`'s environment (the image ARN, `RAYITO_*` variables or the image's
  `--env`) never reaches a process another uid 1000 process can read in
  `/proc/<pid>/environ`.
- `rayd` mounts as root, but it never follows a symbolic link in the mount
  path (neither when creating it nor when relaunching a mount): if the
  sandbox code replaces `/home/user/<folder>` with a link to a system
  directory, the mount fails with `invalid_path` instead of mounting the
  bucket on top of that directory.
- The IAM policy is scoped to the prefixes you declare, also for writing
  and deleting: containment does not depend only on `mount-s3 --prefix`
  inside a guest that runs untrusted code.

Full detail: [Security](../security.md), threat T20.

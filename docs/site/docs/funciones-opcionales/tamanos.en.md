# Sizes

A closed catalog of five CPU/RAM sizes (512mb/1gb/2gb/4gb/8gb) that the
SDK resolves **client-side, with no RPC at all**: `size=` decides which
image to launch (`rayito-base-4gb`), never a tweak to a running guest.

!!! info "Cost and activation"
    - **Default**: off. Without `size=`/`size` the SDK makes no new AWS
      call: the behavior is exactly that of 0.5.x.
    - **Turns on**: `Sandbox.create(size="4gb")` / `Sandbox.create({ size: "4gb" })`
      (or `SizeRequest(memory_mib=...)`/`{ memoryMib: ... }`); `rayito image
      publish --sizes 512mb,4gb` to publish the additional images;
      `rayito stack deploy sizes-guard` for the optional IAM guardrail.
    - **Resources and AWS calls**: nothing new to launch a sandbox with
      `size=` (the size is already in the image name). `get_info()`/
      `getInfo()` makes, at most once per image version and per
      process (cached), a free call to `GetMicrovmImageVersion`
      to confirm `minimumMemoryInMiB`. `rayito image publish --sizes`
      makes the same `create`/`update-microvm-image` calls as a normal
      publish, one per additional size. `rayito image sizes` adds, only if
      some additional size is published, one `GetMicrovmImageVersion`
      (also free) per published image for `sameArtifact`; it never
      launches any sandbox. `sizes-guard` creates a single
      `AWS::IAM::ManagedPolicy`.
    - **Approximate cost** (us-east-1): $0 for `size=` itself: there is no
      surcharge for the catalog, but a bigger MicroVM costs more per
      hour, from ≈ $0.0315/h (512 MiB) to ≈ $0.5044/h (8192 MiB) at baseline
      ([Limits: size](../limits.md#tamano-cpuram)). Each published size
      is one more image version, with its one-week minimum of snapshot
      storage (≈ $0.04/week at 2 GB, see
      [Pricing](../cost.md#componentes-del-precio)); the snapshot grows with
      the size (from ≈450 MB at 512 MiB up to ≈1.1 GB at 8192 MiB on the
      same minimal image). `sizes-guard`: $0 at rest and per use (IAM
      only).
    - **IAM**: nothing additional to launch with `size=`. `sizes-guard`
      (`RayitoRunAllowedSizes`) adds a Deny of `lambda:RunMicrovm` outside
      the image ARNs the operator lists (not just an Allow: an Allow, by
      itself, restricts nothing if the identity already has the
      `microvm-image:*` of the standard `CallerPolicy` in `infra/iam.yaml`,
      which is exactly the typical case); attach it to the identity that
      creates sandboxes to keep it from launching an unpublished size,
      whatever the rest of its permissions.
    - **How to turn it off**: do not pass `size=`/`size` (default `None`/
      `undefined`); `rayito image prune --image-name rayito-base-4gb
      --keep 1` deletes the old versions of each sized image (each
      one bills its snapshot; `prune` keeps at least the newest), and
      delete the `sizes-guard` stack if you deployed it (the stack deletes
      no image).
    - **Example**:

      === "Python"

          ```python
          from rayito import Sandbox

          with Sandbox.create(size="4gb") as sbx:
              sbx.commands.run("echo hello")
          ```

      === "TypeScript"

          ```ts
          import { Sandbox } from "rayito";

          await using sbx = await Sandbox.create({ size: "4gb" });
          await sbx.commands.run("echo hello");
          ```

!!! success "Accepted on real AWS (2026-10-02)"
    `size=`/`size` (Python sync/async and TypeScript), `rayito image publish
    --sizes`/`--env`, `rayito image sizes` and the `sizes-guard` component
    were tested against real AWS: see `AWS_API_NOTES.md` §24 (Q118, Q119).

## When to use it { #cuando-usarlo }

- A one-off workload needs more RAM/CPU than the baseline (2048 MiB; the
  guest sees 4 vCPUs): `size="4gb"` avoids publishing and maintaining a
  separate image by hand.
- You want `Sandbox.create(size="512mb")` to stay cheap for small
  workloads without losing the existing baseline.
- **When not to**: if you already publish images with your own per-size
  names, `size=` is optional sugar, not a mandatory migration; keeping on
  passing the full name (`Sandbox.create("my-image-4gb")`) works
  the same.

## Quick example { #ejemplo-rapido }

Publish the baseline and the sizes you need, from the same artifact:

```bash
rayito image publish --artifact rayito-image.zip --base-image-version 1 \
  --bucket my-bucket --sizes 512mb,4gb
```

=== "Python"

    ```python
    from rayito import Sandbox

    with Sandbox.create(size="4gb") as sbx:  # (1)!
        info = sbx.get_info()
        print(info.size, info.baseline_memory_mib, info.baseline_cpu)  # (2)!
        sbx.commands.run("python train.py")
    ```

    1. Resolves `rayito-base` + `4gb` -> `rayito-base-4gb` client-side; zero
       new AWS calls in `create()`.
    2. `"4gb"`, `4096`, `8`: a single cached `GetMicrovmImageVersion`.
       `info.memory_mb` is still what the guest actually reports
       (up to 4x `baseline_memory_mib`).

=== "Python (async)"

    ```python
    import asyncio

    from rayito import AsyncSandbox


    async def main() -> None:
        async with await AsyncSandbox.create(size="4gb") as sbx:
            info = await sbx.get_info()
            print(info.size, info.baseline_memory_mib, info.baseline_cpu)


    asyncio.run(main())
    ```

=== "TypeScript"

    ```ts
    import { Sandbox } from "rayito";

    await using sbx = await Sandbox.create({ size: "4gb" });
    const info = await sbx.getInfo();
    console.log(info.size, info.baselineMemoryMib, info.baselineCpu);
    await sbx.commands.run("python train.py");
    ```

A size that does not fit exactly is always rounded up, never
down, and warns:

=== "Python"

    ```python
    from rayito import Sandbox, SizeRequest

    # RayitoCompatWarning: rounds to 4096 MiB (4gb)
    with Sandbox.create(size=SizeRequest(memory_mib=3000)) as sbx:
        sbx.commands.run("echo hello")
    ```

=== "TypeScript"

    ```ts
    import { Sandbox } from "rayito";

    // warns with process.emitWarning(..., { type: "RayitoCompatWarning" })
    await using sbx = await Sandbox.create({ size: { memoryMib: 3000 } });
    await sbx.commands.run("echo hello");
    ```

## How it works { #como-funciona }

1. `resolve_size`/`resolveSize` (pure domain) rounds the request up to
   the first value of the closed catalog that covers it
   (`SUPPORTED_MEMORY_MIB`: 512/1024/2048/4096/8192 MiB); above
   8192 MiB it is `InvalidArgumentException`/`InvalidArgumentError` **before
   any call to AWS**.
2. `apply_size_suffix`/`applySizeSuffix` prepends the suffix to the
   template name (`"rayito-base"` + `4gb` -> `"rayito-base-4gb"`); the baseline
   (2048 MiB) never carries a suffix. A template given by **ARN** with `size=`
   is `InvalidArgumentException`/`InvalidArgumentError`: the image is
   named by its ARN, not by the `<variant>[-<size>]` convention.
3. `create()` resolves the ARN of the already-suffixed image and launches
   `run-microvm` exactly as without `size=`.
4. `get_info()`/`getInfo()`, only if `size=` was used, confirms
   `minimumMemoryInMiB` with `GetMicrovmImageVersion` (cached per image
   version) and fills in `size`/`baseline_memory_mib`/`baseline_cpu`.
   `cpu_count`/`memory_mb` (`cpuCount`/`memoryMb`) are still what the
   guest actually reports through `Health`: they can see up to 4x
   `baseline_memory_mib` (measured for the five catalog sizes).
5. `create(pool=, size=)`/`create({ pool, size })` is
   `InvalidArgumentException`/`InvalidArgumentError`: a pool slot already
   came out of a fixed image, it cannot change size when taken.
6. `size`/`baseline_memory_mib`/`baseline_cpu` only show up on the handle
   that actually called `create(size=...)`: `Sandbox.connect(id)` never
   fills them in, even if the image has a size's suffix (it is not
   guessed from the name on purpose, so as not to confirm a size
   nobody asked for in *this* process).

## Guest vs. image (what was measured) { #guest-vs-imagen-lo-medido }

| `minimumMemoryInMiB` | Guest vCPUs | Guest `MemTotal` | Root disk |
|---|---|---|---|
| 512 | 1 | ≈1,989 MiB | 8.3 GB |
| 1024 | 2 | ≈3,998 MiB | 8.3 GB |
| 2048 (baseline) | 4 | ≈8,016 MiB | 8.3 GB |
| 4096 | 8 | ≈16,052 MiB | 16.7 GB |
| 8192 | 16 | ≈32,123 MiB | 33.6 GB |

The guest always sees more than `minimumMemoryInMiB` (up to ≈4x): that is the
billed peak, not the baseline. `baseline_memory_mib`/`baselineMemoryMib` is
the image's declared value (what you are billed for); `memory_mb`/`memoryMb`
is still what the guest reports.

## Publish images by size { #publicar-imagenes-por-tamano }

```bash
rayito image publish --artifact rayito-image.zip --base-image-version 1 \
  --bucket my-bucket --sizes 512mb,4gb \
  --env RAYITO_ALLOWED_MOUNT_BUCKETS=my-bucket
```

- Without `--sizes`, only the baseline is published, exactly as before
  this feature.
- Each additional size is published as an image named
  `<image-name>-<size>` from the **same artifact**; `--env KEY=VALUE`
  (repeatable) adds image variables (`environmentVariables`) to all
  the ones published in the invocation, baseline included — never an SDK
  activation switch (ADR-014 rule 4), only configuration baked into the
  image. `rayito image publish` also bakes
  `RAYITO_BASELINE_MEMORY_MIB` into each suffixed image. Neither
  reaches the `commands.run` environment (measured, Q119): they are
  declared information about the image, which you read with `GetMicrovmImageVersion`.
  **Never put secrets in `--env`**: anyone with `GetMicrovmImageVersion`
  and every process in the guest reads them in the clear; use `SecretStore`/`secrets=`
  for that.
- With `--sizes`, `--memory-mib` only accepts the default value (2048 MiB):
  the unsuffixed image of `--sizes` is always the baseline.
- Builds of more than one size are all requested before waiting for
  any to settle (a wave of up to 10 simultaneous
  builds, the service limit), so AWS builds them in parallel.
- A version already built with the same artifact and configuration is
  reused, as in a normal publish. With `--env` or `--sizes` the variable
  comparison uses one (free) `GetMicrovmImageVersion` per
  candidate version, because `list-microvm-image-versions` does not return them
  (Q118); with neither, the check is that of 0.5.x, with no new
  calls, and a version published earlier *with* `--env` is
  reused as is: use `--force` to rebuild it without variables.
- `rayito image sizes [--variant | --image-name]` lists, per size of the closed catalog,
  which image of the variant has already been published (or none) and whether it shares
  an artifact with the baseline (`sameArtifact`): always one
  `list-microvm-images`, and if some additional size is published, also
  one `GetMicrovmImageVersion` (no quota of its own) per image already published —
  with only the baseline published, no additional call. It never launches
  any sandbox. See [CLI](../cli.md#image-sizes).

## Cost guardrails (`sizes-guard`, optional) { #guardarrailes-de-coste-sizes-guard-opcional }

```bash
rayito stack deploy sizes-guard \
  --param ImageArns=<baseline ARN>,<rayito-base-4gb ARN>
```

It creates an IAM policy (`RayitoRunAllowedSizes`) with an Allow of
`lambda:RunMicrovm` on the listed ARNs and, above all, a Deny of
`lambda:RunMicrovm` with `NotResource` for any other ARN: an
identity with this policy attached cannot launch a size that is not
on the list, even if it exists, **even if it also has** the `microvm-image:*`
of the standard `CallerPolicy` in `infra/iam.yaml` (an explicit Deny always wins
over any Allow, whichever policy it comes from). `rayito
stack destroy sizes-guard` deletes the policy; no image is touched.

The policy also denies creating, updating or deleting images and versions
(`CreateMicrovmImage`, `UpdateMicrovmImage`, `UpdateMicrovmImageVersion`,
`DeleteMicrovmImageVersion`): size is a property of each version,
so without that Deny an identity that also has the `CallerPolicy`
could rebuild an allowed image at 8192 MiB and launch it without leaving
the list. Attach it to the identity that **launches** sandboxes
(`SandboxLauncherPolicy`) and publish the images with another
(`ImagePublisherPolicy` or `RayitoTemplateBuilder`).

!!! warning "Old versions of an allowed image"
    `lambda:RunMicrovm` is authorized against the image ARN without a version
    (`AWS_API_NOTES.md` §24), so the policy cannot prevent a specific
    `imageVersion`. If you ever published a bigger size under a name
    you now allow, delete those versions
    (`rayito image prune`): while they exist, they can be launched.

## Errors and troubleshooting { #errores-y-solucion-de-problemas }

| Python | TypeScript | When | What to do |
|---|---|---|---|
| `InvalidArgumentException` | `InvalidArgumentError` | `size=` asks for more than 8192 MiB, a name that is not in the catalog, or is combined with a template given by ARN | use a catalog name or `SizeRequest(memory_mib=...)`/`{ memoryMib }`, and pass the image name, not its ARN |
| `InvalidArgumentException` | `InvalidArgumentError` | `create(pool=, size=)` | fix the size by publishing the `PoolConfig`'s image with that suffix, not in `take()` |
| `SandboxNotFoundException` | `SandboxNotFoundError` | `size=` asks for a valid catalog size you never published (`No active version found for MicroVM image ...-8gb`); `RunMicrovm` rejects it without creating any MicroVM | publish it with `rayito image publish --sizes 8gb`, or ask for a size that exists (`rayito image sizes`) |
| `RayitoCompatWarning` | `process.emitWarning` warning | `size=` does not exactly match a catalog value | informational: the sandbox launches anyway, rounded up |

## Differences from E2B { #diferencias-con-e2b }

E2B has no named size catalog: each template declares its own
CPU/RAM when built. `size=` is a Rayito convenience on top of
the `<variant>[-<size>]` naming convention; the E2B shim exposes
no new kwarg for this.

## See also { #ver-tambien }

- [Optional features](../optional-features.md)
- [Optional stacks (`rayito stack`)](pilas-opcionales.md)
- [Cost](../cost.md)
- [IAM](../operacion/iam.md)

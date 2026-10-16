# Migrate from E2B

If your code uses the E2B 2.x SDK (`e2b`, `e2b_code_interpreter`, `e2b` from
npm or `@e2b/code-interpreter`), you can move it to your AWS account by
changing **one import line**. `rayito.e2b` (Python) and `rayito/e2b`
(TypeScript) implement the same API on top of Lambda MicroVMs.

!!! note "Trademarks"
    E2B is a trademark of its owner. Rayito is an independent project, not
    affiliated with, sponsored or endorsed by E2B; the name is used only to
    describe which SDK the `rayito.e2b` / `rayito/e2b` API is compatible with.

## In five steps { #en-cinco-pasos }

### 1. Prepare your AWS account { #1-prepara-tu-cuenta-de-aws }

Follow [Set up AWS](../primeros-pasos/configurar-aws.md): IAM stack, bucket
and the published `rayito-base` image. It is the equivalent of having an E2B
account with its default template.

### 2. Install Rayito { #2-instala-rayito }

=== "Python"

    ```bash
    pip uninstall e2b-code-interpreter e2b      # optional
    pip install rayito
    ```

=== "TypeScript"

    ```bash
    pnpm remove @e2b/code-interpreter e2b       # optional
    pnpm add rayito
    ```

### 3. Change the import { #3-cambia-el-import }

=== "Python"

    ```diff
    - from e2b_code_interpreter import Sandbox
    + from rayito.e2b import Sandbox
    ```

=== "TypeScript"

    ```diff
    - import { Sandbox } from "@e2b/code-interpreter";
    + import { Sandbox } from "rayito/e2b";
    ```

The rest of the program does not change:

=== "Python"

    ```python
    from rayito.e2b import Sandbox

    with Sandbox.create(timeout=300, metadata={"run": "42"}) as sbx:
        print(sbx.run_code("1 + 1").text)  # "2"
        print(sbx.commands.run("echo hello").stdout)  # "hello\n"
        sbx.files.write("/home/user/a.txt", "a")
        sbx.set_timeout(600)
    ```

=== "Python (async)"

    ```python
    import asyncio

    from rayito.e2b import AsyncSandbox


    async def main() -> None:
        async with await AsyncSandbox.create(timeout=300, metadata={"run": "42"}) as sbx:
            print((await sbx.run_code("1 + 1")).text)  # "2"
            print((await sbx.commands.run("echo hello")).stdout)
            await sbx.set_timeout(600)


    asyncio.run(main())
    ```

=== "TypeScript"

    ```ts
    import { Sandbox } from "rayito/e2b";

    await using sbx = await Sandbox.create({ timeoutMs: 300_000, metadata: { run: "42" } });
    console.log((await sbx.runCode("1 + 1")).text); // "2"
    console.log((await sbx.commands.run("echo hello")).stdout);
    await sbx.setTimeout(600_000);
    ```

### 4. Change the configuration { #4-cambia-la-configuracion }

| In E2B | In Rayito |
|---|---|
| `E2B_API_KEY` | AWS credentials: `AWS_PROFILE` and `AWS_REGION` (or the machine's role). `api_key=` is accepted and ignored with a warning |
| the default template (`code-interpreter-v1`) | `RAYITO_TEMPLATE=rayito-base` (or `template=` / `Sandbox.create("rayito-base")`) |
| `template="my-template"` built with `Template.build` | the same `Template.build` (since 0.6.0, composed on top of `rayito-base` with `from_base_image`; [Templates](../funciones-opcionales/templates.md)) or an image of your own published with `rayito image publish` |
| `upload_url` / `download_url` | a bucket of your own: `RAYITO_TRANSFER_BUCKET=amzn-s3-demo-bucket` ([Files and S3](../files.md)) |
| `allow_internet_access=False`, `network=` | the `rayito-base-caps` image ([Outbound network](../network.md)) |
| `run_code(language="js" / "ts" / "bash")` | the `rayito-base-poly` image ([Languages and kernels](../kernels.md)) |

### 5. Run it and review the warnings { #5-ejecutalo-y-revisa-los-avisos }

What the shim accepts but ignores warns with a `RayitoCompatWarning` (for
example, `api_key` or `domain`). What Lambda MicroVMs cannot do raises
`UnimplementedError` with the reason; it is never approximated silently. Run
your tests with warnings visible:

```bash
python -W always::UserWarning -m pytest
```

## Checklist { #lista-de-comprobacion }

- [ ] `rayito doctor` finishes without `FAIL` in the target account and region.
- [ ] `RAYITO_TEMPLATE` points to an image from the current release (the shim
      requires a 0.3.0 or later image).
- [ ] No call uses anything from [Not supported](../e2b-compat.md#lanza-unimplementederror)
      (`fork`, snapshots, `Volume` without `volume_store=`, `mcp=`, `iam=`...).
- [ ] If you store the `sandbox_id` to reconnect, also store the access
      token: `sbx.native.access_token` (TypeScript: `sbx.native.accessToken`).
      E2B does not need it; Rayito does, because there is no API key.
- [ ] If you use `upload_url`, the transfer bucket exists and your
      credentials have its permissions ([IAM](../operacion/iam.md)).
- [ ] If your sandboxes live longer than 8 hours (paused ones too), use
      [Persistence](../persistence.md) with `reincarnate()`.
- [ ] `except BuildException` imported from `rayito.e2b` catches the failed
      build; beware if an `except SandboxException` comes before it, because
      it intercepts it: in Rayito `BuildException` is a `SandboxException`
      ([Compatibility](../e2b-compat.md#tabla-de-imports)).

## What changes { #que-cambia }

The shim's defaults are E2B's (`timeout=300` as the logical deadline, no
auto-suspend, internet egress). The differences you will notice most:

| Topic | E2B | Rayito |
|---|---|---|
| Where it runs | E2B's cloud | your AWS account, on your bill |
| Authentication | `E2B_API_KEY` | AWS credentials + an access token per sandbox |
| Maximum lifetime | 24 h on Pro; paused with no limit | 8 h from start, counting paused time |
| CPU and memory | per template | per image: `Template.build(memory_mb=)` or `rayito image publish --sizes` and, in the native SDK, `size=` ([Sizes](../funciones-opcionales/tamanos.md)) |
| `get_host(port)` | public URL | hostname + AWS proxy headers on every request |
| `upload_url` | multipart `POST`, the file appears immediately | `PUT` to S3, lands in ≈ 1 s (or `ticket.wait()`); single use |
| Outbound network with rules | outside the VM | inside the guest, on `rayito-base-caps` |

All the differences, by area: [Differences](../e2b-compat.md#diferencias-por-area).
Row by row (113 features): [Parity](../e2b-parity.md).

## What does not exist { #que-no-existe }

`fork`, snapshots and `pause(keep_memory=False)`; the content operations of
`Volume` without a sandbox; `mcp=`; `iam=`; `network.rules` and the
resolution of `Secret.fill()`; R and Java kernels. All of them raise
`UnimplementedError` with the reason. What to use instead:
[What to do about what is missing](../e2b-parity.md#que-hacer-con-lo-que-no-esta).

Since 0.6.0 the following do exist, as optional features in your account
(off by default): `Template.build`
([Templates](../funciones-opcionales/templates.md)), S3 bucket mounts with
`mounts=` ([S3 mounts](../funciones-opcionales/montajes-s3.md)), lifecycle
events and webhooks with E2B's signature through `events=`
([Events and webhooks](../funciones-opcionales/eventos-y-webhooks.md)) and
export of the sandbox's metrics over OTLP with `telemetry=`
([OTLP export](../funciones-opcionales/exportacion-otlp.md)).
Since 0.7.0, `Volume` does real CRUD and `volume_mounts=` mounts EFS volumes
with `E2B(volume_store=..., volume_connector_arn=...)`, as an experimental
feature ([EFS volumes](../funciones-opcionales/volumenes-efs.md)).

## When to move to the native SDK { #cuando-pasar-al-sdk-nativo }

The shim covers E2B's API. The native SDK (`from rayito import Sandbox`) adds
what E2B does not have: a sandbox [pool](../pool.md) in < 1 s,
[persistence](../persistence.md) on S3, [auto-suspend](../guias/pausar-reanudar.md)
by default, the [optional features](../optional-features.md) and, since
0.8.0, a [coding agent](../guias/agente-en-el-sandbox.md) that runs inside
the sandbox (`sbx.agent`). From the shim, `sbx.native` is the native sandbox
(`sbx.native.agent`, `sbx.native.access_token`...), so you can migrate
gradually.

## See also { #ver-tambien }

- [Differences](../e2b-compat.md): import table, defaults, what maps with a
  note and what raises `UnimplementedError`.
- [Parity](../e2b-parity.md): E2B's 113 features, one per row.
- [Migrating from the 1.x shim](../e2b-compat.md#migrar-desde-el-shim-1x):
  if you used Rayito 0.2.

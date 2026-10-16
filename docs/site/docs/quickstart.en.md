# First sandbox

<a id="quickstart"></a><a id="instalacion"></a><a id="credenciales"></a><a id="la-imagen"></a>

On this page you create a sandbox, run a command, write and read a file,
run Python code with state and destroy the sandbox. Then you reconnect to a
sandbox from another process.

First you need the SDK installed ([Installation](primeros-pasos/instalacion.md))
and the `rayito-base` image published in your account
([Set up AWS](primeros-pasos/configurar-aws.md)):

```bash
export AWS_PROFILE=<your-profile> AWS_REGION=us-east-1 RAYITO_TEMPLATE=rayito-base
```

!!! tip "How to run each example"
    Save the code to a file and run it: `python first.py` in Python,
    `npx tsx first.ts` in TypeScript. In TypeScript the project needs
    `"type": "module"` and a `tsconfig.json` with
    `"lib": ["ES2022", "ESNext.Disposable"]`; the full recipe is in
    [Running the examples](primeros-pasos/instalacion.md#ejecutar-los-ejemplos).

## Create, use and destroy { #crear-usar-y-destruir }

=== "Python"

    ```python
    from rayito import Sandbox

    with Sandbox.create(timeout=900) as sbx:  # (1)!
        print(sbx.commands.run("echo hello").stdout)  # "hello\n"
        sbx.files.write("/home/user/a.txt", "content")
        print(sbx.files.read("/home/user/a.txt"))  # "content"
        print(sbx.run_code("x = 40; x + 2").text)  # "42" (2)
    ```

    1. `timeout` is the sandbox's maximum lifetime in seconds (3600 by
       default, capped at 8 h). `with` calls `kill()` on exit.
    2. The kernel keeps state between cells: `x` still exists in the next
       `run_code` call.

=== "Python (async)"

    ```python
    import asyncio

    from rayito import AsyncSandbox


    async def main() -> None:
        async with await AsyncSandbox.create(timeout=900) as sbx:
            print((await sbx.commands.run("echo hello")).stdout)  # "hello\n"
            await sbx.files.write("/home/user/a.txt", "content")
            print(await sbx.files.read("/home/user/a.txt"))  # "content"
            print((await sbx.run_code("x = 40; x + 2")).text)  # "42"


    asyncio.run(main())
    ```

=== "TypeScript"

    ```ts
    import { Sandbox } from "rayito";

    await using sbx = await Sandbox.create({ timeoutMs: 900_000 }); // (1)!
    console.log((await sbx.commands.run("echo hello")).stdout); // "hello\n"
    await sbx.files.write("/home/user/a.txt", "content");
    console.log(await sbx.files.read("/home/user/a.txt")); // "content"
    console.log((await sbx.runCode("x = 40; x + 2")).text); // "42"
    ```

    1. In TypeScript, times are in milliseconds (`timeoutMs`). `await using`
       calls `kill()` when the block exits.

=== "Shim E2B"

    ```python
    from rayito.e2b import Sandbox  # before: from e2b_code_interpreter import Sandbox

    with Sandbox.create(timeout=900) as sbx:
        print(sbx.commands.run("echo hello").stdout)  # "hello\n"
        sbx.files.write("/home/user/a.txt", "content")
        print(sbx.files.read("/home/user/a.txt"))  # "content"
        print(sbx.run_code("x = 40; x + 2").text)  # "42"
    ```

    The same program written for the E2B 2.x SDK, changing only the import.
    The shim follows E2B's defaults (`timeout=300`, no auto-suspend) and
    requires a 0.3.0 or later image:
    [Migrate from E2B](migrar-desde-e2b/index.md).

What happens underneath:

1. `create()` launches a MicroVM from the image (`run-microvm`) and waits
   for `rayd`, the agent inside, to be ready: about 2 s to the agent and
   5–6 s to the Python kernel.
2. Each call travels over gRPC through the AWS proxy, authenticated with a
   proxy token and with the sandbox's *access token*.
3. When the block exits, `kill()` terminates the MicroVM
   (`terminate-microvm`).

!!! warning "Always release the sandbox"
    Without `with` / `await using`, call `kill()` yourself. A forgotten
    sandbox keeps billing ≈ $0.126/h (2 GB, [Pricing](cost.md#cuanto-cuesta-con-ejemplos))
    until its `timeout`. To find orphans: `rayito sandbox list` and
    `rayito sandbox kill`.

## Reconnect from another process { #reconectar-desde-otro-proceso }

A sandbox does not depend on the process that created it. Save its
`sandbox_id` and its *access token* and connect from another process,
another machine or after a restart:

=== "Python"

    ```python
    from rayito import Sandbox

    sbx = Sandbox.create()
    sandbox_id, token = sbx.sandbox_id, sbx.access_token  # (1)!

    again = Sandbox.connect(sandbox_id, access_token=token)
    print(again.commands.run("hostname").stdout)
    again.kill()
    ```

    1. The access token is a secret: store it as you would a password.
       Without it you cannot talk to the sandbox.

=== "Python (async)"

    ```python
    import asyncio

    from rayito import AsyncSandbox


    async def main() -> None:
        sbx = await AsyncSandbox.create()
        sandbox_id, token = sbx.sandbox_id, sbx.access_token

        again = await AsyncSandbox.connect(sandbox_id, access_token=token)
        print((await again.commands.run("hostname")).stdout)
        await again.kill()


    asyncio.run(main())
    ```

=== "TypeScript"

    ```ts
    import { Sandbox } from "rayito";

    const sbx = await Sandbox.create();
    const { sandboxId, accessToken } = sbx;

    const again = await Sandbox.connect(sandboxId, { accessToken });
    console.log((await again.commands.run("hostname")).stdout);
    await again.kill();
    ```

`connect()` resumes the sandbox if it was paused. Without `timeout=` it does
not extend its deadline; with it, it only extends it
([Server-side deadline](lifecycle.md)).

## Recap { #recap }

- `Sandbox.create()` launches a MicroVM in your account; `with` /
  `await using` destroys it when done.
- `commands.run` runs processes, `files` reads and writes files and
  `run_code` runs code in a kernel with state.
- `sandbox_id` + `access_token` are enough to reconnect from any process
  with AWS credentials.

<a id="siguiente"></a>

## Next step { #siguiente-paso }

- [Concepts](concepts.md): what runs where, deadlines, tokens and
  reconnection.
- [Commands](guias/comandos.md), [Run code](guias/ejecutar-codigo.md) and
  [Files](files.md): each feature in detail.
- [Agent in the sandbox](guias/agente-en-el-sandbox.md): a coding agent
  (OpenCode or deepagents) inside the sandbox, with a normal `create()` and
  `sbx.agent.run(...)`.
- [Migrate from E2B](migrar-desde-e2b/index.md): if you come from
  `e2b_code_interpreter` or `@e2b/code-interpreter`.
- [Pricing](cost.md): what each operation costs, measured.

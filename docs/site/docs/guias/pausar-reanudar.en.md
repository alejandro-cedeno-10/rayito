# Pause and resume

`pause()` freezes the whole sandbox into a memory snapshot and `resume()`
wakes it up just as it was: processes, terminals, files and the kernel
variables are still alive on the other side.

## When to use it { #cuando-usarlo }

- An agent waiting for the user for minutes or hours: while paused it pays
  no compute, only the snapshot storage.
  For a [coding agent](agente-en-el-sandbox.md) it is the default option
  between turns: see [Which one do I use?](agente-en-el-sandbox.md#que-uso).
- Work with gaps: **auto-suspend** (on by default) pauses the sandbox after
  300 s without traffic and resumes it with the next call.
- **When not to**: pauses shorter than ≈ 150 s. A suspend/resume cycle
  costs the same as ≈ 140 s of compute ([Costs](../cost.md#precios)), so it
  saves no money.
- **Nor** for waits longer than 8 h: suspended time counts toward the
  maximum lifetime and the sandbox ends anyway. Save `HOME` with
  [persistence](../persistence.md) (`persist=`) and create another VM when
  needed; for an agent, see point 2 of
  [Which one do I use?](agente-en-el-sandbox.md#que-uso).

## Quick example { #ejemplo-rapido }

=== "Python"

    ```python
    from rayito import Sandbox

    with Sandbox.create() as sbx:
        sbx.run_code("x = 42")
        sbx.pause()  # (1)!
        sbx.resume()
        print(sbx.run_code("x").text)  # "42": the kernel kept its state
        print(sbx.get_health().resume_generation)  # 1
    ```

    1. `suspend-microvm`. It takes ≈ 1.4 s; `resume()` ≈ 0.4 s.

=== "Python (async)"

    ```python
    import asyncio

    from rayito import AsyncSandbox


    async def main() -> None:
        async with await AsyncSandbox.create() as sbx:
            await sbx.run_code("x = 42")
            await sbx.pause()
            await sbx.resume()
            print((await sbx.run_code("x")).text)  # "42"
            print((await sbx.get_health()).resume_generation)  # 1


    asyncio.run(main())
    ```

=== "TypeScript"

    ```ts
    import { Sandbox } from "rayito";

    await using sbx = await Sandbox.create();
    await sbx.runCode("x = 42");
    await sbx.pause();
    await sbx.resume();
    console.log((await sbx.runCode("x")).text); // "42"
    console.log((await sbx.getHealth()).resumeGeneration); // 1
    ```

=== "Shim E2B"

    ```python
    from rayito.e2b import Sandbox

    with Sandbox.create() as sbx:
        sbx.run_code("x = 42")
        sbx.pause()  # (1)!
        sbx.connect()  # (2)!
        print(sbx.run_code("x").text)  # "42"
    ```

    1. `pause(keep_memory=False)` is `UnimplementedError`: the pause always
       saves memory and disk. `beta_pause()` is an alias.
    2. As in E2B, `connect()` resumes; the class form
       `Sandbox.connect(sandbox_id, access_token=...)` does it from another
       process.

## Step by step { #paso-a-paso }

### Manual pause { #pausa-manual }

`pause()` asks AWS to suspend the MicroVM and waits for `SUSPENDED`. It
returns `False` if it was already suspended. Open streams (a background
command, a PTY, a `watch_dir`, a running cell) are cut with an orderly end
and **reattach on their own** the next time you read them after resuming.

`resume()` resumes the MicroVM, waits for the agent and records the new
generation (`get_health().resume_generation`). If a kernel did not survive,
`get_health().kernel_state_lost` reports it.

`pause(wait=False)` and `resume(wait=False)` (TypeScript: `{ wait: false }`)
request the transition and return without waiting for it.

Without a handle, the class forms do the same:
`Sandbox.pause(sandbox_id)` / `Sandbox.resume(sandbox_id)` (TypeScript:
`Sandbox.pause(sandboxId)` / `Sandbox.resume(sandboxId)`). They do not need
the access token, but they cannot wait for the agent either: `resume` only
waits for `get-microvm` to say `RUNNING`.

### Continue from another process { #continuar-desde-otro-proceso }

`connect()` also resumes: `sbx.connect()` on the handle you already have,
or `Sandbox.connect(sandbox_id, access_token=...)` from another process (a
worker, an agent's next turn). It is the pattern of a conversation paused
between turns ([Lifecycle: connect](ciclo-de-vida.md#conectar)).

=== "Python"

    ```python
    from rayito import Sandbox

    sbx = Sandbox.create()
    sandbox_id, token = sbx.sandbox_id, sbx.access_token
    sbx.pause()
    sbx.close()  # (1)!

    again = Sandbox.connect(sandbox_id, access_token=token)  # (2)!
    try:
        print(again.commands.run("echo back").stdout)
    finally:
        again.kill()
    ```

    1. The sandbox stays suspended; only the local handle is released.
    2. It resumes and waits for the agent.

=== "Python (async)"

    ```python
    import asyncio

    from rayito import AsyncSandbox


    async def main() -> None:
        sbx = await AsyncSandbox.create()
        sandbox_id, token = sbx.sandbox_id, sbx.access_token
        await sbx.pause()
        await sbx.close()

        async with await AsyncSandbox.connect(sandbox_id, access_token=token) as again:
            print((await again.commands.run("echo back")).stdout)


    asyncio.run(main())
    ```

=== "TypeScript"

    ```ts
    import { Sandbox } from "rayito";

    const sbx = await Sandbox.create();
    const { sandboxId, accessToken } = sbx;
    await sbx.pause();
    sbx.close(); // the sandbox stays suspended

    await using again = await Sandbox.connect(sandboxId, { accessToken });
    console.log((await again.commands.run("echo back")).stdout);
    ```

### Auto-suspend and `IdlePolicy` { #auto-suspension-e-idlepolicy }

By default every sandbox is created with `IdlePolicy(max_idle_seconds=300,
auto_resume=True)`: AWS suspends it after 300 s without traffic through its
endpoint, and the SDK's next call wakes it on its own (≈ 0.7 s).

If you do not pass `idle` and that 300 s window does not fit before the end
of the sandbox (`timeout`, or `max_lifetime` with `on_timeout`), the
default auto-suspend is turned off: it would never fire and the sandbox
ends at its `timeout`, as in E2B. That way `Sandbox.create(timeout=120)`
just works. With `on_timeout="pause"` the idle policy is what suspends with
no client, so instead of turning off it drops to 60 s (the minimum). An
`idle` you pass yourself is not adapted: if it does not fit, it is
`InvalidArgumentException` / `InvalidArgumentError`.

=== "Python"

    ```python
    from rayito import IdlePolicy, Sandbox

    with Sandbox.create(idle=IdlePolicy(max_idle_seconds=900)) as sbx:  # (1)!
        print(sbx.commands.run("echo awake").stdout)

    with Sandbox.create(idle=None) as sbx:  # (2)!
        print(sbx.commands.run("echo never suspends").stdout)
    ```

    1. Suspends after 15 minutes without traffic.
    2. No auto-suspend: it bills compute until its `timeout`.

=== "Python (async)"

    ```python
    import asyncio

    from rayito import AsyncSandbox, IdlePolicy


    async def main() -> None:
        async with await AsyncSandbox.create(idle=IdlePolicy(max_idle_seconds=900)) as sbx:
            print((await sbx.commands.run("echo awake")).stdout)
        async with await AsyncSandbox.create(idle=None) as sbx:
            print((await sbx.commands.run("echo never suspends")).stdout)


    asyncio.run(main())
    ```

=== "TypeScript"

    ```ts
    import { Sandbox } from "rayito";

    {
      await using sbx = await Sandbox.create({ idle: { maxIdleSeconds: 900 } });
      console.log((await sbx.commands.run("echo awake")).stdout);
    }
    {
      await using sbx = await Sandbox.create({ idle: null });
      console.log((await sbx.commands.run("echo never suspends")).stdout);
    }
    ```

!!! warning "A silent cell can be suspended midway"
    Inactivity counts the bytes that cross the endpoint. A cell or a command
    that prints nothing for `max_idle_seconds` can be suspended while it
    runs. The SDK waits for the resume and reattaches, but if your tasks are
    long and silent, raise `max_idle_seconds` or pass `idle=None`.

!!! note "Reading a handle does not wake the sandbox"
    While it is suspended, reading a background command, a PTY or a watch
    blocks until something resumes it (`resume()`, or any new call such as
    `commands.run` with auto-resume). It does not consume the reconnection
    budget.

## `IdlePolicy` options { #opciones-de-idlepolicy }

| Python | TypeScript | Default | What it does |
|---|---|---|---|
| `max_idle_seconds` | `maxIdleSeconds` | 300 | seconds without traffic before suspending (minimum 60) |
| `auto_resume` | `autoResume` | `True` | the next call resumes a suspended sandbox |
| `suspended_duration_seconds` | `suspendedDurationSeconds` | `timeout − max_idle_seconds` | how long it can stay suspended before ending; `0` ends it on suspend |
| `idle=None` | `idle: null` | — | turns auto-suspend off |
| no `idle` | no `idle` | 300 s | turned off if it does not fit before the `timeout` (in `pause` mode, 60 s) |

## Cost { #coste }

- **Suspended**: pays no compute, only the snapshot storage.
- **Each suspend + resume cycle** writes and reads the whole snapshot: it
  costs the same as ≈ 140 s of compute.
- **Running**: the normal compute for the image size.

Prices and examples, with sources: [Costs](../cost.md#precios).

## Errors and troubleshooting { #errores-y-solucion-de-problemas }

| Python | TypeScript | When | What to do |
|---|---|---|---|
| `SandboxStateException` | `SandboxStateError` | the sandbox is ending or in a transition that does not allow the operation | wait and retry, or create a new one |
| `SandboxNotFoundException` | `SandboxNotFoundError` | the sandbox no longer exists (it reached its `timeout`, even when suspended) | create a new one; for more than 8 h, [Persistence](../persistence.md) (`persist=`) |
| `InvalidArgumentException` | `InvalidArgumentError` | an explicit `idle` with `max_idle_seconds` greater than or equal to the `timeout` (or `max_lifetime`) | lower `max_idle_seconds`, pass `idle=None` or do not pass `idle` |
| `RateLimitException` | `RateLimitError` | more than 2 suspensions per second in the account | the SDK already limits the rate per process; space out the pauses |

## Differences from E2B { #diferencias-con-e2b }

- Suspended time counts toward the 8 h maximum lifetime: a paused sandbox
  also ends 8 h after it started.
- `pause()` always saves memory: in the shim, E2B's
  `pause(keep_memory=False)` is `UnimplementedError`.
- Besides `connect()` (E2B's form), Rayito has an explicit `resume()`.

## See also { #ver-tambien }

- [Concepts: deadline, cap and idle](../concepts.md#plazo-tope-e-idle)
- [Server-side deadline](../lifecycle.md): pause when a deadline expires
- [Pool](../pool.md): suspended sandboxes ready to use
- [Costs](../cost.md)

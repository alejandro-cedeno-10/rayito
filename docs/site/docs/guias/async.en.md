# Async

`AsyncSandbox` is the same surface as `Sandbox` on top of `asyncio`: every
method is a coroutine. In TypeScript everything is asynchronous from the
start.

## When to use it { #cuando-usarlo }

- Your application is already `asyncio` (FastAPI, an async agent, a bot).
- You want several sandboxes or several operations in parallel without
  threads.
- **When not to**: scripts, notebooks and sequential CLIs; `Sandbox` is
  simpler.

## Quick example { #ejemplo-rapido }

=== "Python (async)"

    ```python
    import asyncio

    from rayito import AsyncSandbox


    async def main() -> None:
        async with await AsyncSandbox.create(metadata={"run": "42"}) as sbx:  # (1)!
            print((await sbx.run_code("1 + 1")).text)  # "2"
            print((await sbx.commands.run("echo hello")).stdout)  # "hello\n"
            await sbx.files.write("/home/user/a.txt", "a")


    asyncio.run(main())
    ```

    1. `await AsyncSandbox.create()` returns the sandbox; `async with` kills
       it on exit.

=== "Python"

    ```python
    from rayito import Sandbox

    with Sandbox.create(metadata={"run": "42"}) as sbx:
        print(sbx.run_code("1 + 1").text)  # "2"
        print(sbx.commands.run("echo hello").stdout)  # "hello\n"
        sbx.files.write("/home/user/a.txt", "a")
    ```

=== "TypeScript"

    ```ts
    import { Sandbox } from "rayito";

    await using sbx = await Sandbox.create({ metadata: { run: "42" } });
    console.log((await sbx.runCode("1 + 1")).text); // "2"
    console.log((await sbx.commands.run("echo hello")).stdout); // "hello\n"
    await sbx.files.write("/home/user/a.txt", "a");
    ```

## Several sandboxes in parallel { #varios-sandboxes-en-paralelo }

=== "Python (async)"

    ```python
    import asyncio

    from rayito import AsyncSandbox


    async def evaluate(code: str) -> str | None:
        async with await AsyncSandbox.create() as sbx:
            return (await sbx.run_code(code)).text


    async def main() -> None:
        results = await asyncio.gather(*(evaluate(f"{n} ** 2") for n in range(5)))
        print(results)  # ['0', '1', '4', '9', '16']


    asyncio.run(main())
    ```

=== "TypeScript"

    ```ts
    import { Sandbox } from "rayito";

    async function evaluate(code: string): Promise<string | undefined> {
      await using sbx = await Sandbox.create();
      return (await sbx.runCode(code)).text;
    }

    const results = await Promise.all([0, 1, 2, 3, 4].map((n) => evaluate(`${n} ** 2`)));
    console.log(results); // ["0", "1", "4", "9", "16"]
    ```

AWS calls go through a per-process rate limiter aligned with the API quotas
([Limits](../limits.md)): launching 20 sandboxes at once is safe, it just
takes a little longer.

## Equivalents { #equivalencias }

| Synchronous Python | Async Python | TypeScript |
|---|---|---|
| `Sandbox.create(...)` | `await AsyncSandbox.create(...)` | `await Sandbox.create({...})` |
| `with Sandbox.create() as sbx:` | `async with await AsyncSandbox.create() as sbx:` | `await using sbx = await Sandbox.create();` |
| `sbx.kill()` | `await sbx.kill()` | `await sbx.kill()` |
| `sbx.close()` | `await sbx.close()` | `sbx.close()` |
| `Sandbox.connect(id, access_token=t)` | `await AsyncSandbox.connect(id, access_token=t)` | `await Sandbox.connect(id, { accessToken: t })` |
| `for item in Sandbox.list():` | `for item in await AsyncSandbox.list():` | `for await (const item of Sandbox.list())` |
| `for chunk in handle:` | `async for chunk in handle:` | `for await (const chunk of handle)` |
| `sbx.files.watch_dir(...)` (with `with`) | `await sbx.files.watch_dir(...)` (with `async with`) | `await using watch = await sbx.files.watchDir(...)` |
| `SandboxPool` | `AsyncSandboxPool` | `SandboxPool` |
| `timeout=60` (seconds) | `timeout=60` | `timeoutMs: 60_000` |
| `on_stdout=` | `on_stdout=` | `onStdout:` |

Naming rules between Python and TypeScript: `snake_case` versus
`camelCase` and seconds versus milliseconds (`timeout` → `timeoutMs`,
`request_timeout` → `requestTimeoutMs`). The [TypeScript
reference](../referencia/typescript.md) lists every class.

## Implementation details { #detalles-de-la-implementacion }

- RPCs to the agent go through `grpc.aio`; calls to the AWS control plane
  (`boto3`, which is synchronous) go through `asyncio.to_thread`.
- `rayito.e2b.AsyncSandbox` (the E2B shim) wraps the native `AsyncSandbox`
  with the E2B signatures; the native one is at `sbx.native`.
- TypeScript has no synchronous tree: Node has no blocking gRPC client,
  just like E2B's JS SDK.

## See also { #ver-tambien }

- [Lifecycle](ciclo-de-vida.md)
- [Python reference: `AsyncSandbox`](../referencia/python/sandbox.md)
- [TypeScript reference](../referencia/typescript.md)

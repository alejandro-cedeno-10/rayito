# Async

`AsyncSandbox` es la misma superficie que `Sandbox` sobre `asyncio`: cada
método es una corrutina. En TypeScript todo es asíncrono desde el principio.

## Cuándo usarlo

- Tu aplicación ya es `asyncio` (FastAPI, un agente asíncrono, un bot).
- Quieres varios sandboxes o varias operaciones en paralelo sin hilos.
- **Cuándo no**: scripts, notebooks y CLIs secuenciales; `Sandbox` es más
  simple.

## Ejemplo rápido

=== "Python (async)"

    ```python
    import asyncio

    from rayito import AsyncSandbox


    async def main() -> None:
        async with await AsyncSandbox.create(metadata={"run": "42"}) as sbx:  # (1)!
            print((await sbx.run_code("1 + 1")).text)  # "2"
            print((await sbx.commands.run("echo hola")).stdout)  # "hola\n"
            await sbx.files.write("/home/user/a.txt", "a")


    asyncio.run(main())
    ```

    1. `await AsyncSandbox.create()` devuelve el sandbox; `async with` lo
       mata al salir.

=== "Python"

    ```python
    from rayito import Sandbox

    with Sandbox.create(metadata={"run": "42"}) as sbx:
        print(sbx.run_code("1 + 1").text)  # "2"
        print(sbx.commands.run("echo hola").stdout)  # "hola\n"
        sbx.files.write("/home/user/a.txt", "a")
    ```

=== "TypeScript"

    ```ts
    import { Sandbox } from "rayito";

    await using sbx = await Sandbox.create({ metadata: { run: "42" } });
    console.log((await sbx.runCode("1 + 1")).text); // "2"
    console.log((await sbx.commands.run("echo hola")).stdout); // "hola\n"
    await sbx.files.write("/home/user/a.txt", "a");
    ```

## Varios sandboxes en paralelo

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

Las llamadas a AWS pasan por un limitador de ritmo por proceso alineado con
las cuotas de la API (5 `run-microvm` por segundo): lanzar 20 sandboxes a la
vez es seguro, sólo tarda algo más.

## Equivalencias

| Python síncrono | Python async | TypeScript |
|---|---|---|
| `Sandbox.create(...)` | `await AsyncSandbox.create(...)` | `await Sandbox.create({...})` |
| `with Sandbox.create() as sbx:` | `async with await AsyncSandbox.create() as sbx:` | `await using sbx = await Sandbox.create();` |
| `sbx.kill()` | `await sbx.kill()` | `await sbx.kill()` |
| `for item in Sandbox.list():` | `for item in await AsyncSandbox.list():` | `for await (const item of Sandbox.list())` |
| `for chunk in handle:` | `async for chunk in handle:` | `for await (const chunk of handle)` |
| `sbx.files.watch_dir(...)` (con `with`) | `await sbx.files.watch_dir(...)` (con `async with`) | `await using watch = await sbx.files.watchDir(...)` |
| `SandboxPool` | `AsyncSandboxPool` | `SandboxPool` |
| `timeout=60` (segundos) | `timeout=60` | `timeoutMs: 60_000` |
| `on_stdout=` | `on_stdout=` | `onStdout:` |

Reglas de nombres entre Python y TypeScript: `snake_case` frente a
`camelCase` y segundos frente a milisegundos (`timeout` → `timeoutMs`,
`request_timeout` → `requestTimeoutMs`). La [referencia de
TypeScript](../referencia/typescript.md) lista cada clase.

## Detalles de la implementación

- Los RPC al agente van por `grpc.aio`; las llamadas al plano de control de
  AWS (`boto3`, que es síncrono) van por `asyncio.to_thread`.
- `rayito.e2b.AsyncSandbox` (el shim de E2B) es el mismo `AsyncSandbox`.
- En TypeScript no hay árbol síncrono: Node no tiene un cliente gRPC
  bloqueante, igual que el SDK JS de E2B.

## Ver también

- [Ciclo de vida](ciclo-de-vida.md)
- [Referencia Python: `AsyncSandbox`](../referencia/python/sandbox.md)
- [Referencia TypeScript](../referencia/typescript.md)

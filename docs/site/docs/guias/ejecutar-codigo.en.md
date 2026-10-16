# Run code

`run_code` runs a cell in a stateful Jupyter kernel inside the sandbox, like
a notebook: variables, imports and open files are still there on the next
call.

## When to use it { #cuando-usarlo }

- The typical agent case: generate Python, run it and read the result, the
  output or the error traceback.
- Data analysis with pandas and charts with matplotlib: charts come back as
  PNG and as structured data.
- **When not to**: for system processes or servers, use
  [commands](comandos.md). For bash, JavaScript or TypeScript, see
  [Languages and kernels](../kernels.md).

## Quick example { #ejemplo-rapido }

=== "Python"

    ```python
    from rayito import Sandbox

    with Sandbox.create() as sbx:
        sbx.run_code("x = 42")
        print(sbx.run_code("x").text)  # "42": the kernel has state

        failed = sbx.run_code("1/0")
        print(failed.error.name if failed.error else None)  # "ZeroDivisionError" (1)
    ```

    1. An error in the code is data on the `Execution`, never a Python
       exception.

=== "Python (async)"

    ```python
    import asyncio

    from rayito import AsyncSandbox


    async def main() -> None:
        async with await AsyncSandbox.create() as sbx:
            await sbx.run_code("x = 42")
            print((await sbx.run_code("x")).text)  # "42"

            failed = await sbx.run_code("1/0")
            print(failed.error.name if failed.error else None)  # "ZeroDivisionError"


    asyncio.run(main())
    ```

=== "TypeScript"

    ```ts
    import { Sandbox } from "rayito";

    await using sbx = await Sandbox.create();
    await sbx.runCode("x = 42");
    console.log((await sbx.runCode("x")).text); // "42"

    const failed = await sbx.runCode("1/0");
    console.log(failed.error?.name); // "ZeroDivisionError"
    ```

## Step by step { #paso-a-paso }

### What it returns: `Execution` { #que-devuelve-execution }

| Field | What it is |
|---|---|
| `text` | the `text/plain` of the main result (the value of the last expression), or `None` |
| `results` | list of `Result`, one per rich output (`display()`, charts, the last expression) |
| `logs.stdout`, `logs.stderr` | lists with what the cell printed |
| `error` | `ExecutionError(name, value, traceback)` if the cell raised, or `None` |
| `execution_count` | the kernel's counter |

Each `Result` carries the representations the kernel produced: `text`,
`html`, `markdown`, `svg`, `png`, `jpeg`, `pdf`, `latex`, `json`,
`javascript`, `chart` (chart data) and `data`. `formats()` tells you which
ones are present, `is_main_result` (TypeScript: `isMainResult`) marks the
value of the last expression and `extra` holds the MIME types that have no
field of their own (and the `rayito/omitted` notice of a result that is
[too large](../limits.md)).

### Charts { #graficos }

=== "Python"

    ```python
    import base64

    from rayito import Sandbox

    with Sandbox.create() as sbx:
        plot = sbx.run_code("import matplotlib.pyplot as plt; plt.plot([1, 2, 3]); plt.show()")
        chart = plot.results[0]
        print(chart.formats())  # includes 'png' and 'chart'
        with open("chart.png", "wb") as out:
            out.write(base64.b64decode(chart.png or ""))  # (1)!
    ```

    1. `png` arrives in base64, as in Jupyter.

=== "Python (async)"

    ```python
    import asyncio
    import base64

    from rayito import AsyncSandbox


    async def main() -> None:
        async with await AsyncSandbox.create() as sbx:
            plot = await sbx.run_code("import matplotlib.pyplot as plt; plt.plot([1, 2, 3]); plt.show()")
            chart = plot.results[0]
            print(chart.formats())  # includes 'png' and 'chart'
            with open("chart.png", "wb") as out:
                out.write(base64.b64decode(chart.png or ""))


    asyncio.run(main())
    ```

=== "TypeScript"

    ```ts
    import { writeFile } from "node:fs/promises";
    import { Sandbox } from "rayito";

    await using sbx = await Sandbox.create();
    const plot = await sbx.runCode("import matplotlib.pyplot as plt; plt.plot([1, 2, 3]); plt.show()");
    const chart = plot.results[0];
    console.log(chart?.formats()); // includes "png" and "chart"
    await writeFile("chart.png", Buffer.from(chart?.png ?? "", "base64"));
    ```

### Streaming output { #salida-en-streaming }

`on_stdout`, `on_stderr`, `on_result` and `on_error` receive each output
while the cell runs, without waiting for the end:

=== "Python"

    ```python
    from rayito import Sandbox

    with Sandbox.create() as sbx:
        sbx.run_code(
            "import time\nfor i in range(3):\n    print(i); time.sleep(1)",
            on_stdout=lambda message: print("stdout:", message.line, end=""),
        )
    ```

=== "Python (async)"

    ```python
    import asyncio

    from rayito import AsyncSandbox


    async def main() -> None:
        async with await AsyncSandbox.create() as sbx:
            await sbx.run_code(
                "import time\nfor i in range(3):\n    print(i); time.sleep(1)",
                on_stdout=lambda message: print("stdout:", message.line, end=""),
            )


    asyncio.run(main())
    ```

=== "TypeScript"

    ```ts
    import { Sandbox } from "rayito";

    await using sbx = await Sandbox.create();
    await sbx.runCode("import time\nfor i in range(3):\n    print(i); time.sleep(1)", {
      onStdout: (message) => process.stdout.write(`stdout: ${message.line}`),
    });
    ```

### Contexts { #contextos }

Each context is an independent kernel with its own state. The `default`
context always exists; `create_code_context()` creates others (with a cap
per sandbox, see [Limits](../limits.md)), with their own `cwd`, `language`,
`envs` and, optionally, their [`secrets`](../secrets.md). It returns a
`CodeContext` (`id`, `language`, `cwd`); `run_code(context=)`,
`restart_code_context()` and `remove_code_context()` accept the object or
its `id`.

=== "Python"

    ```python
    from rayito import Sandbox

    with Sandbox.create() as sbx:
        sbx.run_code("x = 1")
        ctx = sbx.create_code_context(cwd="/tmp")
        print(sbx.run_code("x", context=ctx).error)  # NameError: another kernel
        print([c.id for c in sbx.list_code_contexts()])  # ['default', 'ctx-…']
        sbx.restart_code_context(ctx)  # clears its state
        sbx.remove_code_context(ctx)
    ```

=== "Python (async)"

    ```python
    import asyncio

    from rayito import AsyncSandbox


    async def main() -> None:
        async with await AsyncSandbox.create() as sbx:
            await sbx.run_code("x = 1")
            ctx = await sbx.create_code_context(cwd="/tmp")
            print((await sbx.run_code("x", context=ctx)).error)  # NameError
            await sbx.remove_code_context(ctx)


    asyncio.run(main())
    ```

=== "TypeScript"

    ```ts
    import { Sandbox } from "rayito";

    await using sbx = await Sandbox.create();
    await sbx.runCode("x = 1");
    const ctx = await sbx.createCodeContext({ cwd: "/tmp" });
    console.log((await sbx.runCode("x", { context: ctx })).error?.name); // "NameError"
    console.log((await sbx.listCodeContexts()).map((c) => c.id));
    await sbx.restartCodeContext(ctx);
    await sbx.removeCodeContext(ctx);
    ```

### Timeouts { #timeouts }

`timeout` (300 s by default) is enforced by the agent: when it expires it
interrupts the kernel and the `Execution` comes back with
`error.name == "ExecutionTimeout"`. The kernel stays usable for the next
cell; if it does not recover within 5 s, the agent restarts that context
(and its state is lost).

## `run_code` options { #opciones-de-run_code }

| Python | TypeScript | Default | What it does |
|---|---|---|---|
| `code` | `code` | — | the cell's code |
| `language` | `language` | `"python"` | `bash`, `javascript`/`js`, `typescript`/`ts` on `rayito-base-poly` ([Kernels](../kernels.md)) |
| `context` | `context` | `default` | context (object or id); mutually exclusive with `language` |
| `on_stdout`, `on_stderr` | `onStdout`, `onStderr` | — | each printed line, as an `OutputMessage` |
| `on_result`, `on_error` | `onResult`, `onError` | — | each `Result` and the error |
| `envs` | `envs` | — | environment variables for this cell only (Python contexts) |
| `timeout` | `timeoutMs` | 300 s / 300,000 ms | the agent's limit; `None` / `0` for no limit |
| `request_timeout` | `requestTimeoutMs` | 60 s | call deadline |
| `secrets` | `secrets` | — | [secrets](../secrets.md) in the cell's environment (optional, with a cost) |

## Errors and troubleshooting { #errores-y-solucion-de-problemas }

| Situation | Python | TypeScript | What to do |
|---|---|---|---|
| the code raises | `execution.error` (not an exception) | `execution.error` | read `error.name`, `error.value` and `error.traceback` |
| the cell takes longer than `timeout` | `error.name == "ExecutionTimeout"` | same | raise `timeout` |
| `language` and `context` together, `envs` on a non-Python kernel, `code` over 1 MiB | `InvalidArgumentException` | `InvalidArgumentError` | pick one; for large code, write it to a file and run it |
| a kernel the image does not ship (`bash` on `rayito-base`) | `UnimplementedError` | `UnimplementedError` | use `rayito-base-poly` |
| too many contexts ([Limits](../limits.md)) | `RateLimitException` | `RateLimitError` | delete contexts with `remove_code_context` |
| a silent cell is suspended midway | (the SDK reattaches on its own) | same | raise `max_idle_seconds` ([Pause and resume](pausar-reanudar.md)) |

## Differences from E2B { #diferencias-con-e2b }

- The same API: `rayito.e2b` (Python and TypeScript) calls the native
  `run_code` and returns the same `Execution`. Two details of the shim: a
  `language` that does not exist (`r`, `java`) is `UnimplementedError`
  without calling the agent, as in E2B, and in Python `timeout=None` means
  the 300 s default (in the native one, `None` means no limit). In the
  Python shim, `secrets=` and the `envs` of `create_code_context` are only
  in the native one (`sbx.native`).
- Languages: Python always; bash, JavaScript and TypeScript on
  `rayito-base-poly`; R and Java do not exist.

## See also { #ver-tambien }

- [Languages and kernels](../kernels.md)
- [Commands](comandos.md)
- [MCP server](../mcp.md): `run_code` as an agent tool
- Reference: [Python](../referencia/python/sandbox.md) and
  [TypeScript](../referencia/typescript.md#ejecutar-codigo)

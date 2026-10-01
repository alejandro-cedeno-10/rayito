# Ejecutar código

`run_code` ejecuta una celda en un kernel Jupyter con estado dentro del
sandbox, como un notebook: las variables, los imports y los ficheros
abiertos siguen ahí en la siguiente llamada.

## Cuándo usarlo

- El caso típico de un agente: generar Python, ejecutarlo y leer el
  resultado, la salida o la traza del error.
- Análisis de datos con pandas y gráficos con matplotlib: los gráficos
  vuelven como PNG y como datos estructurados.
- **Cuándo no**: para procesos de sistema o servidores, usa
  [comandos](comandos.md). Para bash, JavaScript o TypeScript, mira
  [Lenguajes y kernels](../kernels.md).

## Ejemplo rápido

=== "Python"

    ```python
    from rayito import Sandbox

    with Sandbox.create() as sbx:
        sbx.run_code("x = 42")
        print(sbx.run_code("x").text)  # "42": el kernel tiene estado

        failed = sbx.run_code("1/0")
        print(failed.error.name if failed.error else None)  # "ZeroDivisionError" (1)
    ```

    1. Un error del código es un dato de la `Execution`, nunca una excepción
       de Python.

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

## Paso a paso

### Qué devuelve: `Execution`

| Campo | Qué es |
|---|---|
| `text` | el `text/plain` del resultado principal (el valor de la última expresión), o `None` |
| `results` | lista de `Result`, uno por cada salida rica (`display()`, gráficos, la última expresión) |
| `logs.stdout`, `logs.stderr` | listas con lo que la celda imprimió |
| `error` | `ExecutionError(name, value, traceback)` si la celda lanzó, o `None` |
| `execution_count` | el contador del kernel |

Cada `Result` trae las representaciones que produjo el kernel: `text`,
`html`, `markdown`, `svg`, `png`, `jpeg`, `pdf`, `latex`, `json`,
`javascript`, `chart` (datos del gráfico) y `data`. `formats()` dice cuáles
hay.

### Gráficos

=== "Python"

    ```python
    import base64

    from rayito import Sandbox

    with Sandbox.create() as sbx:
        plot = sbx.run_code("import matplotlib.pyplot as plt; plt.plot([1, 2, 3]); plt.show()")
        chart = plot.results[0]
        print(chart.formats())  # incluye 'png' y 'chart'
        with open("grafico.png", "wb") as out:
            out.write(base64.b64decode(chart.png or ""))  # (1)!
    ```

    1. `png` llega en base64, como en Jupyter.

=== "Python (async)"

    ```python
    import asyncio
    import base64

    from rayito import AsyncSandbox


    async def main() -> None:
        async with await AsyncSandbox.create() as sbx:
            plot = await sbx.run_code("import matplotlib.pyplot as plt; plt.plot([1, 2, 3]); plt.show()")
            chart = plot.results[0]
            print(chart.formats())  # incluye 'png' y 'chart'
            with open("grafico.png", "wb") as out:
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
    console.log(chart?.formats()); // incluye "png" y "chart"
    await writeFile("grafico.png", Buffer.from(chart?.png ?? "", "base64"));
    ```

### Salida en streaming

`on_stdout`, `on_stderr`, `on_result` y `on_error` reciben cada salida
mientras la celda corre, sin esperar al final:

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

### Contextos

Cada contexto es un kernel independiente con su propio estado. El contexto
`default` existe siempre; `create_code_context()` crea otros (hasta 8 por
sandbox), con su `cwd`, su lenguaje y sus `envs`.

=== "Python"

    ```python
    from rayito import Sandbox

    with Sandbox.create() as sbx:
        sbx.run_code("x = 1")
        ctx = sbx.create_code_context(cwd="/tmp")
        print(sbx.run_code("x", context=ctx).error)  # NameError: otro kernel
        print([c.id for c in sbx.list_code_contexts()])  # ['default', 'ctx-…']
        sbx.restart_code_context(ctx)  # vacía su estado
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

### Timeouts

`timeout` (300 s por defecto) lo impone el agente: al vencer interrumpe el
kernel y la `Execution` vuelve con `error.name == "ExecutionTimeout"`. El
kernel sigue utilizable en la siguiente celda; si no se recupera en 5 s, el
agente reinicia ese contexto (y su estado se pierde).

## Opciones de `run_code`

| Python | TypeScript | Por defecto | Qué hace |
|---|---|---|---|
| `code` | `code` | — | el código de la celda |
| `language` | `language` | `"python"` | `bash`, `javascript`/`js`, `typescript`/`ts` en `rayito-base-poly` ([Kernels](../kernels.md)) |
| `context` | `context` | `default` | contexto (objeto o id); excluyente con `language` |
| `on_stdout`, `on_stderr` | `onStdout`, `onStderr` | — | cada línea impresa, como `OutputMessage` |
| `on_result`, `on_error` | `onResult`, `onError` | — | cada `Result` y el error |
| `envs` | `envs` | — | variables de entorno sólo durante esta celda (contextos Python) |
| `timeout` | `timeoutMs` | 300 s / 300 000 ms | límite del agente; `None` / `0` sin límite |
| `request_timeout` | `requestTimeoutMs` | 60 s | plazo de la llamada |
| `secrets` | `secrets` | — | [secretos](../secrets.md) en el entorno de la celda (opcional, con coste) |

## Errores y solución de problemas

| Situación | Python | TypeScript | Qué hacer |
|---|---|---|---|
| el código lanza | `execution.error` (no es una excepción) | `execution.error` | lee `error.name`, `error.value` y `error.traceback` |
| la celda tarda más que `timeout` | `error.name == "ExecutionTimeout"` | igual | sube `timeout` |
| `language` y `context` a la vez, `envs` en un kernel no Python | `InvalidArgumentException` | `InvalidArgumentError` | elige uno |
| kernel que la imagen no trae (`bash` en `rayito-base`) | `UnimplementedError` | `UnimplementedError` | usa `rayito-base-poly` |
| más de 8 contextos | `RateLimitException` | `RateLimitError` | borra contextos con `remove_code_context` |
| una celda silenciosa se suspende a mitad | (el SDK se reengancha solo) | igual | sube `max_idle_seconds` ([Pausar y reanudar](pausar-reanudar.md)) |

## Diferencias con E2B

- Ninguna en la API de Python: `rayito.e2b` usa el mismo `run_code`.
- Lenguajes: Python siempre; bash, JavaScript y TypeScript en
  `rayito-base-poly`; R y Java no existen.

## Ver también

- [Lenguajes y kernels](../kernels.md)
- [Comandos](comandos.md)
- [Servidor MCP](../mcp.md): `run_code` como herramienta de un agente
- Referencia: [Python](../referencia/python/sandbox.md) y
  [TypeScript](../referencia/typescript.md#ejecutar-codigo)

# Referencia

Todos los parámetros, clases y comandos. Si buscas cómo hacer algo, empieza
por las [Guías](../guias/index.md); esta sección es para consultar.

<div class="grid cards" markdown>

-   :material-language-python:{ .lg .middle } **Python**

    ---

    `Sandbox`, `AsyncSandbox`, subclientes, pool, modelos, opcionales,
    excepciones y el shim de E2B, generados desde los docstrings.

    [:octicons-arrow-right-24: Referencia de Python](../api.md)

-   :material-language-typescript:{ .lg .middle } **TypeScript**

    ---

    Las mismas clases en `camelCase` y milisegundos, con la tabla de
    equivalencias.

    [:octicons-arrow-right-24: Referencia de TypeScript](typescript.md)

-   :material-console:{ .lg .middle } **CLI**

    ---

    `rayito image`, `rayito sandbox`, `rayito doctor`.

    [:octicons-arrow-right-24: CLI](../cli.md)

-   :material-alert-circle:{ .lg .middle } **Errores**

    ---

    Cada excepción en Python y TypeScript, cuándo ocurre y qué hacer.

    [:octicons-arrow-right-24: Errores](errores.md)

</div>

Además: [Variables de entorno](variables-de-entorno.md),
[Límites](../limits.md), [Otros lenguajes (gRPC)](otros-lenguajes.md) y el
[Changelog](changelog.md).

## Python y TypeScript: cómo se corresponden los nombres

Los dos SDK tienen la misma superficie. Tres reglas convierten un nombre en
el otro:

| Regla | Python | TypeScript |
|---|---|---|
| `snake_case` → `camelCase` | `run_code`, `get_info`, `write_files`, `sandbox_id` | `runCode`, `getInfo`, `writeFiles`, `sandboxId` |
| segundos → milisegundos, con sufijo `Ms` | `timeout=60`, `request_timeout=30` | `timeoutMs: 60_000`, `requestTimeoutMs: 30_000` |
| argumentos con nombre → un objeto de opciones | `commands.run("ls", background=True, cwd="/tmp")` | `commands.run("ls", { background: true, cwd: "/tmp" })` |

Excepciones a la regla de los milisegundos: los campos que la API de AWS da
en segundos se quedan en segundos en los dos SDK (`IdlePolicy.max_idle_seconds`
/ `idle.maxIdleSeconds`, `expires_in` / `expiresIn` de las URLs de S3).

| Concepto | Python | TypeScript |
|---|---|---|
| Liberar al salir | `with Sandbox.create() as sbx:` | `await using sbx = await Sandbox.create();` |
| Sin límite de tiempo | `timeout=None` | `timeoutMs: 0` |
| Sin auto-suspensión | `idle=None` | `idle: null` |
| Iterar un listado | `for item in Sandbox.list():` | `for await (const item of Sandbox.list())` |
| Excepciones | `...Exception` | `...Error` |
| Cancelar una llamada | `request_timeout=` | `signal: AbortSignal` (además de `requestTimeoutMs`) |

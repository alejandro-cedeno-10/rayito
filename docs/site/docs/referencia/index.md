# Referencia

Todos los parámetros, clases y comandos. Si buscas cómo hacer algo, empieza
por las [Guías](../guias/index.md); esta sección es para consultar.

<div class="grid cards" markdown>

-   :material-language-python:{ .lg .middle } **Python**

    ---

    `Sandbox`, `AsyncSandbox`, subclientes (`commands`, `files`, `pty`,
    `git`, `agent`…), pool, modelos, opcionales, excepciones y el shim de
    E2B, generados desde los docstrings.

    [:octicons-arrow-right-24: Referencia de Python](../api.md)

-   :material-language-typescript:{ .lg .middle } **TypeScript**

    ---

    Las mismas clases en `camelCase` y milisegundos, con la tabla de
    equivalencias.

    [:octicons-arrow-right-24: Referencia de TypeScript](typescript.md)

-   :material-console:{ .lg .middle } **CLI**

    ---

    `rayito doctor`, `image`, `sandbox`, `stack`, `events`, `template`,
    `domain` y `agent`.

    [:octicons-arrow-right-24: CLI](../cli.md)

-   :material-alert-circle:{ .lg .middle } **Errores**

    ---

    Cada excepción en Python y TypeScript, cuándo ocurre y qué hacer.

    [:octicons-arrow-right-24: Errores](errores.md)

</div>

Todas las páginas de la sección:

| Página | Qué contiene |
|---|---|
| [Python: resumen](../api.md) | qué se importa de `rayito` y de `rayito.e2b`, y por dónde empezar |
| [Python: Sandbox](python/sandbox.md) | `Sandbox` y `AsyncSandbox`: crear, conectar, ciclo de vida, red, persistencia |
| [Python: Subclientes](python/subclientes.md) | `commands`, `files`, `pty`, `git` y `agent`, con sus handles |
| [Python: Pool](python/pool.md) | `SandboxPool`, `AsyncSandboxPool`, `PoolConfig` y los backends |
| [Python: Modelos](python/modelos.md) | los tipos de datos que devuelven y aceptan las llamadas |
| [Python: Opcionales](python/opcionales.md) | las clases de las funciones opcionales (secretos, montajes, eventos, templates…) |
| [Python: Excepciones](python/excepciones.md) | la jerarquía de excepciones |
| [Python: Shim E2B](python/shim-e2b.md) | `rayito.e2b` |
| [TypeScript](typescript.md) | clases y métodos públicos del paquete npm y de `rayito/e2b` |
| [CLI](../cli.md) | cada orden de `rayito` y sus opciones |
| [Errores](errores.md) | cada error en Python y TypeScript, cuándo ocurre y qué hacer |
| [Variables de entorno](variables-de-entorno.md) | `RAYITO_*` y las variables de AWS que leen el SDK y la CLI |
| [Límites](../limits.md) | límites de la plataforma, versionado y compatibilidad SDK ↔ `rayd` |
| [Otros lenguajes (gRPC)](otros-lenguajes.md) | hablar con `rayd` desde un lenguaje sin SDK oficial |
| [Changelog](changelog.md) | los cambios de cada versión |

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

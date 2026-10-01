---
title: Rayito
description: Sandboxes para agentes de IA, compatibles con E2B, dentro de tu propia cuenta de AWS.
---

# Rayito

**Sandboxes para agentes de IA, compatibles con E2B, dentro de tu propia
cuenta de AWS.** Cada sandbox es un MicroVM de AWS Lambda aislado por
hardware. Dentro corre `rayd`, un agente escrito en Rust. No hay servidor de
terceros ni API key: el SDK llama directamente a la API de AWS con tus
credenciales.

> Sandboxes que aparecen en un destello. Dentro de tu propia cuenta de AWS.

[Empezar en 5 minutos](primeros-pasos/index.md){ .md-button .md-button--primary }
[Migrar desde E2B](migrar-desde-e2b/index.md){ .md-button }

## Empieza en tres pasos

**1. Instala el SDK** (Python ≥ 3.11 o Node ≥ 20).

=== "Python"

    ```bash
    pip install "rayito[cli]"      # o: uv add "rayito[cli]"
    ```

=== "TypeScript"

    ```bash
    pnpm add rayito                # o: npm i rayito / yarn add rayito / bun add rayito
    pip install "rayito[cli]"      # la CLI (para el diagnóstico) es Python
    ```

**2. Comprueba tu cuenta.** Con tus credenciales de AWS y una imagen
`rayito-base` publicada ([Configurar AWS](primeros-pasos/configurar-aws.md)),
el diagnóstico dice qué falta antes del primer sandbox:

```bash
export AWS_PROFILE=<tu-perfil> AWS_REGION=us-east-1 RAYITO_TEMPLATE=rayito-base
rayito doctor
```

**3. Crea tu primer sandbox.**

=== "Python"

    ```python
    from rayito import Sandbox

    with Sandbox.create() as sbx:  # (1)!
        print(sbx.commands.run("echo hola").stdout)  # "hola\n"
        print(sbx.run_code("x = 40; x + 2").text)  # "42"
    ```

    1. `with` mata el sandbox al salir: un sandbox olvidado factura hasta su
       `timeout`.

=== "TypeScript"

    ```ts
    import { Sandbox } from "rayito";

    await using sbx = await Sandbox.create(); // (1)!
    console.log((await sbx.commands.run("echo hola")).stdout); // "hola\n"
    console.log((await sbx.runCode("x = 40; x + 2")).text); // "42"
    ```

    1. `await using` mata el sandbox al salir del bloque.

Siguiente paso: [Primer sandbox](quickstart.md) recorre comandos, ficheros,
código y reconexión en Python, Python async y TypeScript.

## Explora la documentación

<div class="grid cards" markdown>

-   :material-rocket-launch:{ .lg .middle } **Primeros pasos**

    ---

    Instala el SDK, prepara tu cuenta de AWS y lanza tu primer sandbox.

    [:octicons-arrow-right-24: Empezar](primeros-pasos/index.md)

-   :material-book-open-variant:{ .lg .middle } **Guías**

    ---

    Una página por función: comandos, código, ficheros, terminal, red,
    pausa, pool, persistencia y más.

    [:octicons-arrow-right-24: Ver las guías](guias/index.md)

-   :material-swap-horizontal:{ .lg .middle } **Migrar desde E2B**

    ---

    Cambia un import y sigue con tu código. Qué cambia, qué no y qué no
    existe en Lambda MicroVMs.

    [:octicons-arrow-right-24: Migrar](migrar-desde-e2b/index.md)

-   :material-api:{ .lg .middle } **Referencia**

    ---

    API de Python y TypeScript, CLI, errores, variables de entorno y
    límites.

    [:octicons-arrow-right-24: Referencia](referencia/index.md)

-   :material-cash:{ .lg .middle } **Costes**

    ---

    Lo que cuesta cada operación, medido en AWS real. Las funciones con
    coste propio están apagadas por defecto.

    [:octicons-arrow-right-24: Modelo de costes](cost.md)

-   :material-shield-lock:{ .lg .middle } **Seguridad**

    ---

    Qué protege el SDK por defecto, qué no poner en `envs` y el modelo de
    amenazas.

    [:octicons-arrow-right-24: Seguridad](security.md)

</div>

## Por qué Rayito

- **El código no sale de tu cuenta.** Los sandboxes corren en tu cuenta de
  AWS y facturan en tu factura de AWS. Nadie más ve los datos.
- **Sin nada que operar.** No hay clúster, plano de control ni servidor de
  Rayito. El SDK habla con la API de Lambda MicroVMs y con `rayd`, el agente
  del MicroVM.
- **La API de E2B.** `rayito.e2b` (Python) y `rayito/e2b` (TypeScript)
  aceptan el código escrito para E2B 2.x cambiando sólo el import. Lo que la
  plataforma no puede hacer lanza `UnimplementedError`: nada se aproxima en
  silencio.
- **Nunca cobra por sorpresa.** Toda función que gasta dinero de AWS además
  del propio sandbox (secretos, índice de metadatos) está apagada por
  defecto y se activa con una opción explícita
  ([Funciones opcionales](optional-features.md)).

## Qué incluye

| Superficie | Qué hace | Guía |
|---|---|---|
| `Sandbox.create / connect / kill / list / get_info` | ciclo de vida del MicroVM, metadatos y plazos | [Ciclo de vida](guias/ciclo-de-vida.md) |
| `pause()` / `resume()`, `IdlePolicy` | suspender y reanudar con todo el estado vivo | [Pausar y reanudar](guias/pausar-reanudar.md) |
| `timeout`, `max_lifetime`, `on_timeout`, `set_timeout()` | un plazo que `rayd` impone aunque tu proceso muera | [Plazo del servidor](lifecycle.md) |
| `sbx.commands` | procesos en primer y segundo plano, `stdin`, `kill`, reconexión | [Comandos](guias/comandos.md) |
| `sbx.run_code` | kernel Jupyter con estado, resultados, logs, errores y gráficos | [Ejecutar código](guias/ejecutar-codigo.md) |
| `run_code(language="bash" / "javascript" / "typescript")` | kernels bash y Deno en `rayito-base-poly` | [Lenguajes y kernels](kernels.md) |
| `sbx.pty` | terminales reales con `resize` y reconexión | [Terminal (PTY)](guias/terminal-pty.md) |
| `sbx.files` | leer, escribir, listar y vigilar; ficheros grandes y URLs firmadas por S3 | [Ficheros y S3](files.md) |
| `sbx.get_host(port)` | acceso HTTP a un puerto del sandbox | [Puertos y host](guias/puertos-y-host.md) |
| `network=`, `allow_internet_access=False` | política de salida a internet de E2B en `rayito-base-caps` | [Red saliente](network.md) |
| `SandboxPool` | sandboxes en menos de un segundo desde un pool de suspendidos | [Pool](pool.md) |
| `persist=`, `reincarnate()` | el `HOME` en S3, más allá de las 8 h | [Persistencia](persistence.md) |
| `get_metrics_history()`, `Sandbox.paginate()` | historial de métricas y listado reanudable | [Métricas y listado](observability.md) |
| `sbx.git` | la API git de E2B | [Git](git.md) |
| `rayito.e2b`, `rayito/e2b` | el SDK de E2B 2.x cambiando sólo el import | [Migrar desde E2B](migrar-desde-e2b/index.md) |
| `rayito-mcp` | un sandbox para Claude Code, Cursor o VS Code por MCP | [Servidor MCP](mcp.md) |
| `rayito` (CLI) | publicar la imagen, diagnosticar la cuenta, operar sandboxes | [CLI](cli.md) |
| `secrets=`, `SecretStore` (opcional) | secretos de AWS Secrets Manager como variables de entorno | [Secretos](secrets.md) |
| `index=DynamoDbIndex(...)` (opcional) | filtrar por metadatos también sandboxes en pausa | [Índice de metadatos](funciones-opcionales/indice-de-metadatos.md) |
| `tracer_provider=` (opcional) | spans OpenTelemetry del lado del SDK | [OpenTelemetry](funciones-opcionales/opentelemetry.md) |

Todo funciona igual en Python (sync y `asyncio`) y en TypeScript, con
`snake_case` y segundos en Python y `camelCase` y milisegundos en TypeScript.
Los números de [costes](cost.md) y [límites](limits.md) están medidos contra
AWS real, nunca contra un simulador.

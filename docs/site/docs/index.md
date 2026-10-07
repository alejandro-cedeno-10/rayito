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

[Empezar (≈ 15 min)](primeros-pasos/index.md){ .md-button .md-button--primary }
[Migrar desde E2B](migrar-desde-e2b/index.md){ .md-button }

!!! tip "Nuevo en 0.9"
    Una release de limpieza: otro proceso puede usar el agente de un
    sandbox con `Sandbox.connect()` (recupera `sbx.gateways`), el SDK de
    Python honra `AWS_REGION`, y se retiran la opción D del arranque rápido
    del agente y las excepciones de la pasarela que nada lanzaba. Tiene
    cambios que rompen, con su migración.
    [Novedades de 0.9.0](novedades/0.9.0.md) ·
    [0.8.0](novedades/0.8.0.md) ·
    [0.7.x](novedades/0.7.1.md)

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
rayito doctor --template "$RAYITO_TEMPLATE"
```

`RAYITO_TEMPLATE` es la imagen que usará `Sandbox.create()`; `rayito doctor`
no la lee, así que pásasela con `--template` (sin él comprueba
`rayito-base`).

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

=== "Shim E2B"

    ```python
    from rayito.e2b import Sandbox  # antes: from e2b_code_interpreter import Sandbox

    with Sandbox.create(timeout=300) as sbx:
        print(sbx.commands.run("echo hola").stdout)  # "hola\n"
        print(sbx.run_code("x = 40; x + 2").text)  # "42"
    ```

    El código escrito para E2B 2.x, cambiando sólo el import
    ([Migrar desde E2B](migrar-desde-e2b/index.md)).

Guarda el código en `primer.py` y ejecútalo con `python primer.py`, o en
`primer.ts` y ejecútalo con `npx tsx primer.ts` (TypeScript necesita
`"type": "module"` y un `tsconfig.json`: ver
[Ejecutar los ejemplos](primeros-pasos/instalacion.md#ejecutar-los-ejemplos)).

Siguiente paso: [Primer sandbox](quickstart.md) recorre comandos, ficheros,
código y reconexión en Python, Python async y TypeScript.

## Un agente de código dentro del sandbox

`sbx.agent` corre un agente de código ([OpenCode](https://github.com/anomalyco/opencode)
o deepagents) dentro del propio sandbox. El agente llama a su modelo
(Bedrock, Anthropic o un endpoint compatible con OpenAI) sólo a través de
la [pasarela de secretos](funciones-opcionales/pasarela-de-secretos.md):
usa la credencial, pero nunca puede leerla. Basta un `Sandbox.create()`
normal sobre la imagen `rayito-agent`
([Templates de agente](funciones-opcionales/templates-de-agente.md)):

=== "Python"

    ```python
    from rayito import AgentModel, AgentSpec, Sandbox, SecretStore, bedrock_gateway

    MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    SecretStore().create("bedrock-key", "Bearer <clave de Bedrock de corta duración>")
    spec = AgentSpec(model=AgentModel(provider="bedrock", id=MODEL_ID, gateway="bedrock", region="us-east-1"))

    with Sandbox.create(
        template="rayito-agent",
        allow_internet_access=False,
        gateways={"bedrock": bedrock_gateway("bedrock-key", region="us-east-1", models=[MODEL_ID])},
    ) as sbx:
        result = sbx.agent.run("Lista los ficheros de /home/user y resume qué hay.", spec=spec)
        print(result.text, result.usage.total)
    ```

=== "TypeScript"

    ```ts
    import { AgentModel, AgentSpec, Sandbox, SecretStore, bedrockGateway } from "rayito";

    const MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0";
    await new SecretStore().create("bedrock-key", "Bearer <clave de Bedrock de corta duración>");
    const spec = new AgentSpec({
      model: new AgentModel({ provider: "bedrock", id: MODEL_ID, gateway: "bedrock", region: "us-east-1" }),
    });

    await using sbx = await Sandbox.create({
      template: "rayito-agent",
      allowInternetAccess: false,
      gateways: { bedrock: bedrockGateway("bedrock-key", { region: "us-east-1", models: [MODEL_ID] }) },
    });
    const result = await sbx.agent.run("Lista los ficheros de /home/user y resume qué hay.", { spec });
    console.log(result.text, result.usage.total);
    ```

Arrancar más rápido (un pool con `warmup`) es
opcional: cuándo compensa cada opción, en
[¿Qué uso?](guias/agente-en-el-sandbox.md#que-uso). Lo que cuesta la VM
frente a los tokens del modelo:
[Precios](cost.md#coste-de-un-agente-vm-frente-a-modelo).

## Explora la documentación

<div class="grid cards" markdown>

-   :material-rocket-launch:{ .lg .middle } **Primeros pasos**

    ---

    Instala el SDK, prepara tu cuenta de AWS y lanza tu primer sandbox.

    [:octicons-arrow-right-24: Empezar](primeros-pasos/index.md)

-   :material-book-open-variant:{ .lg .middle } **Guías**

    ---

    Una página por función: comandos, código, ficheros, terminal, red,
    pausa, pool, persistencia, el agente dentro del sandbox y más.

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

-   :material-toggle-switch-outline:{ .lg .middle } **Funciones opcionales**

    ---

    Secretos, pasarela de secretos, montajes S3, tamaños, eventos y
    webhooks, OTLP, templates, volúmenes EFS y dominio propio: qué activa
    cada una, qué cuesta y cómo apagarla.

    [:octicons-arrow-right-24: Resumen y coste](optional-features.md)

-   :material-new-box:{ .lg .middle } **Novedades**

    ---

    Qué trae cada versión, cómo actualizar y qué está en desarrollo.

    [:octicons-arrow-right-24: Novedades](novedades/index.md)

-   :material-cash:{ .lg .middle } **Costes**

    ---

    Lo que cuesta cada operación (precio de lista y medido en AWS real), el
    pool y un agente de IA: VM frente a tokens del modelo. Las funciones
    con coste propio están apagadas por defecto.

    [:octicons-arrow-right-24: Precios](cost.md) ·
    [Agente en el sandbox](guias/agente-en-el-sandbox.md)

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
- **Nunca cobra por sorpresa.** Las funciones opcionales que gastan dinero
  de AWS además del propio sandbox (secretos, pasarela de secretos, índice
  de metadatos, eventos y webhooks, exportación OTLP, templates, volúmenes
  EFS, dominio propio…) están apagadas por defecto y
  sólo se activan con una opción explícita del SDK; su infraestructura la
  despliegas tú con `rayito stack deploy`, que imprime el coste antes de
  pedir confirmación ([Funciones opcionales](optional-features.md)). La otra vía con coste
  propio, el bucket de transferencias por S3, también la eliges tú
  (`transfer=` o `RAYITO_TRANSFER_BUCKET`; ver [Ficheros y S3](files.md)).

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
| `sbx.agent` | un agente de código (OpenCode o deepagents) dentro del sandbox, con el modelo tras la pasarela de secretos | [Agente en el sandbox](guias/agente-en-el-sandbox.md) |
| `rayito.e2b`, `rayito/e2b` | el SDK de E2B 2.x cambiando sólo el import | [Migrar desde E2B](migrar-desde-e2b/index.md) |
| `rayito-mcp` | un sandbox para Claude Code, Cursor o VS Code por MCP | [Servidor MCP](mcp.md) |
| LangChain, Vercel AI SDK | Rayito como herramienta de un agente que corre fuera del sandbox | [LangChain y Vercel AI](guias/langchain-y-vercel-ai.md) |
| `rayito` (CLI) | publicar la imagen, diagnosticar la cuenta, operar sandboxes y pilas | [CLI](cli.md) |
| `make local-up`, `make local-e2e` | probar los SDK contra un `rayd` real y un AWS emulado, sin cuenta | [Probar en local](guias/probar-en-local.md) |

Funciones opcionales, todas apagadas por defecto hasta que pasas su opción
(o, en las que son sólo de la CLI, ejecutas su orden):

| Superficie | Qué hace | Guía |
|---|---|---|
| `secrets=`, `SecretStore` | secretos de AWS Secrets Manager como variables de entorno | [Secretos](secrets.md) |
| `gateways=`, `SecretGateway` | usar un secreto desde el sandbox sin poder leerlo | [Pasarela de secretos](funciones-opcionales/pasarela-de-secretos.md) |
| `mounts=`, `S3Mount` | un bucket S3 como carpeta del sandbox | [Montajes S3](funciones-opcionales/montajes-s3.md) |
| `size=` | sandboxes de 512 MiB a 8 GiB desde imágenes por tamaño | [Tamaños](funciones-opcionales/tamanos.md) |
| `events=`, `LifecycleEvents` | eventos de ciclo de vida y webhooks con la firma de E2B | [Eventos y webhooks](funciones-opcionales/eventos-y-webhooks.md) |
| `telemetry=`, `TelemetryExport` | métricas del sandbox por OTLP a CloudWatch | [Exportación OTLP](funciones-opcionales/exportacion-otlp.md) |
| `tracer_provider=` | spans OpenTelemetry del lado del SDK | [OpenTelemetry](funciones-opcionales/opentelemetry.md) |
| `index=DynamoDbIndex(...)` | filtrar por metadatos también sandboxes en pausa | [Índice de metadatos](funciones-opcionales/indice-de-metadatos.md) |
| `Template.build()` | imágenes desde el DSL `Template` de E2B | [Templates](funciones-opcionales/templates.md) |
| `AgentTemplate`, `rayito agent template build` | la imagen `rayito-agent` con OpenCode, ripgrep y deepagents | [Templates de agente](funciones-opcionales/templates-de-agente.md) |
| `PoolConfig(warmup=agent_pool_warmup(...))` | plazas del pool con el agente ya calentado (arranque rápido, opcional) | [Pool](pool.md#calentamiento-warmup) |
| `OptionalStacks`, `rayito stack` | desplegar, consultar y borrar la infraestructura de cada función | [Pilas opcionales](funciones-opcionales/pilas-opcionales.md) |
| `rayito sandbox proxy` | un puerto del sandbox en `http://127.0.0.1` de tu máquina | [Proxy local](funciones-opcionales/proxy-local.md) |
| `volumes=`, `VolumeStore`, `EfsVolumes` (experimental) | un sistema de ficheros EFS compartido en vivo entre sandboxes, en tu VPC | [Volúmenes EFS](funciones-opcionales/volumenes-efs.md) |
| `domain=`, `CustomDomain`, `rayito domain` (experimental) | una URL HTTPS bajo tu dominio para un puerto del sandbox | [Dominio propio](funciones-opcionales/dominio-propio.md) |

Todo funciona igual en Python (sync y `asyncio`) y en TypeScript, con
`snake_case` y segundos en Python y `camelCase` y milisegundos en TypeScript.
Los números de [costes](cost.md) y [límites](limits.md) están medidos contra
AWS real, nunca contra un simulador.

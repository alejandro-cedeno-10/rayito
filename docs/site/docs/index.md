---
title: Rayito
description: Sandboxes para agentes de IA, compatibles con E2B, dentro de tu propia cuenta de AWS.
template: home.html
hide:
  - navigation
  - toc
---

<div class="rx-hero" markdown>
<div class="rx-hero__copy" markdown>

[Nuevo en 0.11: el agente arranca en unos 6 s](novedades/0.11.0.md){ .rx-pill }

# Sandboxes para agentes de IA, en tu propia cuenta de AWS { .rx-hero__title }

Cada sandbox es un MicroVM de AWS Lambda aislado por hardware. El SDK llama
directamente a la API de AWS con tus credenciales: sin servidor de Rayito,
sin API key y con la API de E2B.
{ .rx-hero__lead }

<div class="rx-hero__actions" markdown>
[Empezar](primeros-pasos/index.md){ .md-button .md-button--primary }
[Ver en GitHub](https://github.com/alejandro-cedeno-10/rayito){ .md-button }
</div>

¿Vienes de E2B? [Cambia un import](migrar-desde-e2b/index.md).
{ .rx-hero__note }

</div>
<div class="rx-hero__code" markdown>

<div class="rx-window" aria-hidden="true"><span></span><span></span><span></span></div>

=== "Python"

    ```bash
    pip install rayito
    ```

    ```python
    from rayito import Sandbox

    with Sandbox.create() as sbx:
        print(sbx.commands.run("echo hola").stdout)  # "hola\n"
        print(sbx.run_code("x = 40; x + 2").text)  # "42"
    ```

=== "TypeScript"

    ```bash
    pnpm add rayito
    ```

    ```ts
    import { Sandbox } from "rayito";

    await using sbx = await Sandbox.create();
    console.log((await sbx.commands.run("echo hola")).stdout); // "hola\n"
    console.log((await sbx.runCode("x = 40; x + 2")).text); // "42"
    ```

<p class="rx-hero__foot" markdown>Necesita la imagen `rayito-base` en tu cuenta: [Configurar AWS](primeros-pasos/configurar-aws.md), una vez.</p>

</div>
</div>

<div class="rx-ruler" role="img" aria-label="Tiempos medidos: resume 0,38 s; pool.take 0,77 s; agente listo 2,3 s; kernel listo 5,2 s">
<div class="rx-ruler__track">
<span class="rx-ruler__tick" style="--at:0%"><span>0 s</span></span>
<span class="rx-ruler__tick" style="--at:16.667%"><span>1 s</span></span>
<span class="rx-ruler__tick" style="--at:33.333%"><span>2 s</span></span>
<span class="rx-ruler__tick" style="--at:50%"><span>3 s</span></span>
<span class="rx-ruler__tick" style="--at:66.667%"><span>4 s</span></span>
<span class="rx-ruler__tick" style="--at:83.333%"><span>5 s</span></span>
<span class="rx-ruler__tick" style="--at:100%"><span>6 s</span></span>
<span class="rx-ruler__mark rx-ruler__mark--up" style="--at:6.3%; --i:0"><span class="rx-ruler__value">0,38 s</span><span class="rx-ruler__label"><code>resume()</code></span></span>
<span class="rx-ruler__mark rx-ruler__mark--down" style="--at:12.8%; --i:1"><span class="rx-ruler__value">0,77 s</span><span class="rx-ruler__label"><code>pool.take()</code></span></span>
<span class="rx-ruler__mark rx-ruler__mark--up" style="--at:38.3%; --i:2"><span class="rx-ruler__value">2,3 s</span><span class="rx-ruler__label">agente listo</span></span>
<span class="rx-ruler__mark rx-ruler__mark--down rx-ruler__mark--end" style="--at:86.7%; --i:3"><span class="rx-ruler__value">5,2 s</span><span class="rx-ruler__label">kernel listo</span></span>
</div>
<p class="rx-ruler__note">Medianas medidas en AWS real (us-east-1, 2026-09). Detalle en <a href="cost.md#tiempos">Tiempos</a>.</p>
</div>

<div class="rx-hooks" markdown>

<div class="rx-hook" markdown>

### Un sandbox en segundos

`Sandbox.create()` deja el sandbox listo para comandos en unos 2,3 s y el
kernel de Python en unos 5,2 s. Desde un pool de sandboxes suspendidos, en
menos de un segundo.

[Pool](pool.md) · [Tiempos medidos](cost.md#tiempos)

</div>
<div class="rx-hook" markdown>

### Un agente de código dentro

`sbx.agent.run()` corre OpenCode o deepagents en el propio sandbox. El
modelo (Bedrock, Anthropic, OpenAI y [más](guias/agente-proveedores.md)) se
llama a través de la pasarela de secretos: el agente usa la credencial, pero
no puede leerla.

[Agente en el sandbox](guias/agente-en-el-sandbox.md)

</div>
<div class="rx-hook" markdown>

### De E2B en un import

```diff
- from e2b_code_interpreter import Sandbox
+ from rayito.e2b import Sandbox
```

Lo que Lambda MicroVMs no puede hacer lanza `UnimplementedError`: nada se
aproxima en silencio.

[Migrar desde E2B](migrar-desde-e2b/index.md) · [Paridad, fila a fila](e2b-parity.md)

</div>

</div>

<div class="rx-section" markdown>

## Empieza en tres pasos

<ol class="rx-steps" markdown>
<li class="rx-step" markdown>

<h3>Instala el SDK</h3>

Python ≥ 3.11 o Node ≥ 20. La CLI es Python.

```bash
pip install "rayito[cli]"
```

[Instalación](primeros-pasos/instalacion.md)

</li>
<li class="rx-step" markdown>

<h3>Prepara tu cuenta, una vez</h3>

Unos 15 minutos, casi todos esperando a que AWS construya la imagen: pila de
IAM, un bucket y la imagen `rayito-base`. Luego, el diagnóstico te dice qué
falta.

```bash
export AWS_PROFILE=<tu-perfil> AWS_REGION=us-east-1
rayito doctor --template rayito-base
```

[Configurar AWS](primeros-pasos/configurar-aws.md)

</li>
<li class="rx-step" markdown>

<h3>Lanza tu primer sandbox</h3>

Comandos, ficheros, código y reconexión desde otro proceso, en Python,
Python async y TypeScript.

```bash
python primer.py
```

[Primer sandbox](quickstart.md)

</li>
</ol>

</div>

<div class="rx-section rx-split" markdown>
<div class="rx-split__copy" markdown>

## Un agente de código dentro del sandbox

Basta un `Sandbox.create()` sobre la imagen `rayito-agent`. Con
`allow_internet_access=False` el sandbox sólo llega al modelo, por la
pasarela de secretos. El agente es [OpenCode](https://github.com/anomalyco/opencode)
o deepagents, y el proveedor, el que prefieras (ver
[Proveedores del agente](guias/agente-proveedores.md)).

[Templates de agente](funciones-opcionales/templates-de-agente.md) ·
[¿Arranque rápido? Qué uso](guias/agente-en-el-sandbox.md#que-uso) ·
[VM frente a tokens](cost.md#coste-de-un-agente-vm-frente-a-modelo)

</div>
<div class="rx-split__code" markdown>

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

</div>
</div>

<div class="rx-section" markdown>

## Lo que viene incluido

<div class="rx-features" markdown>

<div class="rx-feature" markdown>
:material-pause-circle-outline:{ .lg .middle } [**Pausar y reanudar**](guias/pausar-reanudar.md)

Congela el sandbox con su estado y vuelve en 0,38 s.
</div>
<div class="rx-feature" markdown>
:material-layers-outline:{ .lg .middle } [**Pool**](pool.md)

Sandboxes suspendidos listos en menos de un segundo.
</div>
<div class="rx-feature" markdown>
:material-database-outline:{ .lg .middle } [**Persistencia**](persistence.md)

El `HOME` en S3, más allá de las 8 h.
</div>
<div class="rx-feature" markdown>
:material-shield-off-outline:{ .lg .middle } [**Red saliente**](network.md)

Corta o filtra la salida a internet.
</div>
<div class="rx-feature" markdown>
:material-server-network:{ .lg .middle } [**Servidor MCP**](mcp.md)

Un sandbox para tu editor o asistente por MCP.
</div>
<div class="rx-feature" markdown>
:material-link-variant:{ .lg .middle } [**LangChain y Vercel AI**](guias/langchain-y-vercel-ai.md)

Rayito como herramienta de tu agente.
</div>
<div class="rx-feature" markdown>
:material-cube-outline:{ .lg .middle } [**Templates**](funciones-opcionales/templates.md)

Imágenes propias con el DSL `Template` de E2B.
</div>
<div class="rx-feature" markdown>
:material-chart-line:{ .lg .middle } [**Exportación OTLP**](funciones-opcionales/exportacion-otlp.md)

Métricas del sandbox por OTLP a CloudWatch.
</div>

</div>

</div>

<div class="rx-section rx-split" markdown>
<div class="rx-split__copy" markdown>

## Migra desde E2B en minutos

`rayito.e2b` y `rayito/e2b` aceptan el código escrito para E2B 2.x. Cambia
el import, prepara tu cuenta y ejecuta.

- Mismas clases y métodos: `Sandbox`, `commands`, `files`, `run_code`.
- Lo que no existe en Lambda MicroVMs lanza `UnimplementedError` con enlace
  a la explicación.
- La [tabla de paridad](e2b-parity.md) lo dice fila a fila.

[Migrar desde E2B](migrar-desde-e2b/index.md){ .md-button }

</div>
<div class="rx-split__code" markdown>

=== "Python"

    ```diff
    - from e2b_code_interpreter import Sandbox
    + from rayito.e2b import Sandbox
    ```

=== "TypeScript"

    ```diff
    - import { Sandbox } from "@e2b/code-interpreter";
    + import { Sandbox } from "rayito/e2b";
    ```

</div>
</div>

<div class="rx-band" markdown>

## Tu cuenta, tu factura

Los sandboxes corren en tu cuenta de AWS y se pagan en tu factura de AWS. No
hay servidor de Rayito, ni suscripción, ni API key. Las funciones con coste
propio están apagadas hasta que pasas su opción, y `rayito stack deploy` te
enseña el coste antes de pedir confirmación.
{ .rx-section__lead }

<div class="rx-band__figures" markdown>
<p class="rx-figure"><strong>≈ $0,126/h</strong><span>un sandbox de 2 GB mientras corre</span></p>
<p class="rx-figure"><strong>≈ $0,022</strong><span>diez minutos, con el lanzamiento</span></p>
<p class="rx-figure"><strong>≈ $0,60/mes</strong><span>una plaza de pool suspendida</span></p>
<p class="rx-figure"><strong>$0</strong><span>lo que cobra Rayito</span></p>
</div>

us-east-1, precios de lista consultados el 2026-10-06.
[Precios](cost.md) · [Funciones opcionales y su coste](optional-features.md)

</div>

<div class="rx-section" markdown>

## Explora el manual

<div class="grid cards rx-cards" markdown>

-   :material-rocket-launch:{ .lg .middle } **[Primeros pasos](primeros-pasos/index.md)**

    ---

    Instala el SDK, prepara tu cuenta de AWS y lanza tu primer sandbox.

-   :material-book-open-variant:{ .lg .middle } **[Guías](guias/index.md)**

    ---

    Una página por función: comandos, código, ficheros, red, pausa, pool y
    persistencia.

-   :material-robot-outline:{ .lg .middle } **[Agentes](guias/agente-en-el-sandbox.md)**

    ---

    El agente dentro del sandbox, el [servidor MCP](mcp.md) y las
    integraciones con LangChain y Vercel AI.

-   :material-toggle-switch-outline:{ .lg .middle } **[Funciones opcionales](optional-features.md)**

    ---

    Qué activa cada una, qué cuesta y cómo apagarla.

-   :material-swap-horizontal:{ .lg .middle } **[Migrar desde E2B](migrar-desde-e2b/index.md)**

    ---

    Qué cambia, qué no y qué no existe en Lambda MicroVMs.

-   :material-api:{ .lg .middle } **[Referencia](referencia/index.md)**

    ---

    API de Python y TypeScript, CLI, errores, variables de entorno y límites.

-   :material-server-security:{ .lg .middle } **[Operación](operacion/index.md)**

    ---

    Imágenes, IAM, seguridad, precios y solución de problemas.

-   :material-new-box:{ .lg .middle } **[Novedades](novedades/index.md)**

    ---

    Qué trae cada versión y cómo actualizar.

</div>

</div>

<div class="rx-incluye" markdown>

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

</div>

<div class="rx-cta" markdown>

## Tu primer sandbox, en tres pasos

[Empezar](primeros-pasos/index.md){ .md-button .md-button--primary }
[Ver en GitHub](https://github.com/alejandro-cedeno-10/rayito){ .md-button }

</div>

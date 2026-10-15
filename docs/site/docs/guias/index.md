# Guías

Una página por función. Cada guía empieza con un ejemplo corto en Python,
Python async y TypeScript (las pestañas se sincronizan: elige tu lenguaje una
vez), explica las opciones y termina con los errores habituales y las
diferencias con E2B.

<div class="grid cards rx-cards" markdown>

-   :material-cube-outline:{ .lg .middle } **[Sandbox](#sandbox)**

    ---

    Ciclo de vida, pausa, pool y persistencia.

-   :material-play-circle-outline:{ .lg .middle } **[Ejecutar](#ejecutar)**

    ---

    Comandos, código, kernels y terminal.

-   :material-folder-network-outline:{ .lg .middle } **[Ficheros y red](#ficheros-y-red)**

    ---

    Ficheros, S3, salida a internet y puertos.

-   :material-chart-line:{ .lg .middle } **[Observar y herramientas](#observar-y-herramientas)**

    ---

    Métricas, listado y git.

-   :material-robot-outline:{ .lg .middle } **[Agentes](#agentes)**

    ---

    MCP, LangChain y el agente en el sandbox.

-   :material-flask-outline:{ .lg .middle } **[Desarrollar y probar](#desarrollar-y-probar)**

    ---

    Probar en local, sin cuenta de AWS.

</div>

## Sandbox

| Guía | Para qué |
|---|---|
| [Ciclo de vida](ciclo-de-vida.md) | crear, conectar, listar, inspeccionar y destruir sandboxes; plantillas y metadatos |
| [Pausar y reanudar](pausar-reanudar.md) | congelar un sandbox con todo su estado y despertarlo; la auto-suspensión por inactividad |
| [Plazo del servidor](../lifecycle.md) | un plazo que el sandbox cumple aunque tu proceso muera; `set_timeout()` |
| [Pool](../pool.md) | sandboxes listos en menos de un segundo |
| [Persistencia](../persistence.md) | el `HOME` en S3, más allá de las 8 h |
| [Async](async.md) | `AsyncSandbox` en Python y las equivalencias en TypeScript |

## Ejecutar

| Guía | Para qué |
|---|---|
| [Comandos](comandos.md) | procesos en primer y segundo plano, `stdin`, señales y reconexión |
| [Ejecutar código](ejecutar-codigo.md) | celdas de Python con estado, resultados, gráficos, errores y contextos |
| [Lenguajes y kernels](../kernels.md) | bash, JavaScript y TypeScript con Deno |
| [Terminal (PTY)](terminal-pty.md) | una terminal interactiva real |

## Ficheros y red

| Guía | Para qué |
|---|---|
| [Ficheros y S3](../files.md) | leer, escribir, listar, vigilar; ficheros grandes y URLs firmadas |
| [Red saliente](../network.md) | cortar o filtrar la salida a internet |
| [Puertos y host](puertos-y-host.md) | llegar por HTTP a un servidor que corre en el sandbox |

## Observar y herramientas

| Guía | Para qué |
|---|---|
| [Métricas y listado](../observability.md) | CPU, memoria y disco; historial; listado paginado |
| [Git](../git.md) | clonar, hacer commit y push desde el sandbox |

## Agentes

¿Dónde corre el agente? **Fuera** (tu host MCP o tu app con LangChain/Vercel
AI): el sandbox es su herramienta y basta `rayito-base`. **Dentro**
(`sbx.agent`): el bucle entero corre en el sandbox; necesita la imagen
`rayito-agent` y la pasarela de secretos.

| Guía | Para qué |
|---|---|
| [Servidor MCP](../mcp.md) | un sandbox para Claude Code, Claude Desktop, Cursor o VS Code |
| [LangChain y Vercel AI](langchain-y-vercel-ai.md) | Rayito como herramienta de un agente |
| [Agente en el sandbox](agente-en-el-sandbox.md) | un agente de código (OpenCode o deepagents) **dentro** del sandbox con `sbx.agent.run()`, el modelo tras la pasarela de secretos; el arranque rápido, opcional |
| [Proveedores del agente](agente-proveedores.md) | los proveedores de modelo que admite `sbx.agent` tras la pasarela de secretos, cómo configurar cada uno y cuáles no se admiten |
| [Templates de agente](../funciones-opcionales/templates-de-agente.md) | `AgentTemplate`: la imagen `rayito-agent` con OpenCode, ripgrep y deepagents, sus pines y el coste de cada versión |

## Desarrollar y probar

| Guía | Para qué |
|---|---|
| [Probar en local (Docker + Floci)](probar-en-local.md) | correr los SDK contra un `rayd` real y un AWS emulado, sin cuenta ni coste |

Las funciones que tienen coste propio en AWS y están apagadas por defecto
(secretos, pasarela de secretos, montajes S3, tamaños, eventos y webhooks,
exportación OTLP, OpenTelemetry, índice de metadatos, templates y templates
de agente, pilas opcionales, volúmenes EFS y dominio propio, estos dos
experimentales) o que son sólo de la CLI (proxy local) están en
[Funciones opcionales](../optional-features.md). Lo nuevo de cada versión:
[Novedades](../novedades/index.md).

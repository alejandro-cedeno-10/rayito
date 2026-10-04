# Guías

Una página por función. Cada guía empieza con un ejemplo corto en Python,
Python async y TypeScript (las pestañas se sincronizan: elige tu lenguaje una
vez), explica las opciones y termina con los errores habituales y las
diferencias con E2B.

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

| Guía | Para qué |
|---|---|
| [Servidor MCP](../mcp.md) | un sandbox para Claude Code, Claude Desktop, Cursor o VS Code |
| [LangChain y Vercel AI](langchain-y-vercel-ai.md) | Rayito como herramienta de un agente |

Las funciones que tienen coste propio en AWS (secretos, índice de metadatos,
montajes S3, tamaños, eventos y webhooks, exportación OTLP, templates,
pasarela de secretos) o que son sólo de la CLI (proxy local) están en
[Funciones opcionales](../optional-features.md). Lo nuevo de cada versión:
[Novedades](../novedades/index.md).

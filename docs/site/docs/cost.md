# Modelo de costes

Rayito no cobra nada: pagas a AWS, en tu factura, lo que consumen tus
sandboxes. Todo lo de esta página está medido contra AWS real (`us-east-1`,
ARM, imagen `rayito-base` de 2 GB / 1 vCPU, septiembre de 2026) o sale de la
lista de precios pública.

## Cuánto cuesta, con ejemplos

| Escenario | Coste aproximado |
|---|---|
| 1 sandbox de 2 GB durante 10 minutos | 600 s × $0,126/h ≈ **$0,021**, más el lanzamiento ($0,0014) ≈ **$0,022** |
| 1 sandbox de 2 GB durante 1 hora | **$0,126** + $0,0014 |
| Un agente con 100 sesiones al día de 5 minutos | 100 × (300 s × $0,126/h + $0,0014) ≈ **$1,19/día** |
| El mismo sandbox pausado 8 horas | almacenamiento del snapshot (≈ 0,92 GB × $0,08/GB-mes durante 8 h) ≈ **$0,0008** + un ciclo suspend/resume ($0,0049) |
| Mantener publicada una versión de imagen | ≈ **$0,04 por semana** (mínimo una semana por versión) |
| `rayito doctor --launch` | ≈ **$0,002** |
| Un pool de 3 plazas ociosas | ≈ 3 × $0,6 = **$1,8/mes** (ver abajo) |

Sin free tier ni cuota de plan: pagas por segundo mientras el sandbox está
`RUNNING`, por GB en cada snapshot y por el almacenamiento de las versiones
de imagen y de los sandboxes suspendidos.

<a id="precios-fuente-aws_api_notesmd-12"></a>

## Precios

| Concepto | Precio |
|---|---|
| Cómputo mientras `RUNNING` | $0,0000276944 por vCPU-s + $0,0000036667 por GB-s ⇒ **$0,126/h** a 2 GB / 1 vCPU |
| Escritura de snapshot (cada `suspend`) | $0,0038 por GB |
| Lectura de snapshot (cada `run-microvm` o `resume`) | $0,00155 por GB |
| Almacenamiento de snapshots (versiones de imagen, sandboxes suspendidos) | $0,08 por GB-mes, **mínimo una semana** por versión de imagen ⇒ ≈ $0,037 por semana y versión |
| Un ciclo suspend + resume a 2 GB (0,92 GB de snapshot escritos y leídos) | ≈ **$0,0049**, lo mismo que ≈ 140 s de cómputo |
| Un lanzamiento (`run-microvm`, lectura del snapshot de 0,92 GB) | ≈ **$0,0014** |

!!! tip "La auto-suspensión y el umbral de 150 s"
    Como un ciclo suspend/resume cuesta lo mismo que ≈ 140 s de cómputo, un
    `max_idle_seconds` por debajo de ≈ 150 s nunca ahorra dinero. El valor
    por defecto (300 s) sí. Para trabajos con huecos cortos, sube
    `max_idle_seconds` o pasa `idle=None`
    ([Pausar y reanudar](guias/pausar-reanudar.md)).

### El pool: almacenamiento frente a coste total por plaza

Una plaza de [pool](pool.md) aparcada (suspendida) paga dos cosas
distintas, y es fácil confundirlas:

| Concepto | Coste por plaza |
|---|---|
| Sólo el almacenamiento del snapshot mientras está aparcada | 0,92 GB × $0,08/GB-mes ≈ **$0,074/mes** |
| Reciclado: cada ≈ 7 h la plaza se relanza y se vuelve a aparcar para que nunca llegue al límite de 8 h | ≈ $0,005 por ciclo ≈ **$0,52/mes** |
| **Total de una plaza ociosa** | ≈ **$0,6/mes** (frente a ≈ $91/mes de un sandbox `RUNNING` todo el mes) |
| Tomar una plaza | $0,0014 (lectura del snapshot) + el cómputo normal mientras la usas |

<a id="tiempos-fuente-docsbenchmarks2026-09-cold-startmd-milestonesmd-aws_api_notesmd-16"></a>

## Tiempos

| Operación | Medido |
|---|---|
| `create()` hasta el agente listo (`agent_ready`), cliente a ≈ 90 ms | **p50 2,3 s / p95 3,0 s** |
| `create()` hasta el kernel listo (`kernel_ready`) | **p50 5,2 s / p95 6,1 s** secuencial; p50 5,7 s / p95 8,6 s en ráfaga de 20 |
| `pool.take()` hasta la primera celda | **p50 0,77 s / p95 0,90 s** |
| `pause()` hasta `SUSPENDED` | **1,37–1,49 s** |
| `resume()` explícito | **p50 0,38 s / p95 0,40 s** |
| Auto-resume: primera llamada sobre un sandbox suspendido | **≈ 0,67 s** |
| `files.write` a 2 GB por el proxy | **0,65 MB/s** |
| `files.read` a 2 GB por el proxy | **6,71 MB/s** |
| Ficheros grandes por S3 (con `transfer=`) | **55–106 MB/s** dentro del VM |
| `Sandbox.get_info(id)` con metadatos | **0,61–0,66 s** |
| `Sandbox.list(metadata=...)` sin índice | **≈ 0,6–0,8 s por sandbox `RUNNING`** |
| `get_metrics()` | ≈ 100 ms |

## Reglas prácticas

- Todo lo de esta página es lo que **crea o llama el propio `create()` /
  `connect()`**. Las funciones opcionales (secretos, índice de metadatos,
  montajes S3, tamaños, eventos y webhooks, exportación OTLP, templates y
  pasarela de secretos) tienen su propio coste, apagado por defecto y
  activado sólo con una opción explícita del SDK:
  [Funciones opcionales](optional-features.md).
- Un sandbox olvidado factura hasta su `timeout` (3600 s por defecto): usa
  `with` / `await using` o `kill()`, y un `timeout` acorde a la tarea. Con el
  [plazo del servidor](lifecycle.md) y `on_timeout="kill"`, un sandbox
  huérfano factura hasta su plazo más ≈ 15 s, no hasta `max_lifetime`.
- Los ficheros grandes con `transfer=S3Staging(...)` van por S3, mucho más
  rápido que por el proxy: menos segundos de cómputo facturados. S3 cobra
  sus peticiones y el almacenamiento temporal del prefijo de transferencias.
- En sandboxes de segundos, el coste dominante es la lectura del snapshot de
  cada lanzamiento: el tamaño de la imagen importa más que el cómputo.
- `list(metadata=)` sin índice cuenta como tráfico para cada sandbox
  sondeado y le retrasa la auto-suspensión: no lo llames en bucle sobre una
  flota grande.
- Un sandbox más grande cuesta proporcionalmente más por hora
  ([Límites: tamaño](limits.md#tamano-cpuram)).

??? info "Fuentes y mediciones"
    - Precios: `AWS_API_NOTES.md` §12, en
      [GitHub](https://github.com/alejandro-cedeno-10/rayito/blob/main/AWS_API_NOTES.md),
      y la lista de precios pública de Lambda MicroVMs.
    - Ciclo suspend/resume y arranque en frío:
      [`docs/benchmarks/2026-09-cold-start.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/docs/benchmarks/2026-09-cold-start.md)
      (§8 y §9).
    - Pool: `clients/python/tests/e2e/test_m7_pool.py` (2026-09-16).
    - `pause()`, auto-resume, caudal de ficheros, `get_info` y
      `list(metadata=)`: aceptaciones de 0.1.0 (`MILESTONES.md`) y
      `AWS_API_NOTES.md` Q45; S3: Q59; el plazo del servidor: Q58.

## Coste de un agente: VM frente a modelo

!!! warning "Borrador, aún no publicado"
    Esta sección documenta el diseño aceptado de `sbx.agent`
    (`ai-agent-core`/`ai-agent-fast-start`/`ai-agent-deepagents`, sin
    fusionar todavía): [Agente en el sandbox](guias/agente-en-el-sandbox.md).
    Los precios de MicroVMs de esta página ya están publicados; los de
    Bedrock que siguen son de la lista de precios pública, consultada el
    2026-10-06, y todavía no se han medido en una factura real de esta
    cuenta (eso es [Q151](https://github.com/alejandro-cedeno-10/rayito/blob/main/AWS_API_NOTES.md)
    de la etapa de aceptación en AWS).

Un agente dentro del sandbox paga **dos facturas distintas**: la VM (igual
que cualquier sandbox) y el modelo, por tokens, en Bedrock, Anthropic o el
proveedor que uses por la pasarela. Casi siempre, la del modelo es la
mayor con diferencia.

### Precios del modelo (Bedrock, us-east-1, consultado 2026-10-06)

| Modelo | Perfil | Entrada /1M tok | Salida /1M tok | Escritura de caché (5 min) | Lectura de caché |
|---|---|---|---|---|---|
| Claude Haiku 4.5 | Regional | $1,10 | $5,50 | $1,375 | $0,11 |
| Claude Haiku 4.5 | Global | $1,00 | $5,00 | $1,25 | $0,10 |
| Claude Sonnet 4.5 | Regional | $3,30 | $16,50 | $4,125 | $0,33 |
| Claude Sonnet 4.5 | Global | $3,00 | $15,00 | $3,75 | $0,30 |

Fuente: offer `AmazonBedrockFoundationModels` de la API de lista de precios
(`us-east-1`, publicada 2026-09-30) y
[aws.amazon.com/bedrock/pricing](https://aws.amazon.com/bedrock/pricing).
El [caché de prompts](https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-caching.html)
exige al menos 4096 tokens por punto de corte en Haiku (1024 en Sonnet), con
hasta 4 puntos de corte por petición; por debajo del mínimo la petición
funciona igual, simplemente no cachea nada, y `inputTokens` nunca cuenta lo
que ya viene de caché. Con un paso ya sale ≈ 1,25× más caro que sin caché
(la escritura cuesta más que una lectura normal); desde el segundo paso ya
gana: 1,35× acumulado frente a 2× sin caché. OpenCode y el runner de
deepagents la activan por defecto (`prompt_caching=True` en `AgentModel`).

### Ejemplo: una tarea de 10 pasos

5 minutos de VM de 2 GB (≈ $0,0119, igual que cualquier sandbox de esa
duración) frente al modelo para 10 pasos de 15 000 tokens de entrada y 400
de salida con Haiku Regional:

| | Coste del modelo |
|---|---|
| Sin caché | **≈ $0,187** |
| Con caché desde el paso 2 | **≈ $0,083** |
| Global (≈10% más barato) | proporcional a la tabla de arriba |
| Sonnet 4.5 con caché | **≈ $0,25** |

**El modelo cuesta entre 7 y 20 veces más que la VM** en este ejemplo: el
tamaño del sandbox casi nunca es la palanca de coste de un agente; el
número de pasos, el tamaño del contexto y el caché de prompts sí lo son.

### Coste de la VM, con fast-start

| Escenario | Coste aproximado |
|---|---|
| Plaza de pool ociosa sin agente (base) | ≈ **$0,60/mes** (tabla de arriba) |
| Plaza de pool con calentamiento (opción C, [Pool](pool.md#calentamiento-warmup-y-servidor-residente)) | ≈ **$0,78/mes** |
| Plaza de pool con servidor residente (opción D) | ≈ **$0,92/mes** |
| Sandbox pausado 8 h entre turnos | ≈ $0,0008 de almacenamiento + un ciclo suspend/resume |
| Versión de imagen `rayito-agent` | ≈ **$0,056/semana** ([Templates de agente](funciones-opcionales/templates-de-agente.md)) |

??? info "Fuentes y mediciones (agentes)"
    - Diseño y los hechos F1–F14 verificados offline: `design.md` de
      `ai-agent-fast-start`/`ai-agent-core` en
      [`openspec/changes/`](https://github.com/alejandro-cedeno-10/rayito/tree/main/openspec/changes).
    - Spike: [`docs/research/2026-10-agent-spike.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/docs/research/2026-10-agent-spike.md).
    - Precios de MicroVMs: igual que el resto de esta página, `AWS_API_NOTES.md` §12.
    - Precios de Bedrock: offer `AmazonBedrockFoundationModels`,
      [aws.amazon.com/bedrock/pricing](https://aws.amazon.com/bedrock/pricing),
      consultados 2026-10-06.
    - Caché de prompts:
      [docs.aws.amazon.com/bedrock/.../prompt-caching.html](https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-caching.html).
    - La medición contra una factura real de esta cuenta queda pendiente de
      la etapa de aceptación en AWS (Q146–Q152 de `AWS_API_NOTES.md` §16).

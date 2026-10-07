---
title: Precios (MicroVMs, pool, agentes)
---

# Precios: MicroVMs, pool y agentes

Rayito no cobra nada: pagas a AWS, en tu factura, lo que consumen tus
sandboxes (y, si usas un agente, los tokens del modelo). Cada número de esta
página lleva una de dos etiquetas:

- **List** (precio de lista): sale de la lista de precios pública de AWS
  (`us-east-1`, ARM), consultada el **2026-10-06**, o es aritmética sobre
  ella.
- **Measured** (medido): contrastado contra AWS real o Cost Explorer en la
  cuenta de pruebas (imagen `rayito-base` de 2 GB / 1 vCPU, septiembre de
  2026).

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

### Componentes del precio

Cada MicroVM factura por cinco cosas, cada una por separado — ninguna
incluye las otras, y es fácil confundirlas al sumar un escenario:

- **Cómputo** mientras el sandbox está `RUNNING`: por vCPU-segundo y por
  GB-segundo, con los *bursts* por encima de tu tamaño base facturados
  también (no hay un tope "incluido"). *List.*
- **Lectura de snapshot**: cada `run-microvm` (lanzamiento) y cada
  `resume` leen el snapshot completo del disco y la memoria. *List.*
- **Escritura de snapshot**: cada `suspend` escribe ese mismo snapshot.
  *List.*
- **Almacenamiento de snapshots**: por GB-hora mientras existan — una
  versión de imagen publicada o un sandbox suspendido — con un
  **mínimo de una semana** por versión de imagen, aunque la borres antes.
  *List.*
- **Transferencia de datos**: tarifas estándar de transferencia de datos
  de AWS (misma región: gratis; a Internet: por GB). *List.*

!!! note "`maximumDuration` no es un precio"
    Un sandbox no puede vivir, lanzado + suspendido, más de **8 horas** sin
    terminarse; el tiempo suspendido cuenta igual que el `RUNNING`. Es la
    razón por la que [el pool](pool.md) relanza y vuelve a aparcar cada
    plaza a las ≈ 7 h, antes de tocar el límite. *List (límite).*

| Concepto | Precio | Fuente |
|---|---|---|
| Cómputo mientras `RUNNING` | $0,0000276944 por vCPU-s + $0,0000036667 por GB-s ⇒ **$0,126/h** a 2 GB / 1 vCPU | List — AWSLambda price-list offer, 2026-10-01 |
| Escritura de snapshot (cada `suspend`) | $0,0037977138 por GB | List — AWSLambda price-list offer; **Measured** −0,06 % en Cost Explorer (2026-09-16) |
| Lectura de snapshot (cada `run-microvm` o `resume`) | $0,0015467699 por GB | List — AWSLambda price-list offer; **Measured** −0,21 % en Cost Explorer (2026-09-16) |
| Almacenamiento de snapshots (versiones de imagen, sandboxes suspendidos) | $0,0001111111 por GB-hora = $0,08 por GB-mes a 720 h/mes, **mínimo una semana** por versión de imagen ⇒ ≈ $0,037 por semana y versión | List — AWSLambda price-list offer; **Measured** exacto en Cost Explorer (2026-09-16) |
| Un ciclo suspend + resume a 2 GB (0,92 GB de snapshot escritos y leídos) | ≈ **$0,0049**, lo mismo que ≈ 140 s de cómputo | Derivado — List |
| Un lanzamiento (`run-microvm`, lectura del snapshot de 0,92 GB) | ≈ **$0,0014** | Derivado — List |

Los precios de cómputo y de almacenamiento coinciden exactos con lo
facturado (Cost Explorer); lectura y escritura de snapshot difieren del
precio de lista en menos de un 0,25 % — dentro del redondeo de la
cantidad facturada, no un precio distinto. La relación entre GB-s y vCPU-s
facturados en la cuenta de pruebas fue **1,87–1,89**, no exactamente 2,0
(pendiente de contrastar con los VM-segundos del banco de pruebas).

!!! info "Referencias oficiales (consultadas 2026-10-06)"
    - [aws.amazon.com/lambda/pricing](https://aws.amazon.com/lambda/pricing/)
      (sección Lambda MicroVMs).
    - Offer `AWSLambda` de la API de lista de precios de AWS, publicado
      2026-10-01.
    - [docs.aws.amazon.com/.../microvms-images-snapshots.html](https://docs.aws.amazon.com/lambda/latest/dg/microvms-images-snapshots.html)
      (semántica y mínimo de almacenamiento de un snapshot).
    - [docs.aws.amazon.com/.../microvms-how-it-works.html](https://docs.aws.amazon.com/lambda/latest/dg/microvms-how-it-works.html)
      (cómputo, lectura/escritura de snapshot por ciclo de vida).
    - [docs.aws.amazon.com/.../gettingstarted-limits.html](https://docs.aws.amazon.com/lambda/latest/dg/gettingstarted-limits.html)
      (`maximumDuration` de 8 h, cuotas por defecto).

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

Un [agente en el sandbox](guias/agente-en-el-sandbox.md) paga **dos
facturas distintas**: la VM (igual que cualquier sandbox) y el modelo, por
tokens, en Bedrock, Anthropic o el proveedor que uses por la pasarela. Casi
siempre la del modelo es, con diferencia, la mayor. Los precios de Bedrock
de esta sección son **List**: aún no se han contrastado con una factura real
(Q151 de `AWS_API_NOTES.md` sigue sin medir); los tokens de los ejemplos
medidos sí son reales. En la factura aparecen como
uso de AWS Marketplace.

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

*List* (precio de lista, hechos aritméticos, no medidos contra una factura
real todavía — ver la advertencia de arriba). 10 pasos, 15 000 tokens de
entrada y 400 de salida por paso, Haiku Regional ($1,10/$5,50/1M, caché
$1,375 escritura / $0,11 lectura por 1M):

- **VM**: 300 s × $0,1261/h + lanzamiento $0,0014 = 300 × 0,1261/3600 +
  0,0014 = $0,0105 + $0,0014 ≈ **$0,0119**.
- **Modelo, sin caché**: 10 pasos × 15 000 tok × $1,10/1M + 10 × 400 tok ×
  $5,50/1M = $0,165 + $0,022 ≈ **$0,187**.
- **Modelo, con caché**: supone que el paso 1 escribe todo el prefijo
  (15 000 tok) y que, de ahí en adelante, cada paso añade ≈ 15 % de
  tokens nuevos al contexto (2 250 escritos a caché) y lee el 85 % restante
  de caché (12 750). Paso 1: 15 000 × $1,375/1M = $0,0206. Pasos 2–10 (9):
  9 × (2 250 × $1,375/1M + 12 750 × $0,11/1M) = 9 × ($0,0031 + $0,0014) =
  $0,0405. Más la salida de los 10 pasos ($0,022) ⇒ $0,0206 + $0,0405 +
  $0,022 ≈ **$0,083**. Sin ese 15 % de tokens nuevos por paso (contexto
  estático), el mismo cálculo da ≈ $0,057 en vez de $0,083 — la cifra
  depende de cuánto crece el contexto en cada paso, no sólo de que haya
  caché.
- **Global** (misma fórmula, ≈10 % más barato por la tabla de precios de
  arriba): proporcional a los números de Regional.
- **Sonnet 4.5 con caché** (misma fórmula y mismo supuesto del 15 %,
  precios de Sonnet Regional): $0,0619 + 9 × ($0,0093 + $0,0042) + $0,066 ≈
  **$0,25**.

**El modelo cuesta entre ≈ 7× y ≈ 21× la VM** en este ejemplo ($0,083 y
$0,25 frente a $0,0119): el tamaño del sandbox casi nunca es la palanca de
coste de un agente; el número de pasos, el tamaño del contexto y cuánto
crece en cada paso sí lo son.

<a id="plaza-de-pool-de-agente-c-y-d"></a>

### Coste de la VM, con fast-start

Medido en AWS real el 2026-10-07 (`AWS_API_NOTES.md` Q146–Q150): los
tiempos y los tokens son **Measured**; el dinero es precio de lista por esos
tiempos (**List**), porque Cost Explorer llega un día tarde.

| Escenario | Coste aproximado |
|---|---|
| Plaza de pool ociosa sin agente (base) | ≈ **$0,60/mes** (tabla de arriba) |
| Plaza de pool con calentamiento (opción C, [Pool](pool.md#calentamiento-warmup-y-servidor-residente)) | ≈ **$0,64/mes** |
| Plaza de pool con servidor residente (opción D, pendiente de la nueva medida) | ≈ **$0,82/mes** |
| Sandbox pausado entre turnos (opción B) | un ciclo suspend/resume de ≈ 0,92–1,2 GB ≈ **$0,005–0,006** + ≈ $0,0001/h guardado |
| Versión de imagen `rayito-agent` | código 2,10 GB + memoria 0,92 GB + disco 0,04 GB ≈ 3,1 GB × $0,08/GB-mes × 7/30 ≈ **$0,057/semana** (mínimo una semana; [Templates de agente](funciones-opcionales/templates-de-agente.md)) |

Coste medido por escenario (VM + modelo, una respuesta corta de Claude
Haiku 4.5 sin herramientas, p50 de n=5):

| Escenario | Hasta el primer token | VM | Modelo |
|---|---|---|---|
| `create()` sin prefetch | 28,3 s | lanzamiento $0,0014 + 28,5 s ≈ $0,0024 | ≈ $0,0009 (7 596 tokens leídos de caché; $0,0104 la vuelta que los escribe) |
| `create()` con prefetch (A), antes del arreglo | 20,5 s | ≈ $0,0021 | ≈ $0,0009 |
| `create()` con prefetch (A), demonio que espera al guest | <!-- REMEDIR-A --> | <!-- REMEDIR-A --> | ≈ $0,0009 |
| `connect()` tras `pause()` (B) | 3,1 s | ciclo ≈ $0,005–0,006 | ≈ $0,0009 |
| `take()` de un pool con `warmup` (C) | 5,1 s | lectura $0,0014 + ≈ 5 s ≈ $0,0016 | ≈ $0,0009 |
| `take()` de un pool con servidor residente (D), con la relectura | <!-- REMEDIR-D --> | <!-- REMEDIR-D --> | ≈ $0,0009 |
| `create()` con deepagents | 7,5 s tras `create()` (5,0 s OpenCode) | ≈ $0,0014 + segundos de VM | ≈ $0,0039 (3 503 tokens sin caché: por debajo del mínimo de 4 096 de Haiku) |

Cómo salen las cifras de las plazas:

- **Plaza base**: el límite de 8 h incluye el tiempo suspendido, así que
  el pool recicla cada ≈ 7 h ⇒ 720 h / 7 h ≈ **103 ciclos al mes**. Un ciclo
  es lanzar (lee 0,92 GB: $0,0014) + escribir el snapshot al aparcar
  (0,92 GB × $0,0038: $0,0035) + unos segundos de cómputo ≈ $0,0052.
  103 × $0,0052 ≈ $0,53 + almacenamiento 0,92 GB × $0,08 ≈ $0,074 ⇒
  **≈ $0,60/mes**.
- **C (warmup)**: lanzar $0,0014 + 15,5 s medidos hasta la plaza lista
  ($0,0005) + aparcar ≈ 0,92 GB ($0,0035) ≈ $0,0054 × 103 ≈ $0,56 +
  almacenamiento $0,074 ⇒ **≈ $0,64/mes**. La API no da el tamaño del
  snapshot de un `suspend`; se toma igual al de la imagen porque la memoria
  usada del guest tras la toma (503 MiB) es menor que la de un `create()`
  fresco.
- **D (servidor residente)**: 17,7 s hasta la plaza lista y un snapshot de
  ≈ 1,29 GB (el guest usa 352 MiB más, casi todo el servidor) ⇒
  ≈ $0,0069 × 103 + ≈ $0,10 ⇒ **≈ $0,82/mes**.
- **Pausar entre turnos (B)**: el sandbox no puede pasar de 8 h
  lanzado + suspendido. Pasado ese tope, guarda el estado con
  [persistencia](persistence.md) y crea una VM nueva (otro lanzamiento,
  $0,0014, y otra vez el primer `exec` frío).

### Qué opción de arranque rápido elegir

| Tu caso | Opción | Coste extra (List) |
|---|---|---|
| Tareas sueltas, puedes esperar unos segundos | A. Prefetch / `prepare()` | ≈ $0 |
| Conversaciones con pausas de minutos u horas (< 8 h) | B. `pause()` y `connect()` | ≈ $0,005–0,006 por ciclo + ≈ $0,0001/h guardado |
| Muchas tomas al día, latencia mínima | C. Pool con `warmup` | ≈ $0,64/plaza/mes |
| Lo anterior y el servidor ya arrancado | D. Pool con servidor residente | ≈ $0,82/plaza/mes (pendiente de la nueva medida) |

Decidido con las medidas: A queda encendida por defecto (baja el primer
`exec` tras `create()` un 76 %, por encima del 50 % pedido) y C es la
recomendada para latencia mínima. D se midió antes de arreglar la
relectura de la vuelta (`opencode run --attach` perdía los eventos) y está
pendiente de la nueva medida; ver
[Pool](pool.md#resultado-de-la-medida-en-aws) y la tabla completa en
[Agente en el sandbox](guias/agente-en-el-sandbox.md#arranque-rapido).

??? info "Fuentes y mediciones (agentes)"
    - Diseño: `design.md` de `ai-agent-core` en
      [`openspec/changes/`](https://github.com/alejandro-cedeno-10/rayito/tree/main/openspec/changes).
    - Spike: [`docs/research/2026-10-agent-spike.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/docs/research/2026-10-agent-spike.md).
    - Precios de MicroVMs: igual que el resto de esta página, `AWS_API_NOTES.md` §12.
    - Precios de Lambda MicroVMs:
      [aws.amazon.com/lambda/pricing](https://aws.amazon.com/lambda/pricing/)
      y la offer `AWSLambda` (publicada 2026-10-01), consultados 2026-10-06.
    - Precios de Bedrock: offer `AmazonBedrockFoundationModels` (publicada
      2026-09-30),
      [aws.amazon.com/bedrock/pricing](https://aws.amazon.com/bedrock/pricing),
      consultados 2026-10-06.
    - Caché de prompts:
      [docs.aws.amazon.com/bedrock/.../prompt-caching.html](https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-caching.html).
    - Tiempos, tamaños y tokens medidos en AWS real el 2026-10-07
      (Q146–Q150 y Q152 de `AWS_API_NOTES.md` §16); el contraste con una
      factura real (Q151) sigue pendiente.

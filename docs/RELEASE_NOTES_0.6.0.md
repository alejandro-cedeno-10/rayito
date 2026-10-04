# Rayito 0.6.0 — notas de la release (borrador)

Pendiente: esta página se completa al cerrar M15 (Rayito 0.6), cuando la
aceptación serializada contra AWS real (`MILESTONES.md`, presupuesto
≤ $8.00) termine. Cada sección la rellena la función correspondiente con
sus números reales; `m15-docs-integration` consolida el documento final.

## Foundations (`v06-foundations`)

Pendiente: `ConfigureSandbox` (ADR-015), el convenio `OptionalStack`
(ADR-016, `rayito stack`), la fila de compatibilidad 0.6 y los seams de
cada función. Zombie reaping de PID 1 implementado y probado, activación
pendiente de registrar los PIDs en los tres adaptadores de spawn (ver
MILESTONES.md).

## Montajes S3 (`m15-s3-mounts`)

Pendiente.

## Volúmenes EFS (`m15-efs-volumes`, experimental)

`VolumeStore`/`AsyncVolumeStore` (CRUD real de access points EFS) y
`Sandbox.create(volumes=)` (valida la petición y siempre lanza
`UnimplementedError`, pendiente de la campaña de medición EFS-1..EFS-20).
`rayito stack deploy efs-volumes` despliega el sistema de ficheros, sus
mount targets y un conector de egress dedicado. El shim de E2B
(`Volume`/`AsyncVolume`) hace CRUD real sobre `E2B(volume_store=...)`; sus
operaciones de contenido siguen sin plano de datos propio. El montaje real
en el guest queda para una función posterior, una vez la campaña de
medición despeje sus tres criterios de parada (EFS-2, EFS-3, EFS-8). Ver
[Volúmenes EFS](site/docs/funciones-opcionales/volumenes-efs.md).

## Tamaños (`m15-sizes-catalog`)

Catálogo cerrado de cinco tamaños (512mb/1gb/2gb/4gb/8gb, Q87), apagado
por defecto: `Sandbox.create(size="4gb")`/`Sandbox.create({ size: "4gb" })`
resuelve en cliente, sin ningún RPC, redondeando siempre hacia arriba y
avisando si no encaja exacto. `rayito image publish --sizes 512mb,4gb`
publica, desde el mismo artefacto, una imagen adicional por tamaño.
`get_info()`/`getInfo()` confirma el tamaño real con una única llamada
cacheada a `GetMicrovmImageVersion` (`baseline_memory_mib`/`baselineMemoryMib`,
`baseline_cpu`/`baselineCpu`, medido exactamente para los cinco tamaños).
Guardarraíles de coste opcional `rayito stack deploy sizes-guard`.
Pendiente: aceptación en AWS real (SZ-1 y siguientes, MILESTONES.md).

## Eventos y webhooks (`m15-events-webhooks`)

`rayd` emite `created`/`paused`/`resumed`/`killed` firmados por HMAC en su
propio stdout, sólo cuando `ConfigureSandbox` trae una clave por sandbox
(derivada y empujada por el SDK; `rayd` nunca ve el secreto del stack). Una
pila opcional (`infra/events-webhooks.yaml`) los verifica, guarda en
DynamoDB (TTL 7 días) y entrega a tus webhooks con firma compatible con
E2B, con un guardián SSRF y sin perder entregas (reclamo `attempting` →
`delivered`/`failed`, reintento sólo ante 5xx, presupuesto de tiempo por
invocación). Un reconciliador (cada `ReconcilerIntervalMinutes`, 5 por
defecto) sintetiza `killed{unknown}` para sandboxes que `ListMicrovms` ya no
reporta. Nuevo: `LifecycleEvents`/`AsyncLifecycleEvents` (`deploy`/`status`/
`destroy`, `register_webhook`/`list_webhooks`/`delete_webhook`/
`get_events`), CLI `rayito events`. Apagado por defecto.

**Pendiente de la aceptación contra AWS real:** CP-4/CP-5 (si `/terminate`
llega siempre y si las líneas de `/suspend` alcanzan CloudWatch a tiempo),
el supuesto del forwarder sobre el nombre del *log stream*, y el flujo de
extremo a extremo (entrega firmada, línea forjada descartada, `killed`
sintetizado). Ver `AWS_API_NOTES.md` §25 y `ADR-020`.

**`Sandbox.create(events=...)`:** valida la opción (un `LifecycleEvents` y
`logging` con CloudWatch) antes de lanzar y, tras `run-microvm`, manda la
clave del sandbox (`k_sbx`, derivada de la clave del stack y el
`sandbox_id`) en el mismo `ConfigureSandbox` que `mounts=`/`gateways=`/
`telemetry=`; si la pila no está desplegada o la imagen no lo soporta,
termina el sandbox. Ver `ADR-020`.

## Exportación OTLP (`m15-rayd-otlp`)

`rayd` exporta 7 métricas de CPU, memoria y disco a CloudWatch por
OTLP/HTTP cada `interval_s` (15-300 s), firmadas con SigV4 sobre el
execution role (`OtlpAuth.execution_role()`, exige `rayito-base-caps`) o
con un token al portador (`OtlpAuth.bearer(...)`, experimental, funciona en
`rayito-base`). Opt-in: `telemetry=`/`telemetry` en `Sandbox.create()`.
`sbx.get_telemetry_status()`/`sbx.getTelemetryStatus()` consulta exportadas,
descartadas y el último error. Con `tracer_provider=`/`tracerProvider`,
cada RPC del handle lleva además W3C `traceparent` hasta `rayd`, que lo
registra como `trace_id`/`span_id`. Números `OT*` reales y mediciones de coste/overhead
pendientes de la etapa de aceptación contra AWS real.

## Templates (`m15-templates`)

`Template`/`AsyncTemplate`: un DSL fluido, igual al `Template` de E2B v2,
que compila a un Dockerfile y un zip deterministas sobre una imagen
`rayito-base`/`rayito-base-caps` ya publicada y los sube con
`create`/`update-microvm-image`. Build pipeline completo en los dos SDKs
(composición, subida por hash, reuso de versión idéntica, explicación de
fallos desde el log de BuildKit o el `stateReason` del `ready_cmd`,
guardia de 10 builds concurrentes que cubre toda la espera, cuota de AWS
como `build_quota`), sobre un núcleo de build compartido con `rayito image
publish`. La imagen compuesta hereda la configuración de la base
(`rayito-base-caps` sigue siendo caps). `set_start_cmd()`/`setStartCmd()`:
`rayd` 0.6 lee `/etc/rayito/template.json`, lanza `start_cmd` como proceso
gestionado y gatea `/ready` con `ready_cmd`. Shim de E2B
`Template`/`AsyncTemplate` ya construye de verdad, con la firma de E2B; `BuildException`/`TemplateException`
(Python) y `BuildError`/`TemplateError` (TypeScript) del shim pasan a ser
las clases nativas. `infra/templates.yaml` (`RayitoTemplateBuilder`, sólo
IAM, $0 en reposo). Divergencias: sin caché de capas, sólo ARM64, sólo
`from_base_image()`, sin streaming en vivo de los pasos, sin etiquetado
por versión.

**Pendiente de la aceptación serializada contra AWS real** (plan y tope de
coste en `openspec/changes/m15-templates/proposal.md` y en el plan de
aceptación del agente): un build real de principio a fin, uno que falle en
un paso del Dockerfile, uno con un `ready_cmd` que falle, y la supervivencia
del `start_cmd` a un ciclo de suspend/resume (necesita una imagen
`rayito-base` publicada con el `rayd` de esta versión).

## Pasarela de secretos (`m15-secrets-gateway`)

Pendiente.

## Dominio propio (`m15-custom-domain`)

Pendiente de D3 (dominio y certificado ACM del mantenedor).

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

Pendiente, tras la campaña de medición EFS-1..EFS-20.

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

Pendiente.

## Exportación OTLP (`m15-rayd-otlp`)

Pendiente.

## Templates (`m15-templates`)

Pendiente.

## Pasarela de secretos (`m15-secrets-gateway`)

Pendiente.

## Dominio propio (`m15-custom-domain`)

Pendiente de D3 (dominio y certificado ACM del mantenedor).

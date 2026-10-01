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

Pendiente.

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

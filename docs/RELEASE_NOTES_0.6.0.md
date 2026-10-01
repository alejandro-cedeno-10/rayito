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

Pendiente.

## Eventos y webhooks (`m15-events-webhooks`)

Pendiente.

## Exportación OTLP (`m15-rayd-otlp`)

`rayd` exporta 7 métricas de CPU, memoria y disco a CloudWatch por
OTLP/HTTP cada `interval_s` (15-300 s), firmadas con SigV4 sobre el
execution role (`OtlpAuth.execution_role()`, exige `rayito-base-caps`) o
con un token al portador (`OtlpAuth.bearer(...)`, experimental, funciona en
`rayito-base`). Opt-in: `telemetry=`/`telemetry` en `Sandbox.create()`.
`sbx.get_telemetry_status()`/`sbx.getTelemetryStatus()` consulta exportadas,
descartadas y el último error. La propagación W3C `traceparent` del lado
del SDK está implementada pero aún no conectada al canal real (seguimiento
no bloqueante). Números `OT*` reales y mediciones de coste/overhead
pendientes de la etapa de aceptación contra AWS real.

## Templates (`m15-templates`)

Pendiente.

## Pasarela de secretos (`m15-secrets-gateway`)

Pendiente.

## Dominio propio (`m15-custom-domain`)

Pendiente de D3 (dominio y certificado ACM del mantenedor).

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

Pendiente.

## Templates (`m15-templates`)

`Template`/`AsyncTemplate`: un DSL fluido, igual al `Template` de E2B v2,
que compila a un Dockerfile y un zip deterministas sobre una imagen
`rayito-base`/`rayito-base-caps` ya publicada y los sube con
`create`/`update-microvm-image`. Build pipeline completo en los dos SDKs
(composición, subida por hash, reuso de versión idéntica, explicación de
fallos desde el log de BuildKit o el `stateReason` del `ready_cmd`,
guardia de 10 builds concurrentes). Shim de E2B `Template`/`AsyncTemplate`
ya construye de verdad; `BuildException`/`TemplateException`
(Python) y `BuildError`/`TemplateError` (TypeScript) del shim pasan a ser
las clases nativas. `infra/templates.yaml` (`RayitoTemplateBuilder`, sólo
IAM, $0 en reposo). Divergencias: sin caché de capas, sólo ARM64, sólo
`from_base_image()`, sin streaming en vivo de los pasos, sin etiquetado
por versión.

**Pendiente de la aceptación serializada contra AWS real** (plan y tope de
coste en `openspec/changes/m15-templates/proposal.md` y en el plan de
aceptación del agente): un build real de principio a fin, uno que falle en
un paso del Dockerfile, uno con un `ready_cmd` que falle, y la supervivencia
del `start_cmd` a un ciclo de suspend/resume — este último bloqueado además
por un seguimiento no bloqueante: el lado de `rayd` que lee
`/etc/rayito/template.json` y arranca/sondea el proceso no se incluyó en
este cambio (ver `proposal.md`).

## Pasarela de secretos (`m15-secrets-gateway`)

Pendiente.

## Dominio propio (`m15-custom-domain`)

Pendiente de D3 (dominio y certificado ACM del mantenedor).

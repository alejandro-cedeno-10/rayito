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

`rayd` emite `created`/`paused`/`resumed`/`killed` firmados por HMAC en su
propio stdout, sólo cuando `ConfigureSandbox` trae una clave por sandbox
(derivada y empujada por el SDK; `rayd` nunca ve el secreto del stack). Una
pila opcional (`infra/events-webhooks.yaml`) los verifica, guarda en
DynamoDB (TTL 7 días) y entrega a tus webhooks con firma compatible con
E2B, con un guardián SSRF. Un reconciliador (`rate(5 min)`) sintetiza
`killed{unknown}` para sandboxes que `ListMicrovms` ya no reporta. Nuevo:
`LifecycleEvents`/`AsyncLifecycleEvents` (`deploy`/`status`/`destroy`,
`register_webhook`/`list_webhooks`/`delete_webhook`/`get_events`), CLI
`rayito events`. Apagado por defecto; `events=` exige
`logging="cloudwatch"`.

**Pendiente de la aceptación contra AWS real:** CP-4/CP-5 (si `/terminate`
llega siempre y si las líneas de `/suspend` alcanzan CloudWatch a tiempo),
el supuesto del forwarder sobre el nombre del *log stream*, y el flujo de
extremo a extremo (entrega firmada, línea forjada descartada, timeout
sintetizado). Ver `AWS_API_NOTES.md` §25 y `ADR-020`.

**Hueco de integración conocido:** `create()` todavía no envía la sección
de `ConfigureSandbox` de esta función (necesita `sandbox_id`, que sólo se
conoce después de `run-microvm`); `events=` ya valida, pero la clave por
sandbox aún no llega a `rayd` en esta iteración. Ver `ADR-020`, "Hueco de
integración conocido".

## Exportación OTLP (`m15-rayd-otlp`)

Pendiente.

## Templates (`m15-templates`)

Pendiente.

## Pasarela de secretos (`m15-secrets-gateway`)

Pendiente.

## Dominio propio (`m15-custom-domain`)

Pendiente de D3 (dominio y certificado ACM del mantenedor).

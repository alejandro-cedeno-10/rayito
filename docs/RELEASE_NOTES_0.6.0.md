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

Pendiente.

## Pasarela de secretos (`m15-secrets-gateway`)

Pendiente.

## Dominio propio (`m15-custom-domain`)

`CustomDomain` (Python sync/async, TypeScript) despliega una distribución
CloudFront con alias comodín, una CloudFront Function de enrutado y un
KeyValueStore (`infra/custom-domain.yaml`, `rayito domain
deploy|status|destroy`), y gestiona las rutas `{puerto}-{alias}.<tu
dominio>` con `register()`/`unregister()`/`refresh()`. Experimental y
apagado por defecto: construirlo no llama a AWS, y sólo `deploy()`,
`register()`, etc. lo hacen.

`Sandbox.create(domain=)`/`get_host()`/`expose()` **no** están cableados
todavía a `CustomDomain` en esta release: `domain=` sigue lanzando
`UnimplementedError`, con un mensaje que nombra ese seguimiento en vez de
este cambio (seguimiento no bloqueante, ver `ARCHITECTURE.md` ADR-024). El
refresher Lambda opcional que la arquitectura de M15 describe tampoco se
construyó (**DOM-14, pendiente**): `CustomDomain.refresh()` cubre el mismo
caso desde el SDK mientras tanto. DOM-2 (HTTP/1.1 por
`cf.updateRequestOrigin`), DOM-3 (WebSocket), DOM-5 (latencia de
propagación del KeyValueStore), DOM-7 (keep-alive tras caducar el JWE),
DOM-8 (auto-resume por el dominio) y DOM-14 están pendientes de D3 (dominio
y certificado ACM del mantenedor) y de la etapa de aceptación contra AWS
real.

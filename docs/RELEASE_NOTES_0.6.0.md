# Rayito 0.6.0 — notas de la release

Publicada el 2026-10-03 (SDK Python `rayito` 0.6.0, SDK TypeScript `rayito`
0.6.0 y `rayd` 0.6.0). El detalle completo, cambio a cambio, está en los
tres CHANGELOG, que son la referencia:

- [`clients/python/CHANGELOG.md`](../clients/python/CHANGELOG.md#060---2026-10-03)
- [`clients/typescript/CHANGELOG.md`](../clients/typescript/CHANGELOG.md#060---2026-10-03)
- [`crates/rayd/CHANGELOG.md`](../crates/rayd/CHANGELOG.md#060---2026-10-03)

Esta página resume qué trae 0.6.0. Todas las funciones nuevas están
**apagadas por defecto**: sin la opción correspondiente, un sandbox se
comporta byte a byte como en 0.5.x y no hace ninguna llamada nueva a AWS
([Funciones opcionales y su coste](site/docs/optional-features.md)).

## Foundations (`v06-foundations`)

`ConfigureSandbox` (ADR-015), el único canal por el que el SDK configura
una función opcional dentro del sandbox justo después de que el agente esté
listo, y el convenio `OptionalStack` (ADR-016): `OptionalStacks` en los dos
SDKs y `rayito stack list|deploy|status|destroy` para desplegar en tu
cuenta la infraestructura de cada función. Fila de compatibilidad 0.6 en
`rayito doctor`.

El reaper de zombis huérfanos de PID 1 quedó implementado y probado en
0.6.0, pero **sin activar**; lo activa 0.6.1 (`rayd-orphan-reaper`), junto
con `reincarnate()` reaplicando todas las secciones de `ConfigureSandbox`
(`m15-reincarnate-configure-replay`).

## Funciones nuevas

| Función | Opción | Guía | Aceptación en AWS real |
|---|---|---|---|
| Montajes S3 | `mounts=` / `mounts` | [Montajes S3](site/docs/funciones-opcionales/montajes-s3.md) | sí (`AWS_API_NOTES.md` Q100–Q104) |
| Tamaños | `size=` / `size`, `rayito image publish --sizes` | [Tamaños](site/docs/funciones-opcionales/tamanos.md) | sí (Q118, Q119) |
| Eventos y webhooks | `events=` / `events`, `rayito events` | [Eventos y webhooks](site/docs/funciones-opcionales/eventos-y-webhooks.md) | sí (Q105–Q108) |
| Exportación OTLP | `telemetry=` / `telemetry` | [Exportación OTLP](site/docs/funciones-opcionales/exportacion-otlp.md) | sí (Q109–Q113, Q120) |
| Templates declarativos | `Template.build()`, `rayito template` | [Templates](site/docs/funciones-opcionales/templates.md) | sí (`AWS_API_NOTES.md` §27) |
| Pasarela de secretos | `gateways=` / `gateways` | [Pasarela de secretos](site/docs/funciones-opcionales/pasarela-de-secretos.md) | sí (`AWS_API_NOTES.md` §28) |

En el shim de E2B, `Template`/`AsyncTemplate` construyen de verdad y
`TemplateException`/`BuildException` se lanzan desde `Template.build()`
([Paridad con E2B](site/docs/e2b-parity.md)).

## Lo que no trae 0.6.0

- **Volúmenes EFS** (`volumes=`, `m15-efs-volumes`) y **dominio propio**
  (`domain=`, `m15-custom-domain`): siguen sin implementar; las dos
  opciones existen en la firma de `create()` y lanzan `UnimplementedError`
  nombrando su cambio, sin llamar a AWS.

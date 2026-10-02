# docs-delta: m15-rayd-otlp

Applied only by `m15-docs-integration`, in one PR together with every other
feature's delta. Each block below names the file, the anchor/context to
find the target by (line numbers drift as sibling features land their own
deltas; the surrounding text does not), and the exact replacement.

## `docs/site/docs/e2b-parity.md` — row 108

Current row 108 (0.5.0, divergent) talks only about the SDK-side OTel spans
(`tracer_provider=`). Replace it to also name the new, real OTLP export to
CloudWatch:

**Find** the row starting `| 108 | exportación de telemetría OTel (docs,
Enterprise) | divergente (0.5.0) |`.

**Replace with:**

```
| 108 | exportación de telemetría OTel (docs, Enterprise) | divergente (0.6.0) | la exportación de telemetría del sandbox de E2B es sólo Enterprise, la configura E2B en el onboarding y no tiene API ni kwarg; aquí es explícita y opt-in: `telemetry=`/`telemetry` (`TelemetryExport`/`OtlpAuth`) hace que `rayd` exporte 7 gauges de CPU, memoria y disco a CloudWatch por OTLP/HTTP, firmado con SigV4 sobre el execution role o con un token al portador — complementario a `tracer_provider=`/`tracerProvider` (spans del lado cliente, sin relación); el shim de E2B no añade ningún kwarg nuevo para esto | [Exportación OTLP](funciones-opcionales/exportacion-otlp.md) |
```

## `docs/site/docs/optional-features.md` — "De un vistazo" table

**Find** the row for `[Trazas OpenTelemetry]` in the "De un vistazo" table
(the 6-column one near the top of the file).

**Add** a new row immediately after it:

```
| [Exportación OTLP](funciones-opcionales/exportacion-otlp.md) | apagada | `telemetry=` / `telemetry`: `rayd` exporta 7 gauges de CPU/memoria/disco a CloudWatch por OTLP/HTTP | $0 por la opción; CloudWatch factura las métricas que de verdad se exporten | `cloudwatch:PutMetricData` sobre el dataset OTLP por defecto (no acotable por namespace) | no pasar `telemetry=` / `telemetry` |
```

## `docs/site/docs/optional-features.md` — full table (the 11-column one, "Funciones con coste AWS")

**Find** the row for `[Trazas OpenTelemetry del SDK](#otel-sdk)`.

**Add** a new row immediately after it, and add the anchor `<a id="otlp-export"></a>` right before it (same pattern as the existing `<a id="otel-sdk"></a>` anchor):

```
<a id="otlp-export"></a>
| [Exportación OTLP](#otlp-export) | disponible (0.6.0) | `telemetry=` | `telemetry` | `None` / `undefined` | `rayd` exporta 7 gauges (CPU, memoria, disco) a CloudWatch cada `interval_s` (15-300 s, 60 por defecto) por OTLP/HTTP, firmado con SigV4 sobre el execution role (`OtlpAuth.execution_role()`, exige `rayito-base-caps`) o con un token al portador empujado por `ConfigureSandbox` (`OtlpAuth.bearer(...)`, experimental, funciona en `rayito-base`) | `cloudwatch:PutMetricData` por lote exportado; con `OtlpAuth.execution_role()`, la política `RayitoOtlpExport` (`infra/otlp-export.yaml`, `rayito stack deploy otlp-export`) en el execution role | $0 por la opción en sí; CloudWatch factura las métricas OpenTelemetry a $0,50/GB ingerido ([precios de CloudWatch](https://aws.amazon.com/cloudwatch/pricing/), consultado 2026-10-02): ≈ $0,00014 por sandbox-hora con `interval_s=60` (estimación de la investigación, provisional hasta OT2), p. ej. 30 000 sandbox-horas/mes ≈ $4,20; `sandbox_id` crea 7 series por sandbox (por OTLP se paga por bytes, no por serie) | `cloudwatch:PutMetricData` sobre el dataset OTLP por defecto de la cuenta — no se puede acotar por namespace (OT9); con `OtlpAuth.bearer(...)`, `secretsmanager:GetSecretValue` sobre `rayito/*` (`RayitoSecretsReader`: el nombre se resuelve bajo `rayito/`, como en `secrets=`) | No pasar `telemetry=` / `telemetry` (o `None`/`undefined`); `rayito stack destroy otlp-export` si ya no la usa ningún sandbox | `clients/python/src/rayito/_telemetry_export/` / `clients/typescript/src/telemetry-export/` |
```

## `SECURITY.md` — new threat T23

**Find** the end of the threat table (last row is `T19`, metadata index).

**Add** a new row:

```
| T23 | exportación de telemetría del guest | `telemetry=TelemetryExport(auth=OtlpAuth.execution_role())` da a `rayd` las credenciales del execution role dentro del guest para firmar `PutMetricData`; la política mínima de CloudWatch **no se puede acotar por namespace** (investigado, OT9), así que un sandbox comprometido con esta opción puede escribir métricas arbitrarias, no sólo las 7 de `rayito.sandbox.*` — coste e integridad de dashboards, no una fuga de datos del cliente | Opt-in explícito (`telemetry=`/`telemetry`, ADR-014 regla 4: ninguna variable de entorno la activa); la cola está acotada y el valor de un `OtlpAuth.bearer(...)` nunca se loguea ni se devuelve por `ConfigureStatus` (sólo vive en memoria del agente, `Zeroizing`); recomendación para cuentas que no confían en el código del sandbox: `OtlpAuth.bearer(...)` (un token acotado a un log group, sin dar el execution role) en vez de `OtlpAuth.execution_role()`, o una imagen caps sin más privilegios de los necesarios. Residual aceptado: con `OtlpAuth.execution_role()`, el alcance de `cloudwatch:PutMetricData` es el dataset OTLP por defecto de toda la cuenta, no un namespace de Rayito — una limitación del endpoint OTLP de CloudWatch, no de esta implementación | m15-rayd-otlp |
```

## `docs/site/docs/security.md` — concise table + detailed section

**Find** the concise table row for `Índice de metadatos (T19)` (near the
top, "Lo que protege el SDK por defecto").

**Add** a new row immediately after it:

```
| Exportación OTLP (T23) | apagado por defecto; con `telemetry=OtlpAuth.bearer(...)` (recomendado si no confías en el código del sandbox) no se entrega el execution role; con `OtlpAuth.execution_role()` la política de CloudWatch no puede acotarse por namespace (riesgo residual documentado) ([Exportación OTLP](funciones-opcionales/exportacion-otlp.md)) |
```

**Add** a new detailed section near the end of the file (same pattern as
`## Persistencia en S3 (T15)` / `## Custodia de secretos del usuario
(T18)`), titled `## Exportación OTLP (T23)`, summarizing the residual risk
above and pointing to `SECURITY.md` T23 for the full detail (`Detalle en
SECURITY.md T23.`).

## Not changed by this feature

`docs/site/docs/cost.md` only references `optional-features.md` for opt-in
feature pricing (no separate per-feature cost table to update).
`docs/site/docs/limits.md`'s 0.6 compatibility row was already added by
`v06-foundations` (no protocol/agent-version change in this feature beyond
what that row already covers). `docs/site/docs/referencia/{errores,variables-de-entorno}.md`
need no entry: this feature adds no new exception class (it reuses
`InvalidArgumentException`/`UnimplementedError`/`SecretException`, already
documented) and no new environment variable (ADR-014 rule 4).

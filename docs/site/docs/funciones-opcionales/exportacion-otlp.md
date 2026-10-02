# Exportación OTLP

`rayd` (el agente dentro del `MicroVM`) exporta 7 métricas de CPU, memoria y
disco a CloudWatch cada `interval_s` (60 s por defecto, 15..=300 s) por
OTLP/HTTP, firmadas con SigV4 o con un token al portador. <small>Desde 0.6.0</small>

!!! info "Coste y activación"
    - **Por defecto**: apagado. Sin `telemetry=` (TypeScript: `telemetry`)
      el SDK no envía ninguna sección de `ConfigureSandbox` y `rayd` no abre
      ninguna conexión saliente nueva: cero coste, cero llamada a AWS.
    - **Activa**: `telemetry=TelemetryExport(...)` en `Sandbox.create()` /
      `AsyncSandbox.create()` (TypeScript: `telemetry: new TelemetryExport({...})`).
      Se envía como una sección de `ConfigureSandbox` justo después de que
      el agente esté listo, nunca en el propio `run-microvm`.
    - **Recursos y llamadas AWS**: `rayd` hace un `PutMetricData` por lote
      exportado (uno cada `interval_s`, agrupando las 7 gauges). Con
      `OtlpAuth.execution_role()` necesitas la política IAM
      `RayitoOtlpExport` (`infra/otlp-export.yaml`,
      `rayito stack deploy otlp-export`) en el execution role.
    - **Coste aproximado** (medido, Q108): $0 por la opción en sí;
      CloudWatch factura las métricas OpenTelemetry a **$0,50 por GB
      ingerido** ([precios de CloudWatch](https://aws.amazon.com/cloudwatch/pricing/),
      us-east-1, consultado 2026-10-02). Cada lote son 7 puntos en una
      petición de 639 bytes de protobuf (353 bytes con gzip): con
      `interval_s=60`, 60 lotes ≈ 38 KB por sandbox-hora ≈ **$0,00002 por
      sandbox-hora** contando los bytes sin comprimir (la cota alta: AWS no
      publica si factura sobre el cuerpo comprimido). Ejemplo: 1 000
      sandboxes al día de una hora cada uno = 30 000 sandbox-horas al mes ≈
      1,15 GB ≈ **$0,58/mes**; con `interval_s=15`, 4 veces más (≈ $2,30/mes).
      Más la política IAM, que no cuesta nada.
    - **Cardinalidad**: `sandbox_id` es un atributo de recurso, así que
      cada sandbox crea sus propias 7 series. Por OTLP se paga por bytes,
      no por serie, así que muchos sandboxes cortos no multiplican la
      factura de ingesta; pero si reenvías estas métricas como métricas
      personalizadas clásicas ($0,30 por métrica y mes, prorrateado por
      hora) cada sandbox costaría 7 × $0,30 / 730 h ≈ $0,0029 por hora
      iniciada, unas 20 veces más, y los dashboards que agregan por
      `sandbox_id` crecen con el número de sandboxes.
    - **IAM**: `cloudwatch:PutMetricData` sobre el dataset OTLP por defecto
      de la cuenta — el endpoint OTLP de CloudWatch no admite acotar por
      namespace (investigado, ver "Fuentes y mediciones" abajo). Con
      `OtlpAuth.bearer(...)` en su lugar hace falta permiso para leer el
      secreto que guarda el token (`secretsmanager:GetSecretValue` sobre
      `rayito/*`, la política `RayitoSecretsReader`).
    - **Cómo apagarla**: no pases `telemetry=` (por defecto `None`/`undefined`);
      borra la pila `otlp-export` (`rayito stack destroy otlp-export`) si ya
      no la usa ningún sandbox.

## Cuándo usarlo

- Quieres ver CPU, memoria y disco de tus sandboxes en un dashboard de
  CloudWatch junto al resto de tu infraestructura, sin montar tu propio
  collector.
- **Cuándo no**: si sólo necesitas una instantánea puntual o un historial
  corto dentro de tu propio proceso, [`get_metrics_history()`](../observability.md#instantanea-e-historial)
  no tiene coste de AWS y no exige ninguna imagen especial.

## Qué exporta

Exactamente 7 gauges, muestreados del mismo anillo de 5 s que ya sirve
`get_metrics_history()` — ningún sondeo adicional del guest:

| Métrica (`names="rayito"`) | `names="e2b"` | Qué es |
|---|---|---|
| `rayito.sandbox.cpu.used_pct` | `e2b.sandbox.cpu.used_pct` | % de CPU en uso |
| `rayito.sandbox.cpu.count` | `e2b.sandbox.cpu.count` | CPUs que ve el guest |
| `rayito.sandbox.memory.used_bytes` | `e2b.sandbox.memory.used_bytes` | Memoria en uso |
| `rayito.sandbox.memory.total_bytes` | `e2b.sandbox.memory.total_bytes` | Memoria total del guest |
| `rayito.sandbox.memory.cache_bytes` | `e2b.sandbox.memory.cache_bytes` | Caché de página |
| `rayito.sandbox.disk.used_bytes` | `e2b.sandbox.disk.used_bytes` | Disco en uso |
| `rayito.sandbox.disk.total_bytes` | `e2b.sandbox.disk.total_bytes` | Disco total |

Cada lote lleva 4 atributos de recurso, una lista cerrada (nunca una ruta,
un comando o un valor de `metadata`): `sandbox_id`, `image_arn`,
`image_version` e `image_memory_mib` (la memoria **declarada** de la
imagen, no la que ve el guest: el guest ve 4× esa cifra, así que el SDK
divide por ese factor antes de enviarla; ver `limits.md`).

!!! note "`get_telemetry_status()` es una llamada aparte"
    El boceto original de la arquitectura preveía `sbx.get_health().telemetry`.
    Esto habría obligado a `get_health()` -- que llama *todo* sandbox, use o
    no `telemetry=` -- a hacer una llamada extra a `ConfigureStatus`
    condicional (o siempre), arriesgando la garantía de "cero llamadas sin
    la opción". `get_telemetry_status()`/`getTelemetryStatus()` es, en su
    lugar, un método nuevo y explícito: nunca se llama si no lo pides, así
    que esa garantía se cumple trivialmente (`design.md` D7 de
    `m15-rayd-otlp` registra la decisión).

## Autenticación

=== "Rol de ejecución (`OtlpAuth.execution_role()`)"

    SigV4 sobre las credenciales IMDS del execution role. Exige la variante
    `rayito-base-caps` (o una derivada por tamaño, p. ej.
    `rayito-base-caps-4gb`): sin ella, `Sandbox.create()` lanza
    `UnimplementedError` antes de `run-microvm` cuando el nombre de la
    imagen ya lo permite saber. Con un ARN o un nombre propio el SDK no
    puede saber la variante y `rayd` sólo comprueba que haya execution
    role: sin rol rechaza la sección (`role_not_permitted`), el SDK termina
    la `MicroVM` y lanza `SandboxException`; con rol sobre una imagen sin
    caps exporta igual, pero sus credenciales quedan legibles para uid 1000
    (T1). Usa el nombre `rayito-base-caps` para que el chequeo sea previo.

    La política mínima de CloudWatch no puede acotarse por namespace: con
    esta opción, un sandbox comprometido puede escribir métricas arbitrarias
    con el nombre que quiera (no sólo las 7 de `rayito`). Si eso te
    preocupa, usa `OtlpAuth.bearer(...)` o una imagen `rayito-base-caps` sin
    más privilegios de los necesarios.

=== "Token al portador (`OtlpAuth.bearer(...)`, experimental)"

    Un secreto de Secrets Manager cuyo valor es una API key de CloudWatch
    Metrics: una credencial específica de servicio
    (`iam create-service-specific-credential --service-name
    cloudwatch.amazonaws.com`) de un usuario IAM con
    `cloudwatch:CallWithBearerToken` y `cloudwatch:PutMetricData`. No se
    acota a un log group ni a un namespace: sólo sirve para el endpoint de
    ingesta OTLP de métricas, de toda la cuenta. Pon siempre
    `--credential-age-days`. Una SCP de la organización que deniegue
    `iam:CreateUser` impide crearla (Q111). El nombre se resuelve igual que en `secrets=` y
    `SecretStore`: bajo el prefijo `rayito/`, así que
    `OtlpAuth.bearer("otlp-key")` lee `rayito/otlp-key` (un ARN completo se
    usa tal cual). El SDK lo lee por la misma caché de secretos que
    `secrets=` (como mucho un `GetSecretValue` cada 5 minutos), al enviar la
    sección, y lo empuja a `rayd` por `ConfigureSandbox` — nunca por una
    variable de entorno (ADR-014 regla 4) ni en texto plano en ningún lado;
    `rayd` lo guarda sólo en memoria. Funciona en `rayito-base`, sin caps.

## Instalación

No hace falta ningún paquete extra: `TelemetryExport`/`OtlpAuth` ya forman
parte del SDK. Si usas `OtlpAuth.execution_role()`, despliega la política
IAM una vez:

```bash
rayito stack deploy otlp-export
```

## Ejemplo rápido

=== "Python"

    ```python
    from rayito import OtlpAuth, Sandbox, TelemetryExport

    role_arn = "arn:aws:iam::123456789012:role/rayito-sandbox-caps"

    sbx = Sandbox.create(
        "rayito-base-caps",
        execution_role_arn=role_arn,
        telemetry=TelemetryExport(
            interval_s=60,
            service_name="agente",
            auth=OtlpAuth.execution_role(),
        ),
    )
    sbx.commands.run("python agent.py")
    status = sbx.get_telemetry_status()
    print(status.exported, status.dropped, status.last_error_class)
    sbx.kill()
    ```

=== "Python (async)"

    ```python
    import asyncio

    from rayito import AsyncSandbox, OtlpAuth, TelemetryExport

    role_arn = "arn:aws:iam::123456789012:role/rayito-sandbox-caps"


    async def main() -> None:
        sbx = await AsyncSandbox.create(
            "rayito-base-caps",
            execution_role_arn=role_arn,
            telemetry=TelemetryExport(auth=OtlpAuth.execution_role()),
        )
        await sbx.commands.run("python agent.py")
        print(await sbx.get_telemetry_status())
        await sbx.kill()


    asyncio.run(main())
    ```

=== "TypeScript"

    ```ts
    import { OtlpAuth, Sandbox, TelemetryExport } from "rayito";

    const roleArn = "arn:aws:iam::123456789012:role/rayito-sandbox-caps";

    await using sbx = await Sandbox.create({
      template: "rayito-base-caps",
      executionRoleArn: roleArn,
      telemetry: new TelemetryExport({
        intervalS: 60,
        serviceName: "agente",
        auth: OtlpAuth.executionRole(),
      }),
    });
    await sbx.commands.run("python agent.py");
    const status = await sbx.getTelemetryStatus();
    console.log(status.exported, status.dropped, status.lastErrorClass);
    ```

Con el token al portador, sólo cambia la autenticación:

=== "Python"

    ```python
    from rayito import OtlpAuth, Sandbox, TelemetryExport

    sbx = Sandbox.create(
        "rayito-base",
        telemetry=TelemetryExport(auth=OtlpAuth.bearer(secret_name="otlp-key")),
    )
    sbx.kill()
    ```

=== "TypeScript"

    ```ts
    import { OtlpAuth, Sandbox, TelemetryExport } from "rayito";

    await using sbx = await Sandbox.create({
      template: "rayito-base",
      telemetry: new TelemetryExport({ auth: OtlpAuth.bearer("otlp-key") }),
    });
    ```

## Errores y solución de problemas

| Síntoma | Causa | Qué hacer |
|---|---|---|
| `UnimplementedError` antes de `run-microvm`, nombra la variante caps | `OtlpAuth.execution_role()` sobre una imagen que no es `rayito-base-caps` (o derivada) | usa una imagen caps, o cambia a `OtlpAuth.bearer(...)` |
| `UnimplementedError` justo después de crear el sandbox (la VM ya se terminó, salvo `keep_on_failure=True`) | la imagen corre un `rayd` anterior a 0.6.0, o un `rayd` 0.6 que arrancó sin `AWS_REGION` | publica una imagen con el `rayd` del tag `rayd-v0.6.0` o posterior |
| `SecretNotFoundException` / `SecretException` al enviar la sección | el secreto `rayito/<nombre>` no existe (el prefijo se añade solo) o no tiene `SecretString` | crea `rayito/<nombre>` (`SecretStore().create(...)`) o pasa el ARN completo |
| `last_error_class="rejected"` justo tras un resume largo | el reloj del guest quedó atrasado más de 5 minutos | se corrige solo: `rayd` mide el desfase con la cabecera `Date` de AWS y reintenta; si persiste, abre un issue |
| `SandboxException: telemetry_export: role_not_permitted` (la VM ya se terminó) | `OtlpAuth.execution_role()` sin `execution_role_arn=` | pasa el execution role con `RayitoOtlpExport`, o usa `OtlpAuth.bearer(...)` |
| `last_error_class="rejected"` desde el primer lote con `OtlpAuth.bearer(...)` | CloudWatch rechaza la API key (caducada, inactiva o sin `cloudwatch:CallWithBearerToken`) | genera una nueva y actualiza el secreto (`SecretStore().update(...)`) |
| `get_telemetry_status()` siempre en cero | nunca pasaste `telemetry=`, o la sección no aplicó | revisa el resultado de `create()` (si no lanzó, aplicó) |

## Diferencias con E2B

La exportación de telemetría del sandbox de E2B es sólo Enterprise, la
configura E2B en el onboarding del cliente y no tiene API ni kwarg: aquí es
explícita y opt-in. El shim de E2B no añade ningún kwarg nuevo para esto;
`names="e2b"` es la única concesión a su convención de nombres.

## Ver también

- [Funciones opcionales](../optional-features.md)
- [Pilas opcionales (`rayito stack`)](pilas-opcionales.md)
- [OpenTelemetry (spans del SDK)](opentelemetry.md) — complementario, no lo
  mismo: aquello instrumenta tus llamadas al SDK, esto exporta métricas del
  *interior* del sandbox.
- [Métricas y listado](../observability.md)

??? info "Fuentes y mediciones"
    - Implementación: [`crates/rayd-core/src/telemetry/`](https://github.com/alejandro-cedeno-10/rayito/tree/main/crates/rayd-core/src/telemetry),
      [`crates/rayd/src/adapters/cloudwatch_otlp_sink.rs`](https://github.com/alejandro-cedeno-10/rayito/blob/main/crates/rayd/src/adapters/cloudwatch_otlp_sink.rs),
      [`clients/python/src/rayito/_telemetry_export/`](https://github.com/alejandro-cedeno-10/rayito/tree/main/clients/python/src/rayito/_telemetry_export)
      y [`clients/typescript/src/telemetry-export/`](https://github.com/alejandro-cedeno-10/rayito/tree/main/clients/typescript/src/telemetry-export).
    - Investigación: `docs/research/2026-10-e2b-out-of-scope.md` §6 (opciones
      B1/B1'), con OT1/OT9 confirmando que el endpoint OTLP de CloudWatch
      acepta SigV4 sobre `monitoring.<región>.amazonaws.com/v1/metrics` y
      que la acción `cloudwatch:PutMetricData` no se puede acotar por
      namespace.
    - Probado con dobles de `TelemetrySink`/`OtlpEncoder` en los dos SDK;
      la firma SigV4 la hace `aws-sigv4`, el mismo firmante que usa
      `aws-sdk-s3`.
    - Aceptación contra AWS real (2026-10-02, `AWS_API_NOTES.md` Q108–Q113):
      7 puntos por lote, 639 bytes (353 con gzip) y 0,01 s de CPU de `rayd`
      por minuto con `interval_s=15` (Q108); SigV4 desde `rayito-base-caps`
      exporta sin errores y sin `telemetry=` el `create()` no hace ninguna
      llamada AWS nueva (Q109); `/suspend` no se retrasa, no se pierde
      ningún punto y el primer lote tras 10 y 56 minutos suspendido sale con
      el pool reconstruido (Q110); un 403 por firma caducada trae la
      cabecera `Date` de la que `rayd` corrige su reloj (Q112); el
      `traceparent` de cada llamada llega a los logs de `rayd` (Q113). La
      autenticación por API key está verificada hasta el rechazo de una
      clave inválida: emitir una real exige crear un usuario IAM (Q111).

# Eventos de ciclo de vida y webhooks

`rayd` emite una línea firmada por cada transición de ciclo de vida
(`created`, `paused`, `resumed`, `killed`); en tu propia cuenta, un
forwarder/deliverer/reconciliador en Lambda las verifica, guarda y entrega a
tus webhooks con la firma de E2B.

!!! info "Coste y activación"
    - **Por defecto**: apagado. Sin `events=` (TypeScript: `events`) el SDK
      no construye ningún cliente de DynamoDB, Secrets Manager o
      CloudFormation, y `rayd` no emite ninguna línea de evento.
    - **Activa**: `events=LifecycleEvents(...)` en `Sandbox.create()`, junto
      con `logging="cloudwatch"` (obligatorio: el forwarder lee de ahí).
    - **Recursos y llamadas AWS**: la pila la despliegas tú
      (`infra/events-webhooks.yaml`: secreto HMAC, tabla DynamoDB on-demand
      con streams, tres Lambdas, una suscripción de CloudWatch Logs y una
      regla de EventBridge Scheduler). `secretsmanager:GetSecretValue` una
      vez (cacheado) para derivar la clave por sandbox; el forwarder y el
      deliverer hacen el resto de llamadas dentro de tu cuenta.
    - **Coste aproximado** (us-east-1, consultado 2026-09-30): ~$0,40/mes el
      secreto; DynamoDB y Lambda son on-demand/por invocación ($0 en
      reposo); ~$1,25 por millón de eventos escritos (WRU) más las lecturas
      de `get_events`; el reconciliador factura una invocación cada
      `reconciler_interval_minutes` (5 por defecto, ~$0,0000002 c/u).
    - **IAM** (credenciales de quien llama al SDK, no el rol del sandbox):
      `secretsmanager:GetSecretValue` sobre el secreto del stack
      (`EventsOperatorPolicy`, que la pila emite como salida).
    - **Cómo apagarla**: deja de pasar `events=`; `destroy()` borra el
      secreto (force-delete: cualquier webhook registrado deja de poder
      verificarse), la tabla, las tres Lambdas, la suscripción y el
      scheduler.

!!! warning "Pendiente de aceptación en AWS real; hueco de integración conocido"
    El dominio, el forwarder/deliverer/reconciliador y la fachada del SDK
    están implementados y probados con fakes. `create()` todavía no envía
    la sección de `ConfigureSandbox` de esta función (necesita conocer
    `sandbox_id` después de `run-microvm`, en un punto que vive en un
    fichero exclusivo de foundations): `events=` ya valida sus argumentos,
    pero la clave por sandbox aún no llega a `rayd` en esta versión. Ver
    ADR-020 en `ARCHITECTURE.md`.

## Cuándo usarlo

- Necesitas reaccionar a que un sandbox se creó, se pausó, se reanudó o
  murió, sin sondear `Health`/`list-microvms` tú mismo.
- Facturas por sandbox-hora, limpias recursos externos al matar un sandbox,
  o auditas el ciclo de vida por cumplimiento.
- **Cuándo no**: si te basta con `get_metrics`/`Health` bajo demanda, o con
  los eventos de datos de CloudTrail, no necesitas esta pila.

## Ejemplo rápido

Despliega la pila una vez (necesitas un bucket S3 para el código de las
Lambdas):

=== "Python"

    ```python
    from rayito import LifecycleEvents

    ev = LifecycleEvents()  # no llama a AWS
    ev.deploy(artifact_bucket="mi-bucket", log_group_name="/rayito/rayito-base")
    wh = ev.register_webhook(
        "https://hooks.example.com/rayito",
        secret_name="mi-webhook",  # ya creado con SecretStore(prefix="rayito/webhooks/")
        types=["sandbox.lifecycle.killed", "sandbox.lifecycle.paused"],
    )
    for event in ev.get_events(limit=10):
        print(event.sandbox_id, event.type, event.occurred_at_ms)
    ```

=== "TypeScript"

    ```ts
    import { LifecycleEvents } from "rayito"; // + npm i @aws-sdk/client-dynamodb @aws-sdk/client-secrets-manager

    const ev = new LifecycleEvents();
    await ev.deploy({ artifactBucket: "mi-bucket", logGroupName: "/rayito/rayito-base" });
    const webhook = await ev.registerWebhook("https://hooks.example.com/rayito", {
      secretName: "mi-webhook",
      types: ["sandbox.lifecycle.killed", "sandbox.lifecycle.paused"],
    });
    for (const event of await ev.getEvents({ limit: 10 })) {
      console.log(event.sandboxId, event.kind, event.occurredAtMs);
    }
    ```

=== "CLI"

    ```bash
    rayito events deploy --artifact-bucket mi-bucket --log-group-name /rayito/rayito-base
    rayito events webhook add https://hooks.example.com/rayito --secret-name mi-webhook --type sandbox.lifecycle.killed
    rayito events list --limit 10
    ```

## Cómo funciona

1. `rayd` emite `rayito.event.v1 <b64url(json)> <b64url(hmac)>` por stdout,
   sólo tras un `ConfigureSandbox` con la clave por sandbox (`k_sbx`), que
   deriva el SDK: `HMAC-SHA256(stack_key, "rayito.events.v1|" + sandbox_id)`.
   `rayd` nunca ve `stack_key`.
2. Una suscripción de CloudWatch Logs reenvía cada línea a un forwarder
   Lambda, que re-deriva `k_sbx`, comprueba que el `sandbox_id` coincide con
   el log stream de origen, verifica el MAC (tiempo constante) y escribe el
   evento de forma idempotente en DynamoDB (TTL 7 días).
3. Un deliverer (disparado por el stream de DynamoDB) entrega el evento a
   cada webhook suscrito a ese tipo, firmado al estilo E2B, con un guardián
   SSRF y hasta 3 reintentos.
4. Un reconciliador (`rate(5 min)`) compara `ListMicrovms` con los
   sandboxes que la tabla aún considera abiertos y sintetiza
   `killed{reason: "unknown"}` para los que ya no aparecen.

## Opciones de `LifecycleEvents`

| Python | TypeScript | Por defecto | Qué hace |
|---|---|---|---|
| `stack_name` | `stackName` | `"rayito-events-webhooks"` | nombre de la pila |
| `region` | `region` | la de la sesión | región de la pila |
| `session` | `credentials` | la sesión por defecto | credenciales de AWS |
| `deploy(artifact_bucket=, log_group_name=, reconciler_interval_minutes=)` | `deploy({ artifactBucket, logGroupName, reconcilerIntervalMinutes })` | 5 minutos | despliega la pila |
| `register_webhook(url, secret_name=, types=)` | `registerWebhook(url, { secretName, types })` | — | `types` son `sandbox.lifecycle.{created,paused,resumed,killed}` |
| `get_events(sandbox_id=, types=, limit=, order=)` | `getEvents({ sandboxId, types, limit, order })` | `limit=100`, `order="desc"` | lee directamente de tu tabla DynamoDB |

## Errores y solución de problemas

| Python | TypeScript | Cuándo | Qué hacer |
|---|---|---|---|
| `WebhookException` | `WebhookError` | registro, listado o borrado de un webhook falló, o la pila no está desplegada | revisa el nombre del secreto y la URL (debe ser `https://`); llama a `deploy()` primero |
| `InvalidArgumentException` | `InvalidArgumentError` | `events=` sin `logging="cloudwatch"`; un `type` desconocido; `limit` > 100 | corrige el argumento antes de reintentar |
| `StackException` | `StackError` | `deploy`/`destroy` de la pila falló (código `blocked`/`not_found`/`failed`) | ver [Pilas opcionales](pilas-opcionales.md) |

## Diferencias con E2B

E2B expone eventos de ciclo de vida y webhooks por su propia API REST, con
un servicio alojado por E2B. Rayito no tiene servidor: la pila (secreto,
tabla, Lambdas) vive en tu cuenta, y el shim no añade ningún método (E2B
sólo lo expone por REST, nunca por el SDK nativo). La firma de entrega sí es
compatible: un webhook ya escrito para E2B puede verificar las entregas de
Rayito sin cambios (mismas cabeceras `e2b-*` y el mismo esquema de firma).

## Ver también

- [Funciones opcionales](../optional-features.md) (la fila y el ancla `#events-webhooks`
  los añade `m15-docs-integration`)
- [Pilas opcionales](pilas-opcionales.md)
- [IAM](../operacion/iam.md)
- Plantilla: [`infra/events-webhooks.yaml`](https://github.com/alejandro-cedeno-10/rayito/blob/main/infra/events-webhooks.yaml)

??? info "Fuentes y mediciones"
    - Contrato de CloudWatch Logs/DynamoDB/Scheduler:
      `AWS_API_NOTES.md` §25, en
      [GitHub](https://github.com/alejandro-cedeno-10/rayito/blob/main/AWS_API_NOTES.md).
    - Despliegue y borrado: [`infra/README.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/infra/README.md).
    - Decisión de diseño (ConfigureSandbox, el hueco de integración
      conocido, el guardián SSRF): ADR-020 en
      [`ARCHITECTURE.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/ARCHITECTURE.md).

# Eventos de ciclo de vida y webhooks

`rayd` emite una línea firmada por cada transición de ciclo de vida
(`created`, `paused`, `resumed`, `killed`); en tu propia cuenta, un
forwarder/deliverer/reconciliador en Lambda las verifica, guarda y entrega a
tus webhooks con la firma de E2B.

!!! info "Coste y activación"
    - **Por defecto**: apagado. Sin `events=` (TypeScript: `events`) el SDK
      no construye ningún cliente de DynamoDB, Secrets Manager o
      CloudFormation, y `rayd` no emite ninguna línea de evento.
    - **Activa**: una llamada explícita a `LifecycleEvents().deploy(...)` (o
      `rayito events deploy`); después, `events=LifecycleEvents(...)` en
      `Sandbox.create()` junto con un `logging` que llegue a CloudWatch
      (`"cloudwatch"` o `{"cloudWatch": {...}}`: el forwarder lee de ahí).
    - **Recursos y llamadas AWS**: `Sandbox.create(events=...)` hace un
      `secretsmanager:GetSecretValue` de la clave del stack por instancia de
      `LifecycleEvents` (más un `cloudformation:DescribeStacks` si esa
      instancia no fue la que desplegó la pila) y manda la clave de ese
      sandbox a `rayd` en el mismo `ConfigureSandbox` que el resto de
      opciones 0.6. `deploy()` crea en tu cuenta
      (`infra/events-webhooks.yaml`) un secreto HMAC, una tabla DynamoDB
      on-demand con streams, tres Lambdas, una suscripción de CloudWatch
      Logs, dos colas SQS (las invocaciones del forwarder y las entregas que
      agotan sus reintentos) y una regla de EventBridge Scheduler. `register_webhook`/`list_webhooks`/
      `delete_webhook`/`get_events` llaman directamente a DynamoDB; el
      forwarder, el deliverer y el reconciliador hacen el resto dentro de tu
      cuenta.
    - **Coste aproximado** (us-east-1, consultado 2026-09-30): ~$0,40/mes el
      secreto; DynamoDB, Lambda y SQS son on-demand/por uso ($0 en reposo);
      ~$1,25 por millón de eventos escritos (WRU) más las lecturas de
      `get_events`; el reconciliador factura una invocación cada
      `reconciler_interval_minutes` (5 por defecto, mínimo 2,
      ~$0,0000002 c/u).
    - **IAM** (credenciales de quien llama al SDK, no el rol del sandbox):
      la política `EventsOperatorPolicy` que la pila emite como salida:
      `dynamodb:PutItem`/`DeleteItem` sólo sobre las filas de webhooks,
      `dynamodb:Query` sobre la tabla y su índice `gsi1`,
      `cloudformation:DescribeStacks` sobre la pila y
      `secretsmanager:GetSecretValue` sobre el secreto del stack.
    - **Cómo apagarla**: deja de pasar `events=`; `destroy()` borra el
      secreto (force-delete: cualquier webhook registrado deja de poder
      verificarse), la tabla, las tres Lambdas, la suscripción, las colas y
      el scheduler. Desvincula antes `EventsOperatorPolicy` de los usuarios y
      roles a los que la vinculaste: si no, CloudFormation no puede borrarla
      y la pila queda en `DELETE_FAILED`. Los secretos de cada webhook
      (`rayito/webhooks/...`), el log group de la imagen y los log groups
      `/aws/lambda/rayito-events-webhooks-*` que crean las propias Lambdas se
      conservan: bórralos aparte si ya no los quieres.

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
    # si tu organización exige etiquetas al crear recursos: --tag Owner=mi-equipo --tag Environment=dev
    rayito events webhook add https://hooks.example.com/rayito --secret-name mi-webhook --type sandbox.lifecycle.killed
    rayito events list --limit 10
    ```

Después, crea sandboxes que emitan eventos. `events=` necesita un `logging`
que llegue a CloudWatch (el forwarder lee de ahí, así que también un
`execution_role_arn` con permiso de escritura en el log group de la imagen)
y una imagen con `rayd` 0.6.0 o posterior:

=== "Python"

    ```python
    from rayito import LifecycleEvents, Sandbox

    role_arn = "arn:aws:iam::111122223333:role/rayito-sandbox"  # escribe en el log group
    events = LifecycleEvents()  # la misma pila de antes
    sbx = Sandbox.create(
        execution_role_arn=role_arn,
        logging="cloudwatch",
        events=events,
    )
    sbx.pause()
    sbx.resume()
    sbx.kill()
    # created, paused, resumed, killed (unos segundos después)
    for event in events.get_events(sandbox_id=sbx.sandbox_id, order="asc"):
        print(event.type)
    ```

    Con `AsyncSandbox.create(events=AsyncLifecycleEvents(), ...)` es igual.

=== "TypeScript"

    ```ts
    import { LifecycleEvents, Sandbox } from "rayito";

    const roleArn = "arn:aws:iam::111122223333:role/rayito-sandbox"; // escribe en el log group
    const events = new LifecycleEvents(); // la misma pila de antes
    const sbx = await Sandbox.create({ executionRoleArn: roleArn, logging: "cloudwatch", events });
    await sbx.pause();
    await sbx.resume();
    await sbx.kill();
    // created, paused, resumed, killed (unos segundos después)
    for (const event of await events.getEvents({ sandboxId: sbx.sandboxId, order: "asc" })) {
      console.log(event.kind);
    }
    ```

`create()` lee la clave del stack, deriva la de este sandbox (`k_sbx`) y la
manda a `rayd` justo después de que el agente esté listo, en el mismo
`ConfigureSandbox` que `mounts=`/`gateways=`/`telemetry=`. Si la pila no está
desplegada, la clave no se puede leer, la imagen es anterior a 0.6.0 o `rayd`
rechaza la sección, `create()` termina el sandbox (salvo `keep_on_failure`)
y relanza el error: nunca devuelve un sandbox que no vaya a emitir eventos.

## Cómo funciona

1. `rayd` emite `rayito.event.v1 <b64url(json)> <b64url(hmac)>` por stdout,
   sólo tras un `ConfigureSandbox` con la clave por sandbox (`k_sbx`), que
   deriva el SDK: `HMAC-SHA256(stack_key, "rayito.events.v1|" + sandbox_id)`.
   `rayd` nunca ve `stack_key`.
2. Una suscripción de CloudWatch Logs reenvía cada línea a un forwarder
   Lambda, que toma el `sandbox_id` del log stream de origen
   (`YYYY/MM/DD[<versión>]<microvmId>`), re-deriva con él `k_sbx` y verifica
   el MAC (tiempo constante) **antes** de leer el contenido. Después exige
   que el evento sea de ese mismo sandbox, que tenga la forma exacta que
   emite `rayd` y que no tenga más de 24 h (ni más de 5 min en el futuro), y
   lo escribe de forma idempotente en DynamoDB (TTL 7 días). Una línea
   rechazada nunca hace fallar el lote: se cuenta y se descarta. Cada
   invocación deja una línea JSON en su log con cuántas líneas aceptó y
   rechazó y por qué (`mac_invalid`, `sandbox_mismatch`, `malformed_line`,
   `stale_event`, `internal_error`) y cuántas escrituras fallaron
   (`failed_writes`); si alguna falla, la invocación falla tras escribir el
   resto y, agotados los reintentos de Lambda, el lote queda en la cola
   `ForwarderFailuresQueueUrl` de la pila. Medido: la línea
   `paused` llega a CloudWatch antes de que la VM se congele, y un evento
   tarda de 0,3 a 4 s en llegar a la tabla (10–14 s el primero, con el
   forwarder en frío).
3. Un deliverer (disparado por el stream de DynamoDB) entrega el evento a
   cada webhook suscrito a ese tipo, firmado al estilo E2B, con un guardián
   SSRF y hasta 3 intentos (sólo ante 5xx o error de red; un 4xx es la
   respuesta del receptor). Cada entrega se marca `attempting` antes de
   intentarla y `delivered`/`failed` al terminar: sólo `delivered` se salta
   si el stream la reentrega, así que un fallo no pierde la entrega. Cada
   webhook se entrega aislado: uno con la URL rota (`invalid_url`) o que
   falla de forma inesperada (`internal_error`) no impide entregar a los
   demás, y el plazo de cada intento (10 s) cubre la resolución DNS y la
   respuesta completa, así que un receptor que contesta byte a byte no lo
   alarga. Lo que agota los reintentos del stream acaba en la cola
   `DelivererFailuresQueueUrl` de la pila.
4. Un reconciliador (cada `reconciler_interval_minutes`) compara
   `ListMicrovms` con los sandboxes que la tabla aún considera abiertos y
   sintetiza `killed{reason: "unknown"}`, con la generación y la imagen de
   su último evento, para los que ya no aparecen. Es el único `killed` de
   un sandbox matado mientras estaba **pausado**: la plataforma no llama a
   `/terminate` de un MicroVM suspendido, así que ese evento llega con hasta
   un intervalo de retraso. Un sandbox que agota su `timeout` en marcha sí
   pasa por `/terminate`, y su evento llega con `kill_reason: "request"`
   (el hook no dice por qué muere la VM).

## Verificar la firma en tu receptor

Cada entrega es un `POST` con un cuerpo JSON y cuatro cabeceras, las mismas
que usa E2B. Un receptor que ya verifica webhooks de E2B acepta las de
Rayito sin cambios.

| Cabecera | Valor |
|---|---|
| `e2b-webhook-id` | el `webhook_id` que devolvió `register_webhook` |
| `e2b-delivery-id` | un id aleatorio **por intento**: cambia en cada reintento |
| `e2b-signature-version` | `v1` |
| `e2b-signature` | base64 sin `=` final de `sha256(secreto + cuerpo)` |

El secreto es el valor del secreto de Secrets Manager que nombraste en
`secret_name` (`rayito/webhooks/<nombre>`), tal cual, en UTF-8. La firma es
la de E2B, **no un HMAC**: un SHA-256 del secreto concatenado con los bytes
exactos del cuerpo. El cuerpo:

```json
{
  "event_id": "0123456789abcdef0123456789abcdef",
  "sandbox_id": "microvm-00000000-0000-0000-0000-000000000001",
  "type": "sandbox.lifecycle.killed",
  "kill_reason": "request",
  "generation": 0,
  "occurred_at_ms": 1790000000000,
  "sandbox_template_id": "arn:aws:lambda:us-east-1:123456789012:microvm-image:rayito-base",
  "sandbox_execution_id": "microvm-00000000-0000-0000-0000-000000000001#0"
}
```

`kill_reason` es `null` salvo en `killed` (`request`, `timeout` o
`unknown`, este último sólo cuando lo sintetiza el reconciliador).

=== "Python"

    ```python
    import base64
    import hashlib
    import hmac
    import json
    import os
    from http.server import BaseHTTPRequestHandler, HTTPServer

    SECRET = os.environ["WEBHOOK_SECRET"].encode()  # el valor de rayito/webhooks/mi-webhook


    def firma_valida(secret: bytes, body: bytes, signature: str) -> bool:
        expected = base64.b64encode(hashlib.sha256(secret + body).digest()).decode().rstrip("=")
        return hmac.compare_digest(expected, signature)  # (1)!


    class Receptor(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers["content-length"]))  # (2)!
            if not firma_valida(SECRET, body, self.headers.get("e2b-signature", "")):
                self.send_response(401)
                self.end_headers()
                return
            event = json.loads(body)
            print(event["type"], event["sandbox_id"], event["event_id"])  # (3)!
            self.send_response(204)
            self.end_headers()


    HTTPServer(("127.0.0.1", 8000), Receptor).serve_forever()
    ```

    1. Comparación en tiempo constante: nunca `==`.
    2. Verifica los bytes tal como llegan, antes de parsear el JSON:
       re-serializarlo cambia la firma.
    3. Deduplica por `event_id`, no por `e2b-delivery-id`: cada reintento
       trae un `e2b-delivery-id` nuevo.

=== "TypeScript"

    Con Node.js, sin dependencias:

    ```ts
    import { createHash, timingSafeEqual } from "node:crypto";
    import { createServer } from "node:http";

    const secret = process.env.WEBHOOK_SECRET ?? ""; // el valor de rayito/webhooks/mi-webhook

    function firmaValida(secret: string, body: Buffer, signature: string): boolean {
      const expected = createHash("sha256").update(secret).update(body).digest("base64").replace(/=+$/, "");
      const a = Buffer.from(expected);
      const b = Buffer.from(signature);
      return a.length === b.length && timingSafeEqual(a, b);
    }

    createServer((req, res) => {
      const chunks: Buffer[] = [];
      req.on("data", (chunk: Buffer) => chunks.push(chunk));
      req.on("end", () => {
        const body = Buffer.concat(chunks); // los bytes exactos, antes de JSON.parse
        const signature = req.headers["e2b-signature"];
        if (typeof signature !== "string" || !firmaValida(secret, body, signature)) {
          res.writeHead(401).end();
          return;
        }
        const event = JSON.parse(body.toString("utf8")) as { type: string; event_id: string };
        console.log(event.type, event.event_id); // deduplica por event_id
        res.writeHead(204).end();
      });
    }).listen(8000, "127.0.0.1");
    ```

El deliverer sólo entrega a URLs `https://` y nunca sigue redirecciones:
pon el receptor detrás de tu terminación TLS (un balanceador, API Gateway o
una URL de función de Lambda). Cómo trata tu respuesta:

| Respuesta | Qué hace el deliverer |
|---|---|
| `2xx` | entregado; no vuelve a intentarlo |
| `5xx`, error de red o más de 10 s sin respuesta | lo reintenta, hasta 3 intentos con espera de 0,5 s y 1 s |
| `3xx` o `4xx` (por ejemplo `401` por firma inválida) | definitivo: no reintenta |

Responde rápido y procesa el evento después: el reintento sólo protege ante
fallos tuyos transitorios. Una entrega que agota sus tres intentos queda
marcada `failed` y no se vuelve a enviar sola, pero el evento sigue en
`get_events()` durante 7 días: un receptor que estuvo caído puede ponerse al
día leyéndolos.

## Opciones de `LifecycleEvents`

| Python | TypeScript | Por defecto | Qué hace |
|---|---|---|---|
| `stack_name` | `stackName` | `"rayito-events-webhooks"` | nombre de la pila |
| `region` | `region` | la de la sesión | región de la pila |
| `session` | `credentials` | la sesión por defecto | credenciales de AWS |
| `deploy(artifact_bucket=, log_group_name=, reconciler_interval_minutes=, tags=)` | `deploy({ artifactBucket, logGroupName, reconcilerIntervalMinutes, tags })` | 5 minutos (mínimo 2); sin etiquetas | despliega la pila; `tags` se propagan a sus recursos. El código de las Lambdas se sube a `rayito/stacks/events-webhooks/<sha256>.zip` del bucket, que debe ser de tu cuenta (`ExpectedBucketOwner`); si ya hay un objeto en esa clave, el SDK compara su contenido y lo sobrescribe si no es el suyo |
| `register_webhook(url, secret_name=, types=)` | `registerWebhook(url, { secretName, types })` | — | `url` es una URL `https://` que el deliverer pueda alcanzar (host DNS válido o IP, puerto 1–65535); `types` son `sandbox.lifecycle.{created,paused,resumed,killed}` |
| `get_events(sandbox_id=, types=, limit=, order=)` | `getEvents({ sandboxId, types, limit, order })` | `limit=100` (1–100), `order="desc"` | lee directamente de tu tabla DynamoDB; filtra `types` en DynamoDB y pagina hasta reunir `limit` |

## Errores y solución de problemas

| Python | TypeScript | Cuándo | Qué hacer |
|---|---|---|---|
| `WebhookException` | `WebhookError` | una llamada a DynamoDB o Secrets Manager falló (`aws_code`/`awsCode` trae sólo el código de AWS, nunca ARNs ni la cuenta), o la pila no está desplegada | revisa los permisos de `EventsOperatorPolicy`; llama a `deploy()` primero |
| `InvalidArgumentException` | `InvalidArgumentError` | `events=` que no es un `LifecycleEvents`, o sin `logging` con CloudWatch; un `type` desconocido; `limit` fuera de 1–100; una URL que no es `https://` o que el deliverer no podría alcanzar (puerto fuera de rango, host inválido) | corrige el argumento antes de reintentar |
| `UnimplementedError` | `UnimplementedError` | `Sandbox.create(events=...)` sobre una imagen anterior a 0.6.0 (el sandbox se termina) | usa una imagen publicada con `rayd` 0.6.0 o posterior |
| `WebhookException` (desde `create()`) | `WebhookError` | `Sandbox.create(events=...)` sin la pila desplegada, o sin permiso para leer su clave (el sandbox se termina) | despliega la pila o vincula `EventsOperatorPolicy` a quien llama |
| `StackException` | `StackError` | `deploy`/`destroy` de la pila falló (código `blocked`/`not_found`/`failed`) | ver [Pilas opcionales](pilas-opcionales.md) |
| `StackException` (`failed`, la pila en `ROLLBACK_COMPLETE`) | `StackError` | una SCP o política de etiquetas de tu organización denegó `sqs:CreateQueue`/`lambda:CreateFunction` sin ciertas etiquetas | borra la pila fallida (`destroy()`) y repite `deploy(tags={...})` (CLI: `--tag K=V`) con las claves y valores que exige tu organización |
| `StackException` (`failed`, la pila en `DELETE_FAILED`) | `StackError` | `destroy()` con `EventsOperatorPolicy` aún vinculada a un usuario o rol | desvincula la política y repite `destroy()` |
| `InvalidArgumentException` desde `run-microvm` ("Logging cannot be enabled without providing executionRoleArn") | `InvalidArgumentError` | `logging="cloudwatch"` sin `execution_role_arn` | pasa un rol de ejecución con permiso de escritura en el log group de la imagen |
| ningún evento en la tabla | — | el log group de `deploy(log_group_name=)` no es el de la imagen, o el forwarder rechaza las líneas | mira la línea JSON del forwarder (`rejected_by_reason`) en su log |

## Seguridad del receptor

- **Verifica la firma y deduplica por `event_id`.** La firma de E2B
  (`e2b-signature` = base64 de `sha256(secreto + cuerpo)`) no lleva marca de
  tiempo: una entrega capturada se puede reenviar más tarde con la misma
  firma. Guarda los `event_id` ya procesados y descarta los repetidos;
  `e2b-delivery-id` cambia en cada intento y no sirve para eso.
- **Usa un secreto largo y aleatorio** (32 bytes o más, por ejemplo
  `secrets.token_urlsafe(32)` o `randomBytes(32).toString("base64url")`):
  una sola entrega observada permite probar secretos débiles sin conexión.
- **Los secretos de firma viven bajo `rayito/webhooks/`** y nunca llegan a
  un sandbox: `RayitoSecretsReader` los deniega y el SDK rechaza pasarlos
  por `secrets=`. Un sandbox con ese secreto podría falsificar entregas.
- **La URL del webhook se guarda en claro** en la tabla (con el cifrado por
  defecto de DynamoDB) y `list_webhooks()` la devuelve: si tu receptor
  lleva una credencial en la ruta (Slack, Discord), trátala como un dato
  que puede leer quien tenga `EventsOperatorPolicy`.

## Diferencias con E2B

E2B expone eventos de ciclo de vida y webhooks por su propia API REST, con
un servicio alojado por E2B. Rayito no tiene servidor: la pila (secreto,
tabla, Lambdas) vive en tu cuenta, y el shim no añade ningún método (E2B
sólo lo expone por REST, nunca por el SDK nativo). La firma de entrega sí es
compatible: un webhook ya escrito para E2B puede verificar las entregas de
Rayito sin cambios (mismas cabeceras `e2b-*` y el mismo esquema de firma).

## Ver también

- [Funciones opcionales](../optional-features.md)
- [Pilas opcionales](pilas-opcionales.md)
- [IAM](../operacion/iam.md)
- Plantilla: [`infra/events-webhooks.yaml`](https://github.com/alejandro-cedeno-10/rayito/blob/main/infra/events-webhooks.yaml)

??? info "Fuentes y mediciones"
    - Contrato de CloudWatch Logs/DynamoDB/Scheduler:
      `AWS_API_NOTES.md` §25, en
      [GitHub](https://github.com/alejandro-cedeno-10/rayito/blob/main/AWS_API_NOTES.md).
    - Despliegue y borrado: [`infra/README.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/infra/README.md).
    - Decisión de diseño (ConfigureSandbox, la derivación de `k_sbx`, el
      guardián SSRF): ADR-020 en
      [`ARCHITECTURE.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/ARCHITECTURE.md).

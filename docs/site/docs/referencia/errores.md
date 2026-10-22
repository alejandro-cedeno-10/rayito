# Errores

Todos los errores del SDK en una tabla: el nombre en Python, el nombre en
TypeScript, el código gRPC del agente que lo produce (si viene de `rayd`),
cuándo ocurre y qué hacer.

## Jerarquía

=== "Python"

    <!-- noqa: example: árbol de clases, no es código ejecutable -->
    ```text
    SandboxException
    ├── TimeoutException
    ├── InvalidArgumentException
    ├── NotFoundException
    │   ├── FileNotFoundException
    │   ├── SandboxNotFoundException
    │   └── SecretNotFoundException   (también SecretException)
    ├── SandboxNotReadyException
    ├── SandboxStateException
    ├── SandboxLifetimeException
    ├── PoolClosedException
    ├── CommandExitException
    ├── PersistenceException
    ├── DiskFullException
    ├── TransferException
    │   └── FileUploadException
    ├── RateLimitException
    ├── GitUpstreamException
    ├── SecretException
    ├── SandboxIndexException
    │   └── IndexWriteException
    ├── MountException
    ├── VolumeException
    │   ├── VolumeMountException
    │   ├── VolumeNotFoundException
    │   └── VolumePathNotFoundException
    ├── BuildException
    ├── TemplateException
    ├── StackException
    ├── WebhookException
    ├── DashboardException
    ├── CustomDomainException
    └── AgentException
    AuthenticationException          (Exception: un problema de credenciales, no del sandbox)
    └── GitAuthException
    QuotaExceededException           (Exception)
    CapacityException                (Exception)
    UnimplementedError               (NotImplementedError: la función no existe aquí)
    └── LifecycleUnsupportedException
    AgentCoreException               (Exception: el despliegue en AgentCore, no un sandbox)
    RayitoCdkException               (Exception, en rayito.cdk: un constructo CDK que no sintetiza)
    RayitoCompatWarning              (UserWarning: un aviso, no una excepción)
    ```

=== "TypeScript"

    <!-- noqa: example: árbol de clases, no es código ejecutable -->
    ```text
    SandboxError
    ├── TimeoutError
    ├── InvalidArgumentError
    ├── NotFoundError
    │   ├── FileNotFoundError
    │   └── SandboxNotFoundError
    ├── SandboxNotReadyError
    ├── SandboxStateError
    ├── SandboxLifetimeError
    ├── PoolClosedError
    ├── CommandExitError
    ├── PersistenceError
    ├── DiskFullError
    ├── TransferError
    │   └── FileUploadError
    ├── RateLimitError
    ├── GitUpstreamError
    ├── SecretError
    │   └── SecretNotFoundError
    ├── SandboxIndexError
    │   └── IndexWriteError
    ├── MountError
    ├── VolumeError
    │   ├── VolumeMountError
    │   ├── VolumeNotFoundError
    │   └── VolumePathNotFoundError
    ├── BuildError
    ├── TemplateError
    ├── StackError
    ├── WebhookError
    ├── DashboardError
    ├── CustomDomainError
    └── AgentError
    AuthenticationError              (Error)
    └── GitAuthError
    QuotaExceededError               (Error)
    CapacityError                    (Error)
    UnimplementedError               (Error)
    └── LifecycleUnsupportedError
    AgentCoreError                   (Error)
    RayitoCdkError                   (Error, en rayito/cdk)
    ```

    `instanceof` funciona en ESM y en CommonJS. En TypeScript
    `SecretNotFoundError` no puede ser además un `NotFoundError` (herencia
    simple); en Python `SecretNotFoundException` es las dos cosas.

Captura `SandboxException` / `SandboxError` para "algo falló en el
sandbox"; `AuthenticationException`, `QuotaExceededException` y
`CapacityException` quedan fuera a propósito: dicen algo de tu cuenta o de
AWS, no del sandbox. Tampoco lo es `AgentCoreException` /
`AgentCoreError`, la del [despliegue en AgentCore](#agentcore), que no
es atribuible a un sandbox, ni `RayitoCdkException` / `RayitoCdkError`, la
de los [constructos CDK](#cdk), que sale al sintetizar. Un error **dentro de `run_code`** nunca es una
excepción: llega en `execution.error`.

## Tabla completa

| Python | TypeScript | gRPC / origen | Cuándo | Qué hacer |
|---|---|---|---|---|
| `InvalidArgumentException` | `InvalidArgumentError` | `INVALID_ARGUMENT`, `FAILED_PRECONDITION`; `ValidationException` de AWS; o el SDK antes de llamar | argumento inválido: `envs` + `metadata` > 4096 caracteres, `timeout` fuera de rango, `language` y `context` a la vez, `envs` repetidos en `secrets`, un peer opcional de TypeScript sin instalar | lee el mensaje: dice qué argumento y por qué |
| `TimeoutException` | `TimeoutError` | `DEADLINE_EXCEEDED`; `sandbox_timeout` | venció el `timeout` de un comando, el plazo de una llamada o el plazo del sandbox | sube el `timeout`; para el plazo del sandbox, [`set_timeout()`](../lifecycle.md) |
| `NotFoundException` | `NotFoundError` | `NOT_FOUND`, `OUT_OF_RANGE`; `not_found` en un stream | proceso, contexto o checkpoint que no existe | — |
| `FileNotFoundException` | `FileNotFoundError` | `NOT_FOUND` en `files` | el fichero no existe | comprueba la ruta (absoluta o relativa al `HOME`) |
| `SandboxNotFoundException` | `SandboxNotFoundError` | `ResourceNotFoundException` del plano de control | el sandbox no existe o ya terminó (también por llegar a su `timeout`) | crea uno nuevo |
| `SandboxNotReadyException` | `SandboxNotReadyError` | el SDK | el agente no estuvo listo en `ready_timeout` (90 s) | `rayito doctor`; `keep_on_failure=True` para inspeccionarlo |
| `SandboxStateException` | `SandboxStateError` | `UNAVAILABLE` (puerta de fase), `suspending` en un stream; `ConflictException` del plano de control | el sandbox está suspendiéndose, reanudándose o terminando | reintenta en unos segundos |
| `SandboxLifetimeException` | `SandboxLifetimeError` | el SDK | `timeout` / `max_lifetime` por encima de 28 800 s | no se puede pasar de 8 h: [Persistencia](../persistence.md) |
| `PoolClosedException` | `PoolClosedError` | el SDK | `take()` sobre un pool sin arrancar o cerrado | `start()` / `with` |
| `CommandExitException` | `CommandExitError` | el proceso | un comando salió con código distinto de cero (`exit_code`, `stdout`, `stderr`) | captúrala si el fallo es esperable |
| `PersistenceException` | `PersistenceError` | el agente (`code`) | checkpoint o restore fallido: `permission_denied`, `failed_precondition`, `internal`, `interrupted`, `unimplemented`, `resource_exhausted` | ver [Persistencia: errores](../persistence.md#errores) |
| `DiskFullException` | `DiskFullError` | `RESOURCE_EXHAUSTED` (disco: `disk_reserve`, `disk_full`) | el disco del sandbox está lleno, también a mitad de una transferencia | borra ficheros o usa una imagen más grande |
| `TransferException` (`code`, `reason`) | `TransferError` (`code`, `reason`) | el agente | una exportación a S3 terminó `FAILED`/`CANCELLED` con un `code` sin excepción propia (ver [Transferencias](#transferencias-por-s3)) | lee `reason`; revisa permisos y región del bucket de transferencias |
| `FileUploadException` (`code`, `reason`) | `FileUploadError` (`code`, `reason`) | el agente | una importación desde S3 (`upload_url`, escritura grande) terminó igual, sin excepción propia: `checksum_mismatch`, `s3_unavailable`, `destination_rejected`… | ver [Ficheros y S3](../files.md) |
| `RateLimitException` | `RateLimitError` | `RESOURCE_EXHAUSTED`; `ThrottlingException` de AWS (`retry_after`/`retryAfter`) | más de 256 procesos/PTYs, 8 contextos o 16 transferencias; cuota TPS de la API | libera recursos; espacia las llamadas |
| `AuthenticationException` | `AuthenticationError` | `UNAUTHENTICATED`, `PERMISSION_DENIED`; 403 del proxy; `AccessDeniedException` de AWS | access token incorrecto, token del proxy rechazado (`proxy_rejected`), credenciales de AWS caducadas | revisa el token guardado; `aws sso login` |
| `GitAuthException` | `GitAuthError` | `git` | el remoto pidió credenciales | pasa `username` y `password` (un token) |
| `GitUpstreamException` | `GitUpstreamError` | `git` | `push`/`pull` sin rama de seguimiento | pasa `remote` y `branch`, o configura la rama de seguimiento |
| `SecretException` | `SecretError` | Secrets Manager | permiso que falta, ya existe, choque de versión, secreto binario | lee `aws_code`; ver [Secretos](../secrets.md#errores) |
| `SecretNotFoundException` | `SecretNotFoundError` | Secrets Manager | el secreto no existe o se está borrando | crea el secreto; ojo a las mayúsculas |
| `SandboxIndexException` | `SandboxIndexError` | DynamoDB | `BatchGetItem` falló o quedaron claves sin leer | revisa `RayitoIndexReader` y la tabla |
| `IndexWriteException` | `IndexWriteError` | DynamoDB | `PutItem` falló al crear con `index=` | revisa `RayitoIndexWriter`; el sandbox se terminó |
| `QuotaExceededException` (`quota_code`) | `QuotaExceededError` (`quotaCode`) | `ServiceQuotaExceededException` del plano de control | cuota de la cuenta agotada (memoria, MicroVMs) | mata sandboxes o pide aumento de cuota |
| `CapacityException` | `CapacityError` | `InsufficientCapacityException` del plano de control | AWS sin capacidad momentánea | reintenta con backoff |
| `UnimplementedError` | `UnimplementedError` | `UNIMPLEMENTED`; el SDK | la función no existe en esta imagen (actualízala) o en la plataforma (`fork`, snapshots…) | lee `feature`, `reason` y `doc` |
| `VolumeException` | `VolumeError` | el SDK (EFS) | `VolumeStore.create/get/list/destroy` falló (IAM, límite de access points, sistema de ficheros no disponible), o `create(volumes=)` no encontró ningún mount target `available` antes de lanzar | revisa el permiso o el estado del sistema de ficheros |
| `VolumeNotFoundException` | `VolumeNotFoundError` | el SDK (EFS) | `VolumeStore.get`/`destroy` sobre un nombre que no existe | lista con `VolumeStore.list()` |
| `VolumePathNotFoundException` | `VolumePathNotFoundError` | — | reservada con el nombre de E2B: hoy no se lanza (las operaciones de contenido de un volumen son `UnimplementedError`) | — |
| `VolumeMountException` (`code`) | `VolumeMountError` (`code`) | el agente (`efs_volumes`) | un volumen de `volumes=` no montó (`network`, `iam_denied`, `not_found`, `tls`, `helper_missing`, `timeout`, `invalid_path`, `unknown`); el sandbox ya se terminó salvo `keep_on_failure` | [Volúmenes EFS](../funciones-opcionales/volumenes-efs.md#errores-y-solucion-de-problemas) |
| `LifecycleUnsupportedException` | `LifecycleUnsupportedError` | el SDK | plazo del servidor contra una imagen anterior a 0.3.0 | publica una imagen de la release actual |
| `MountException` | `MountError` | el agente (`code`) | un montaje de `mounts=` falló o no se asentó en 15 s: `network`, `iam_denied`, `not_found`, `not_allowed`, `invalid_path`, `helper_missing`, `timeout`, `unknown` (el sandbox se terminó) | ver [Montajes S3](../funciones-opcionales/montajes-s3.md) |
| `WebhookException` | `WebhookError` | DynamoDB, Secrets Manager (`aws_code`) | `LifecycleEvents` no pudo leer o escribir en su pila, o `create(events=)` sin la pila desplegada | revisa la política de esa llamada (`EventsLauncherPolicy`, `EventsReaderPolicy` o `EventsWebhookAdminPolicy`); ver [Eventos y webhooks](../funciones-opcionales/eventos-y-webhooks.md#errores-y-solucion-de-problemas) |
| `StackException` | `StackError` | CloudFormation (`code`) | `OptionalStacks.deploy/status/destroy` falló: `blocked` (pila en `ROLLBACK_COMPLETE`), `not_found`, `in_progress`, `failed` | ver [Pilas opcionales](../funciones-opcionales/pilas-opcionales.md#como-se-comporta) |
| `BuildException` | `BuildError` | el build de la imagen (`reason`) | `Template.build()` falló: un paso del Dockerfile (`step`, `command`, `exit_code`, `log_tail`), el `ready_cmd`, la cuota de builds o el plazo | ver [Templates](../funciones-opcionales/templates.md#errores) |
| `TemplateException` | `TemplateError` | el SDK | nombre de template inválido (1-64 `[A-Za-z0-9_-]`, sin `:tag`) | corrige el nombre; la imagen base inválida es `BuildException` con `reason="base_image_*"` |
| `DashboardException` | `DashboardError` | Service Quotas, Cost Explorer, `lambda-microvms` (`aws_code`) | `Quotas`, `UsageCost` o `Fleet` (panel de gestión) recibió un código de AWS sin excepción propia (por ejemplo `DataUnavailableException`); el mensaje sólo lleva el código | ver [Panel de gestión](../funciones-opcionales/panel-de-gestion.md#errores) |
| `CustomDomainException` | `CustomDomainError` | CloudFormation, CloudFront KVS | `CustomDomain` (experimental): deploy/status/destroy de la pila, o `expose()`/`get_host()` sin ruta válida | ver [Dominio propio](../funciones-opcionales/dominio-propio.md) |
| `SandboxException` con `output_truncated` | `SandboxError` | el agente | nadie leyó la salida de un comando en 30 s y se llenó el búfer | consume el handle o redirige a un fichero |
| `AgentException` | `AgentError` | el runtime del agente (`reason`) | `sbx.agent.run()`/`.stream()` falló: ver la tabla siguiente | lee `reason`, `session_id` y `usage`; nunca contiene el prompt ni la respuesta |
| `AgentCoreException` (`code`, `exit_code`) | `AgentCoreError` (`code`, `exitCode`) | el SDK, CloudFormation, CodeBuild, AgentCore | el despliegue en AgentCore falló | ver [la tabla de AgentCore](#agentcore) |
| `RayitoCdkException` (`code`, `fields`) | `RayitoCdkError` (`code`, `fields`) | el constructo, al sintetizar | `RayitoAgent` o `RayitoMicrovmImage` no se puede sintetizar con esas props o ese stack | ver [la tabla de CDK](#cdk) |

## Cómo se elige la excepción

El SDK decide siempre por código, nunca por el texto del mensaje, con la
misma tabla en Python y en TypeScript.

### Errores del agente (`rayd`)

| Código | Unario (status gRPC) | En un stream (`StreamError.code`) |
|---|---|---|
| `INVALID_ARGUMENT`, `FAILED_PRECONDITION` / `invalid_argument`, `failed_precondition` | `InvalidArgumentException` | `InvalidArgumentException` |
| `UNIMPLEMENTED` / `unimplemented` | `UnimplementedError` | `InvalidArgumentException` |
| `UNAUTHENTICATED`, `PERMISSION_DENIED` / `permission_denied` | `AuthenticationException` | `AuthenticationException` |
| `NOT_FOUND` / `not_found` | `FileNotFoundException` en `sbx.files`, si no `NotFoundException` | igual |
| `OUT_OF_RANGE` | `NotFoundException` | — |
| `RESOURCE_EXHAUSTED` / `resource_exhausted` | `DiskFullException` con detalle de disco, si no `RateLimitException` | igual |
| `DEADLINE_EXCEEDED` / `deadline_exceeded`, `sandbox_timeout` | `TimeoutException` | `TimeoutException` |
| `UNAVAILABLE` de la puerta de fase / `suspending` | `SandboxStateException` | `SandboxStateException` |
| `CANCELLED` (sólo lo provoca el cliente) | `SandboxException` | — |
| `output_truncated` | — | `SandboxException` con `output_truncated` en el mensaje |
| cualquier otro | `SandboxException` | `SandboxException` con el código delante |

### Errores de la API de AWS

| `Code` de AWS | Excepción |
|---|---|
| `ResourceNotFoundException` | `SandboxNotFoundException` |
| `ValidationException` | `InvalidArgumentException` |
| `AccessDeniedException` | `AuthenticationException` |
| `ThrottlingException` | `RateLimitException` (`retry_after`) |
| `ConflictException` | `SandboxStateException` |
| `ServiceQuotaExceededException` | `QuotaExceededException` (`quota_code`) |
| `InsufficientCapacityException` | `CapacityException` |
| cualquier otro | `SandboxException` con `aws_code` |

`aws_code`/`awsCode` lleva el `Code`; el mensaje y la causa pasan saneados
(nunca la firma, el token ni el mensaje crudo de AWS).

### Transferencias por S3 { #transferencias-por-s3 }

Una transferencia (`upload_url`, `download_url`, lecturas y escrituras
grandes) que termina `FAILED` o `CANCELLED` trae un `code` y un `reason`; el
mensaje empieza siempre por `"<reason>: "`.

| `code` | `reason` típicos | Excepción |
|---|---|---|
| `deadline_exceeded` | `expired` | `TimeoutException` |
| `invalid_argument` | `too_large`, `wrong_region`, `bucket_missing`, `signature_rejected`… | `InvalidArgumentException` |
| `resource_exhausted` | `disk_reserve`, `disk_full` | `DiskFullException` |
| `permission_denied` | `access_denied` | `AuthenticationException` |
| `not_found` | `no_object` | `FileNotFoundException` |
| `failed_precondition`, `unavailable`, `cancelled`, `internal` | `checksum_mismatch`, `file_shrank`, `s3_unavailable`, `cancelled`, `destination_rejected`… | `FileUploadException` al importar, `TransferException` al exportar |

## `AgentException` / `AgentError`

Desde 0.8.0. La lanza `sbx.agent.run()` (`stream()` nunca lanza por un fallo del agente:
lo emite como evento `AgentFailed`). Guía:
[Agente en el sandbox](../guias/agente-en-el-sandbox.md).

Lleva `reason`, `session_id`/`sessionId`, `usage`, `exit_code`/`exitCode` y `detail_code`/`detailCode`.
El mensaje es una tabla fija en español por `reason`; nunca el texto crudo
del proveedor, el prompt ni el contenido generado.

| `reason` | Cuándo |
|---|---|
| `model_error` | el modelo respondió con error (`detail_code` lleva sólo el nombre de la clase de error, como `APIError`; nunca el mensaje) |
| `runtime_error` | el proceso del runtime falló por otra causa (código de salida distinto de 0 sin evento `error`) |
| `runtime_missing` | el runtime (`opencode` o deepagents) no está en la imagen |
| `runtime_version_mismatch` | reservado: hoy no se emite (`AgentSpec.runtime_version` todavía no se compara) |
| `protocol_error` | una línea del protocolo no se pudo interpretar (ver también `dropped_lines` en `AgentResult`) |
| `timeout` | venció `AgentLimits.timeout_seconds`/`timeoutMs` |
| `max_steps` | se alcanzó el límite duro de pasos del SDK (al empezar el paso `max_steps + 1`); el SDK para el runtime y los procesos que lanzó |
| `token_budget` | se superó `max_total_tokens` tras un `StepFinished` (puede sobrepasarse hasta un paso completo); el SDK para el runtime y los procesos que lanzó |
| `output_limit` | reservado: hoy no se emite |
| `aborted` | `stream.abort()` / cancelación / `AbortSignal` |
| `busy` | ya hay una ejecución en curso en ese sandbox (un `run`/`stream` a la vez por sandbox en esta fase) |

Con un grafo de esquema propio invocado con `input`
([Varios grafos y entrada estructurada](../guias/despliega-tu-agente.md#varios-grafos-y-entrada-estructurada)),
`protocol_error` lleva uno de estos `detail_code`, cada uno con su mensaje:

| `detail_code` | Cuándo |
|---|---|
| `input_required` | el esquema de entrada del grafo tiene claves obligatorias (`required`) y llegó un prompt |
| `invalid_input` | `input` no cumple el esquema de entrada del grafo |
| `output_not_serializable` | la salida del grafo no se puede convertir a JSON o pasa de {{agentServiceMaxOutputBytes}} bytes |

Un `400` a una petición con `input` es `InvalidArgumentException` /
`InvalidArgumentError` con «vuelve a desplegarlo con esta versión de Rayito»: el
agente desplegado es anterior a la entrada estructurada.

Con `secret_env` o `secret_env_from` ([Secretos como variables de
entorno](../guias/secretos-como-entorno.md)), el despliegue falla en cerrado con
`reason="runtime_error"` y uno de estos `detail_code`, que nunca nombran el
secreto ni el valor: `secret_env_unavailable` (no se pudieron leer),
`secret_env_denied` (el rol o tus credenciales no pueden leer un secreto),
`secret_env_invalid` (el valor no es lo que la referencia pide, o se pasa del
tope), `secret_env_invalid_key` (falta una clave pedida, o una clave no es un
nombre de variable válido) y `secret_env_reserved` (una clave daría un nombre
reservado). En AgentCore es el código del marcador de fallo del host.

Con una app `protocol = "langgraph-api"` (experimental), un grafo que se
detiene en un `interrupt` da `protocol_error` con `detail_code`
`graph_interrupted`: el host no reanuda un human-in-the-loop
([LangGraph Platform API](../guias/despliega-tu-agente.md#langgraph-platform-api)).

## `AgentCoreException` / `AgentCoreError` { #agentcore }

Desde 0.15.0. Las lanza el despliegue en AgentCore
(`deploy_agent(target="agentcore")`, `AgentCoreFleet` y `rayito agent ... --target
agentcore`). No heredan de `SandboxException` / `SandboxError`: nada de esto es
atribuible a un sandbox (es el mismo razonamiento que en `rayito setup`). Llevan
`code` (una lista cerrada, la de la tabla) y `exit_code` / `exitCode` (el código
de salida de la CLI). El mensaje está en español y nunca lleva un ARN, una URL,
un token ni el texto crudo de AWS. Guía:
[Desplegar en AgentCore](../guias/desplegar-en-agentcore.md).

**Salida 2** (uso o entorno: se corrige antes de volver a llamar):

| `code` | Cuándo | Qué hacer |
|---|---|---|
| `unsupported_region` | la región no tiene AgentCore Runtime con builds arm64 de CodeBuild | usa una de las 21 regiones de la [guía](../guias/desplegar-en-agentcore.md#antes-de-empezar) |
| `option_not_supported` | una opción de MicroVMs en la llamada no aplica a AgentCore | quítala; el mensaje dice cuál usar en su lugar ([tabla](../guias/desplegar-en-agentcore.md#opciones-que-no-aplican)) |
| `option_requires_agentcore` | una opción de AgentCore (`--tag`, `--bedrock-model`…) con el destino `microvm` | añade `--target agentcore` o quítala |
| `target_unsupported` | `rayito agent list` o `delete` sin `--target agentcore` | usa `--target agentcore`; una flota de MicroVMs se para con `rayito agent stop` |
| `provider_unsupported` | el modelo del proyecto no es de Bedrock | AgentCore sólo cablea `bedrock`, con el rol del runtime; para otro modelo, `--external-model` ([guía](../guias/desplegar-en-agentcore.md#modelos-fuera-de-bedrock)) |
| `network_policy_unsupported` | el proyecto declara `egress` o `network` | quítalo: en modo PUBLIC AgentCore no filtra la salida |
| `name_invalid` | el nombre del agente no cabe en los nombres derivados | hasta 41 caracteres, minúsculas y dígitos con `-` o `_` sueltos; cámbialo con `[image].template` |
| `name_conflict` | ya hay un runtime con ese nombre que no es de Rayito | cambia el nombre con `[image].template`; Rayito no toca runtimes ajenos |
| `model_required` | el proyecto usa `bedrock` sin `[model] id` y no pasas `--bedrock-model` | fija `[model] id` o pásalo con `--bedrock-model` (`bedrock_models` / `bedrockModels`) |
| `image_missing` | `--no-build` y no hay una imagen con ese contenido | despliega sin `--no-build` |
| `lifecycle_invalid` | un tiempo de ciclo de vida fuera de rango, o no entero (en TypeScript, no múltiplo de 1000 ms) | entre 60 y 28 800 s |
| `lifecycle_order` | `idle_session_timeout` mayor que `max_session_lifetime` | baja el tiempo de inactividad o sube la vida máxima |
| `tags_invalid` | una etiqueta no válida o más de 50 con las tres fijas | `CLAVE=VALOR`, letras, dígitos, espacios y `_ . : / = + - @` |
| `iam_resource_invalid` | un secreto legible, un modelo de Bedrock o el modelo del proyecto con comodines (`*`, `?`), comas, o un ARN que no es de tu cuenta, región y partición | un nombre o id sin comodines, o un ARN completo de tu cuenta y región; el campo sale en el mensaje |
| `env_invalid` | más de 50 variables de entorno (con las dos de región) o un valor de más de 5000 caracteres | reduce el `env` del proyecto |
| `raw_protocol` | una app con `protocol = "raw"` | no cumple el contrato de AgentCore (`/ping` y `/invocations`) |
| `no_bucket` | no hay bucket para el contexto de build | `--bucket`, `[deploy].bucket`, `RAYITO_BUCKET` o `rayito setup` |
| `bucket_region` | el bucket está en otra región | CodeBuild sólo lee código de un bucket de su región |
| `needs_confirmation` | sin terminal o con `--json` y sin `--yes` | confirma con `--yes` |
| `delete_needs_confirmation` | lo mismo para `rayito agent delete` | confirma con `--yes` |
| `session_required` | `rayito agent stop --target agentcore` sin `--session-id` | pasa `--session-id`; para quitar el agente, `rayito agent delete` |

**Salida 1** (lo que falla en AWS):

| `code` | Cuándo | Qué hacer |
|---|---|---|
| `stack_blocked` | la pila `rayito-agentcore-<nombre>` está en un estado que no admite cambios | `rayito agent delete <nombre> --target agentcore` y vuelve a desplegar |
| `stack_failed` | la pila no se pudo desplegar, o le faltan salidas | mira sus eventos en la consola de CloudFormation |
| `image_build_failed` | el build de CodeBuild terminó mal | los logs están en el grupo de logs del build (el mensaje lo nombra) |
| `image_build_timeout` | el build sigue en curso pasado el plazo | síguelo en su grupo de logs; sube `build_timeout_seconds` |
| `build_quota` | CodeBuild rechazó el build por la cuota de builds simultáneos | espera a que acaben los que hay o pide más cuota |
| `image_too_large` | la imagen supera el máximo de AgentCore (2 GB) | reduce las dependencias del proyecto |
| `runtime_blocked` | el runtime está en `CREATE_FAILED`, `DELETING` o `DELETE_FAILED` | `rayito agent delete <nombre> --target agentcore` y vuelve a desplegar |
| `runtime_failed` | el runtime quedó en un estado fallido tras crearse o actualizarse | mira sus logs (el mensaje nombra el grupo) |
| `runtime_timeout` | el runtime sigue en curso pasado el plazo | vuelve a ejecutar el despliegue: se reanuda |
| `runtime_not_found` | no hay ningún agente de Rayito con ese nombre en la región | comprueba el nombre y la región con `rayito agent list --target agentcore` |
| `delete_failed` | no se pudo borrar un recurso | reintenta `rayito agent delete`; el mensaje dice cuál |
| `access_denied` | a tus credenciales les falta un permiso | mira los [permisos de la guía](../guias/desplegar-en-agentcore.md#antes-de-empezar); el mensaje nombra la operación y el código de AWS |
| `aws_error` | AWS rechazó una operación por otro motivo | el mensaje nombra la operación y el código de AWS |

Los errores de invocación (`invoke()`, `stream()`) no son `AgentCoreException`,
salvo los de configuración: un `429` es `RateLimitException`, una cuota de
sesiones superada es `QuotaExceededException`, un fallo del propio agente
(el contenedor respondió 4xx o 5xx) es `AgentException(reason="runtime_error")`,
y un runtime que no existe, un permiso que falta o una petición rechazada son
`AgentCoreException` (`runtime_not_found`, `access_denied`, `aws_error`).

## `RayitoCdkException` / `RayitoCdkError` { #cdk }

Desde 0.15.0. Las lanzan los constructos CDK: `RayitoCdkException` se importa de
`rayito.cdk` y `RayitoCdkError` de `rayito/cdk`. Salen en el constructor, al
sintetizar y antes de añadir ningún recurso (`release_download_failed` y los
`artifact_*` salen antes, al preparar el `MicrovmImageArtifact`). No heredan de
`SandboxException` / `SandboxError`. Llevan `code` (una lista cerrada, la de la
tabla) y `fields` (los valores que interpola el mensaje). Los errores del plan
del agente (`name_invalid`, `provider_unsupported`…) siguen siendo
[`AgentCoreException` / `AgentCoreError`](#agentcore). Guía:
[Desplegar con CDK](../guias/desplegar-con-cdk.md#errores).

| `code` | Cuándo | Qué hacer |
|---|---|---|
| `region_unresolved` | el stack no tiene una región concreta (un stack sin `env`) | fija `env.region` en el stack |
| `account_invalid` | la cuenta del stack está fijada y no es un id de 12 dígitos | corrige `env.account` o déjala sin fijar |
| `image_name_invalid` | el nombre de la imagen, con el sufijo de tamaño, no es de letras, dígitos, `-` o `_`, o pasa de 64 caracteres | acórtalo o quita los caracteres que sobran |
| `image_name_reserved` | el nombre empieza por `rayito-base` (el de `rayito setup` y `rayito image publish`) | elige otro nombre |
| `artifact_not_found` | el zip de `from_file` / `fromFile` no existe | comprueba la ruta, o genera el zip con `rayito image zip` |
| `artifact_checksum_mismatch` | el sha256 del zip no es el esperado (el que pasaste o el de `SHA256SUMS` de la release) | vuelve a descargarlo; si pasaste `sha256`, revísalo |
| `artifact_variant_mismatch` | el zip es de otra variante que `variant` | pasa la `variant` del zip, o usa el zip de esa variante |
| `artifact_efs_mismatch` | el marcador de EFS del zip no casa con `with_efs` / `withEfs` | ajusta `with_efs` / `withEfs` al zip, o usa el zip con o sin EFS |
| `option_invalid` | una opción no vale: `latest` como versión base, `with_efs` sin la variante `full` y `os_capabilities`, tamaños fuera del catálogo, memoria que no es un entero positivo o, con `sizes`, distinta de la base, `environment`, `allowed_mount_buckets` o `tags` inválidos, o el plazo de CodeBuild fuera de 5-110 minutos | el mensaje dice qué opción y por qué |
| `release_download_failed` | `download_release` / `downloadRelease` no pudo descargar el zip de la release | revisa la red y la versión; o descárgalo a mano y usa `from_file` / `fromFile` |

## Errores del shim de E2B

`rayito.e2b` (y `rayito/e2b`) exporta los nombres de `e2b.exceptions`
apuntando a las clases nativas: `NotEnoughSpaceException` /
`NotEnoughSpaceError` es `DiskFullException` / `DiskFullError`, y
`ServiceBusyException` / `ServiceBusyError` es `CapacityException` /
`CapacityError`. `Template.build()` del shim lanza las nativas
`BuildException`/`TemplateException`, y las homónimas de `rayito.e2b`
(`BuildError`/`TemplateError` de `rayito/e2b` en TypeScript) son esas
mismas clases: un `except rayito.e2b.BuildException` atrapa el build
fallido igual que `except rayito.BuildException`. Detalle:
[Diferencias con E2B](../e2b-compat.md#funciona-sin-cambios).

## Ver también

- [Solución de problemas](../operacion/solucion-de-problemas.md): por
  síntoma
- [Referencia Python: excepciones](python/excepciones.md)
- [Referencia TypeScript](typescript.md)
- [Desplegar con CDK: errores](../guias/desplegar-con-cdk.md#errores)

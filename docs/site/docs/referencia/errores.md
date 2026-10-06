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
    └── AgentException
    AuthenticationException          (Exception: un problema de credenciales, no del sandbox)
    └── GitAuthException
    QuotaExceededException           (Exception)
    CapacityException                (Exception)
    UnimplementedError               (NotImplementedError: la función no existe aquí)
    └── LifecycleUnsupportedException
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
    └── AgentError
    AuthenticationError              (Error)
    └── GitAuthError
    QuotaExceededError               (Error)
    CapacityError                    (Error)
    UnimplementedError               (Error)
    └── LifecycleUnsupportedError
    ```

    `instanceof` funciona en ESM y en CommonJS.

Captura `SandboxException` / `SandboxError` para "algo falló en el
sandbox"; `AuthenticationException`, `QuotaExceededException` y
`CapacityException` quedan fuera a propósito: dicen algo de tu cuenta o de
AWS, no del sandbox. Un error **dentro de `run_code`** nunca es una
excepción: llega en `execution.error`.

## Tabla completa

| Python | TypeScript | gRPC / origen | Cuándo | Qué hacer |
|---|---|---|---|---|
| `InvalidArgumentException` | `InvalidArgumentError` | `INVALID_ARGUMENT`, `FAILED_PRECONDITION`; o el SDK antes de llamar | argumento inválido: `envs` + `metadata` > 4096 caracteres, `timeout` fuera de rango, `language` y `context` a la vez, `envs` repetidos en `secrets`, un peer opcional de TypeScript sin instalar | lee el mensaje: dice qué argumento y por qué |
| `TimeoutException` | `TimeoutError` | `DEADLINE_EXCEEDED`; `sandbox_timeout` | venció el `timeout` de un comando, el plazo de una llamada o el plazo del sandbox | sube el `timeout`; para el plazo del sandbox, [`set_timeout()`](../lifecycle.md) |
| `NotFoundException` | `NotFoundError` | `NOT_FOUND`, `OUT_OF_RANGE` | proceso, contexto o checkpoint que no existe | — |
| `FileNotFoundException` | `FileNotFoundError` | `NOT_FOUND` en `files` | el fichero no existe | comprueba la ruta (absoluta o relativa al `HOME`) |
| `SandboxNotFoundException` | `SandboxNotFoundError` | plano de control | el sandbox no existe o ya terminó (también por llegar a su `timeout`) | crea uno nuevo |
| `SandboxNotReadyException` | `SandboxNotReadyError` | el SDK | el agente no estuvo listo en `ready_timeout` (90 s) | `rayito doctor`; `keep_on_failure=True` para inspeccionarlo |
| `SandboxStateException` | `SandboxStateError` | `UNAVAILABLE` (puerta de fase); plano de control | el sandbox está suspendiéndose, reanudándose o terminando | reintenta en unos segundos |
| `SandboxLifetimeException` | `SandboxLifetimeError` | el SDK | `timeout` / `max_lifetime` por encima de 28 800 s | no se puede pasar de 8 h: [Persistencia](../persistence.md) |
| `PoolClosedException` | `PoolClosedError` | el SDK | `take()` sobre un pool sin arrancar o cerrado | `start()` / `with` |
| `CommandExitException` | `CommandExitError` | el proceso | un comando salió con código distinto de cero (`exit_code`, `stdout`, `stderr`) | captúrala si el fallo es esperable |
| `PersistenceException` | `PersistenceError` | el agente (`code`) | checkpoint o restore fallido: `permission_denied`, `failed_precondition`, `internal`, `interrupted`, `unimplemented` | ver [Persistencia: errores](../persistence.md#errores) |
| `DiskFullException` | `DiskFullError` | `RESOURCE_EXHAUSTED` (disco) | el disco del sandbox está lleno | borra ficheros o usa una imagen más grande |
| `TransferException` | `TransferError` | el agente | una exportación a S3 falló | revisa permisos y región del bucket de transferencias |
| `FileUploadException` | `FileUploadError` | el agente (`code`) | una importación desde S3 falló: `too_large`, `expired`, `checksum_mismatch`, `disk_reserve`, `wrong_region` | ver [Ficheros y S3](../files.md) |
| `RateLimitException` | `RateLimitError` | `RESOURCE_EXHAUSTED`; `ThrottlingException` de AWS | más de 256 procesos/PTYs, 8 contextos o 16 transferencias; cuota TPS de la API | libera recursos; espacia las llamadas |
| `AuthenticationException` | `AuthenticationError` | `UNAUTHENTICATED`, `PERMISSION_DENIED`; 403 del proxy | access token incorrecto, token del proxy rechazado (`proxy_rejected`), credenciales de AWS caducadas | revisa el token guardado; `aws sso login` |
| `GitAuthException` | `GitAuthError` | `git` | el remoto pidió credenciales | pasa `username` y `password` (un token) |
| `GitUpstreamException` | `GitUpstreamError` | `git` | `push`/`pull` sin rama de seguimiento | pasa `remote` y `branch`, o configura la rama de seguimiento |
| `SecretException` | `SecretError` | Secrets Manager | permiso que falta, ya existe, choque de versión, secreto binario | lee `aws_code`; ver [Secretos](../secrets.md#errores) |
| `SecretNotFoundException` | `SecretNotFoundError` | Secrets Manager | el secreto no existe o se está borrando | crea el secreto; ojo a las mayúsculas |
| `SandboxIndexException` | `SandboxIndexError` | DynamoDB | `BatchGetItem` falló o quedaron claves sin leer | revisa `RayitoIndexReader` y la tabla |
| `IndexWriteException` | `IndexWriteError` | DynamoDB | `PutItem` falló al crear con `index=` | revisa `RayitoIndexWriter`; el sandbox se terminó |
| `QuotaExceededException` | `QuotaExceededError` | plano de control | cuota de la cuenta agotada (memoria, MicroVMs) | mata sandboxes o pide aumento de cuota |
| `CapacityException` | `CapacityError` | plano de control | AWS sin capacidad momentánea | reintenta con backoff |
| `UnimplementedError` | `UnimplementedError` | `UNIMPLEMENTED`; el SDK | la función no existe en esta imagen (actualízala) o en la plataforma (`fork`, snapshots…) | lee `feature`, `reason` y `doc` |
| `VolumeException` | `VolumeError` | el SDK (EFS) | `VolumeStore.create/get/list/destroy` falló (IAM, límite de access points, sistema de ficheros no disponible), o `create(volumes=)` no encontró ningún mount target `available` antes de lanzar | revisa el permiso o el estado del sistema de ficheros |
| `VolumeNotFoundException` | `VolumeNotFoundError` | el SDK (EFS) | `VolumeStore.get`/`destroy` sobre un nombre que no existe | lista con `VolumeStore.list()` |
| `VolumeMountException` (`code`) | `VolumeMountError` (`code`) | el agente (`efs_volumes`) | un volumen de `volumes=` no montó (`network`, `iam_denied`, `not_found`, `tls`, `helper_missing`, `timeout`, `invalid_path`, `unknown`); el sandbox ya se terminó salvo `keep_on_failure` | [Volúmenes EFS](../funciones-opcionales/volumenes-efs.md#errores-y-solucion-de-problemas) |
| `LifecycleUnsupportedException` | `LifecycleUnsupportedError` | el SDK | plazo del servidor contra una imagen anterior a 0.3.0 | publica una imagen de la release actual |
| `MountException` | `MountError` | el agente (`code`) | un montaje de `mounts=` falló o no se asentó en 15 s: `network`, `iam_denied`, `not_found`, `not_allowed`, `invalid_path`, `helper_missing`, `timeout` (el sandbox se terminó) | ver [Montajes S3](../funciones-opcionales/montajes-s3.md) |
| `WebhookException` | `WebhookError` | DynamoDB, Secrets Manager (`aws_code`) | `LifecycleEvents` no pudo leer o escribir en su pila, o `create(events=)` sin la pila desplegada | revisa la política de esa llamada (`EventsLauncherPolicy`, `EventsReaderPolicy` o `EventsWebhookAdminPolicy`); ver [Eventos y webhooks](../funciones-opcionales/eventos-y-webhooks.md#errores-y-solucion-de-problemas) |
| `StackException` | `StackError` | CloudFormation (`code`) | `OptionalStacks.deploy/status/destroy` falló: `blocked` (pila en `ROLLBACK_COMPLETE`), `not_found`, `in_progress`, `failed` | ver [Pilas opcionales](../funciones-opcionales/pilas-opcionales.md#como-se-comporta) |
| `BuildException` | `BuildError` | el build de la imagen (`reason`) | `Template.build()` falló: un paso del Dockerfile (`step`, `command`, `exit_code`, `log_tail`), el `ready_cmd`, la cuota de builds o el plazo | ver [Templates](../funciones-opcionales/templates.md#errores) |
| `TemplateException` | `TemplateError` | el SDK | nombre de template inválido (1-64 `[A-Za-z0-9_-]`, sin `:tag`) | corrige el nombre; la imagen base inválida es `BuildException` con `reason="base_image_*"` |
| `SandboxException` con `output_truncated` | `SandboxError` | el agente | nadie leyó la salida de un comando en 30 s y se llenó el búfer | consume el handle o redirige a un fichero |
| `AgentException` | `AgentError` | el runtime del agente (`reason`) | `sbx.agent.run()`/`.stream()` falló: ver la tabla siguiente | lee `reason`, `session_id` y `usage`; nunca contiene el prompt ni la respuesta |

## `AgentException` / `AgentError`

En `main` desde `ai-agent-core`, sin publicar todavía (llega con 0.8.0):
[Agente en el sandbox](../guias/agente-en-el-sandbox.md).

Lleva `reason`, `session_id`/`sessionId`, `usage`, `exit_code`/`exitCode` y `detail_code`/`detailCode`.
El mensaje es una tabla fija en español por `reason`; nunca el texto crudo
del proveedor, el prompt ni el contenido generado.

| `reason` | Cuándo |
|---|---|
| `model_error` | el modelo respondió con error (`detail_code` lleva sólo el nombre de la clase de error, como `APIError`; nunca el mensaje) |
| `runtime_error` | el proceso del runtime falló por otra causa (código de salida distinto de 0 sin evento `error`) |
| `runtime_missing` | la imagen no tiene el runtime (falta el manifiesto) o su servidor residente no responde con `attach=True` |
| `runtime_version_mismatch` | la versión del manifiesto no coincide con la que `AgentSpec.runtime_version` pide |
| `protocol_error` | una línea del protocolo no se pudo interpretar (ver también `dropped_lines` en `AgentResult`) |
| `timeout` | venció `AgentLimits.timeout_seconds`/`timeoutMs` |
| `max_steps` | se alcanzó el límite duro de pasos del SDK (`max_steps + 1`) |
| `token_budget` | se superó `max_total_tokens` tras un `StepFinished` (puede sobrepasarse hasta un paso completo) |
| `output_limit` | la salida superó `max_output_bytes` |
| `aborted` | `stream.abort()` / cancelación / `AbortSignal` |
| `busy` | ya hay una ejecución en curso en ese sandbox (un `run`/`stream` a la vez por sandbox en esta fase) |

## Errores del shim de E2B

`rayito.e2b` exporta los nombres de `e2b.exceptions` apuntando a las mismas
clases: `NotEnoughSpaceException` es `DiskFullException`,
`ServiceBusyException` es `CapacityException`, y desde 0.6.0
`TemplateException` y `BuildException` se lanzan de verdad desde
`Template.build()`. Detalle:
[Diferencias con E2B](../e2b-compat.md#funciona-sin-cambios).

## Ver también

- [Solución de problemas](../operacion/solucion-de-problemas.md): por
  síntoma
- [Referencia Python: excepciones](python/excepciones.md)
- [Referencia TypeScript](typescript.md)

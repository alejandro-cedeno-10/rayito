"""Jerarquía de excepciones del SDK (misma forma que la de E2B).

`SandboxException` es la raíz de todo lo que le ocurre a un sandbox concreto.
`AuthenticationException`, `QuotaExceededException` y `CapacityException`
quedan fuera de la jerarquía, como en E2B, porque describen la cuenta o el
caller, no el estado de un sandbox.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import grpc


class SandboxException(Exception):
    """Error atribuible a un sandbox.

    `status_code` es el HTTP del plano de control, `grpc_code` el status del
    agente y `aws_code` el nombre de la excepción de botocore; los tres son
    opcionales según el origen.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        grpc_code: grpc.StatusCode | None = None,
        aws_code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.grpc_code = grpc_code
        self.aws_code = aws_code


class TimeoutException(SandboxException):
    pass


class InvalidArgumentException(SandboxException):
    pass


class NotFoundException(SandboxException):
    pass


class FileNotFoundException(NotFoundException):
    pass


class SandboxNotFoundException(NotFoundException):
    pass


class SandboxNotReadyException(SandboxException):
    """El agente no respondió a `Health` dentro de `ready_timeout`.

    `state` y `state_reason` vienen de la única llamada a `get-microvm` que se
    hace tras el timeout; si el MicroVM murió en `/run`, `state_reason` lo dice.
    """

    def __init__(
        self,
        message: str,
        *,
        state: str | None = None,
        state_reason: str | None = None,
        status_code: int | None = None,
        grpc_code: grpc.StatusCode | None = None,
        aws_code: str | None = None,
    ) -> None:
        super().__init__(message, status_code=status_code, grpc_code=grpc_code, aws_code=aws_code)
        self.state = state
        self.state_reason = state_reason


class SandboxStateException(SandboxException):
    pass


class SandboxLifetimeException(SandboxException):
    pass


class PoolClosedException(SandboxException):
    """`take()` sobre un `SandboxPool` que no fue arrancado o ya fue cerrado."""


class CommandExitException(SandboxException):
    """Un comando terminó con exit code distinto de cero. `truncated` es
    `True` cuando `stdout` o `stderr` superaron `max_output_bytes` y sólo
    conservan su final."""

    def __init__(
        self,
        message: str,
        *,
        exit_code: int,
        stdout: str = "",
        stderr: str = "",
        error: str | None = None,
        grpc_code: grpc.StatusCode | None = None,
        truncated: bool = False,
    ) -> None:
        super().__init__(message, grpc_code=grpc_code)
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.error = error
        self.truncated = truncated


class PersistenceException(SandboxException):
    """`Checkpoint`/`Restore` fallaron con un `code` cerrado: `permission_denied`
    (sin execution role, `AccessDenied` o credenciales rechazadas), `internal`
    (red, S3 5xx, tar/gzip, checksum, disco lleno), `failed_precondition`
    (otra operación en curso o región desconocida), `unimplemented` (imagen
    con un `rayd` anterior a `Checkpoint`), `interrupted` (stream cortado a
    mitad por un suspend o el proxy; no se reanuda) o `resource_exhausted`."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        grpc_code: grpc.StatusCode | None = None,
    ) -> None:
        super().__init__(message, grpc_code=grpc_code)
        self.code = code


class DiskFullException(SandboxException):
    """`Write` rechazado por espacio: `rayd` exige 256 MiB libres antes de
    cada fichero (`disk_reserve`) y mapea `ENOSPC` durante la escritura
    (`disk_full`); `grpc_code` es `RESOURCE_EXHAUSTED` en ambos casos."""


class TransferException(SandboxException):
    """Una transferencia por S3 (`download_url`, lectura grande) terminó
    `FAILED` o `CANCELLED` con un `code` sin excepción propia
    (`failed_precondition`, `unavailable`, `cancelled`, `internal` o uno
    desconocido). `reason` es el token de `rayd` (`checksum_mismatch`,
    `file_shrank`, `s3_unavailable`...); el mensaje empieza por `"<reason>: "`
    y nunca contiene una URL, un bucket, una clave ni una ruta."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        reason: str,
        grpc_code: grpc.StatusCode | None = None,
    ) -> None:
        super().__init__(message, grpc_code=grpc_code)
        self.code = code
        self.reason = reason


class FileUploadException(TransferException):
    """La importación de un `UploadTicket` o de una escritura grande terminó
    `FAILED` o `CANCELLED` con un `code` sin excepción propia (el nombre de
    E2B para una subida fallida)."""


class RateLimitException(SandboxException):
    def __init__(
        self,
        message: str,
        *,
        retry_after: float | None = None,
        status_code: int | None = None,
        grpc_code: grpc.StatusCode | None = None,
        aws_code: str | None = None,
    ) -> None:
        super().__init__(message, status_code=status_code, grpc_code=grpc_code, aws_code=aws_code)
        self.retry_after = retry_after


class AuthenticationException(Exception):
    """Credenciales o token rechazados por AWS, por el proxy o por el agente.

    `proxy_rejected` es True sólo cuando el 403 vino del proxy de AWS (JWE
    ausente, expirado o sin el puerto): el SDK reacuña y reintenta una vez.
    """

    def __init__(
        self,
        message: str,
        *,
        proxy_rejected: bool = False,
        grpc_code: grpc.StatusCode | None = None,
        aws_code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.proxy_rejected = proxy_rejected
        self.grpc_code = grpc_code
        self.aws_code = aws_code


class GitAuthException(AuthenticationException):
    """Un comando git del módulo `sandbox.git` falló por credenciales: el
    remoto pidió usuario y contraseña (`GIT_TERMINAL_PROMPT=0` no deja
    preguntar) o los rechazó. El mensaje nombra la acción de git y nunca
    contiene la URL ni las credenciales."""


class GitUpstreamException(SandboxException):
    """`git push`/`git pull` sin rama remota configurada: el mensaje explica
    cómo fijarla (`set_upstream=True` o `remote`/`branch` explícitos)."""


class SecretException(SandboxException):
    """Error de un secreto de Secrets Manager (`SecretStore`, `SecretCache`,
    `secrets=`, y el `Secret`/`AsyncSecret` del shim de E2B). El mensaje
    nunca contiene el valor ni el nombre del secreto (los nombres son
    selectores confidenciales, como en E2B); con `secrets=` nombra la clave
    de la variable de entorno. `aws_code` es el `Code` de AWS, si lo hubo.

    Divergencia con E2B: allí `SecretException` hereda de `Exception`; aquí
    de `SandboxException`, para que un `except SandboxException` de Rayito
    también la atrape."""


class SecretNotFoundException(SecretException, NotFoundException):
    """El secreto no existe o está programado para borrarse. También es la
    `NotFoundException` nativa. Nunca se guarda en `SecretCache`."""


class SandboxIndexException(SandboxException):
    """Error del índice opcional de metadatos (`DynamoDbIndex`): la
    tabla no existe, faltan permisos IAM o `BatchGetItem` dejó claves sin
    procesar tras los reintentos. Un listado con índice nunca devuelve una
    lista incompleta en silencio. El mensaje nunca repite el de AWS;
    `aws_code` es el `Code` de DynamoDB, si lo hubo."""


class IndexWriteException(SandboxIndexException):
    """`create(index=...)` no pudo escribir la fila del sandbox (`PutItem`).
    Con `on_write_failure='terminate'` (por defecto) el MicroVM ya se
    terminó, salvo `keep_on_failure=True`."""


class QuotaExceededException(Exception):
    def __init__(self, message: str, *, quota_code: str | None = None) -> None:
        super().__init__(message)
        self.quota_code = quota_code


class CapacityException(Exception):
    pass


class RayitoCompatWarning(UserWarning):
    """Aviso de compatibilidad: un kwarg de E2B que Rayito ignora
    (`api_key`, `domain`, `debug`, `api_url`, `sandbox_url`,
    `validate_api_key`, `api_headers`, `secure=False`), o la primera vez que
    un proceso usa `secrets=` (fase 1: el valor inyectado es visible para el
    código del sandbox). El aviso nombra el kwarg, nunca su valor."""


class UnimplementedError(NotImplementedError):
    """Una feature que este sandbox no puede ofrecer: falta configuración
    (`upload_url` sin `transfer=S3Staging(...)`), la imagen corre un `rayd`
    anterior o AWS no tiene la primitiva.

    No es `SandboxException` a propósito, como `NotImplementedError`: un
    `except SandboxException` no debe tragarse una feature ausente.
    `feature` y `reason` describen qué falta y por qué; `doc` es la página
    que lo explica (el shim de E2B pasa la de compatibilidad). El mensaje
    nunca contiene datos del usuario.
    """

    def __init__(self, feature: str, reason: str, doc: str | None = None) -> None:
        reference = "" if doc is None else f". Ver {doc}"
        super().__init__(f"{feature} no está disponible en Rayito: {reason}{reference}")
        self.feature = feature
        self.reason = reason
        self.doc = doc


class LifecycleUnsupportedException(UnimplementedError):
    """El agente del sandbox es anterior a 0.3.0 y no impone el timeout del
    servidor (`Health` sin `lifecycle`, o `UNIMPLEMENTED` en `SetTimeout`):
    hace falta publicar una imagen 0.3.0 o posterior o crear el sandbox sin `max_lifetime`
    ni `on_timeout`. Subclase de `UnimplementedError` sólo como discriminador
    tipado para el shim de E2B (`isinstance`, nunca el texto)."""


# --------------------------------------------------------- M15 (Rayito 0.6)
#
# Cada clase la usa la función OpenSpec que la nombra en su docstring; hasta
# entonces nada las lanza (foundations sólo las pre-crea como seam, §1(g) de
# la arquitectura de M15, para que ningún cambio de feature tenga que tocar
# este fichero compartido). `code`/`error_class` son cadenas cerradas, nunca
# el mensaje de AWS ni un identificador del usuario.


class MountException(SandboxException):
    """Un montaje de `mounts=` (m15-s3-mounts) falló o sigue sin asentarse.
    `code` es uno de `network`, `iam_denied`, `not_found`, `not_allowed`,
    `helper_missing`, `timeout`."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class VolumeException(SandboxException):
    """Error de un volumen EFS (m15-efs-volumes, experimental):
    `VolumeStore.create/get/list/destroy` o el estado de `volumes=` tras
    `/run`."""


class VolumeMountException(VolumeException):
    """Un volumen de `volumes=` (m15-efs-volumes) no se montó: `create()`
    ya terminó el sandbox (salvo `keep_on_failure`). `code` es uno de
    `network`, `iam_denied`, `not_found`, `tls`, `helper_missing`,
    `timeout`, `invalid_path` o `unknown` (la clase que reporta `rayd`,
    nunca el mensaje del helper)."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class VolumeNotFoundException(VolumeException):
    """El `AccessPoint` del volumen no existe (`DescribeAccessPoints` vacío
    o `DeleteAccessPoint` sobre un id que ya no está)."""


class VolumePathNotFoundException(VolumeException):
    """Una operación de contenido sobre el volumen (fuera de alcance en
    0.6: `read_file`/`write_file`/... del shim de E2B no tienen plano de
    datos propio) nombra una ruta que no está bajo el volumen montado."""


class BuildException(SandboxException):
    """`Template.build` (m15-templates) falló: `reason` nombra la causa
    (`build_quota`, `aws_error` cuando AWS rechazó el build por otro motivo
    —el mensaje y `__cause__` llevan sólo el resumen saneado—,
    `ready_client_error`, `ready_server_error`, o `None` con
    `step`/`command`/`exit_code`/`log_tail` cuando falló un paso del
    Dockerfile compilado)."""

    def __init__(
        self,
        message: str,
        *,
        reason: str | None = None,
        step: int | None = None,
        command: str | None = None,
        exit_code: int | None = None,
        log_tail: str | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.step = step
        self.command = command
        self.exit_code = exit_code
        self.log_tail = log_tail


class TemplateException(SandboxException):
    """`Template` (m15-templates): un nombre o un tag inválido, o una
    imagen anterior a 0.6 pasada a `Sandbox.create()` con un `Template`."""


class StackException(SandboxException):
    """Un `OptionalStacks.deploy/status/destroy` (M15 foundations) falló.
    `code` es `blocked` (pila en `ROLLBACK_COMPLETE`, hay que borrarla
    antes), `not_found`, `in_progress` o `failed`; el mensaje nunca repite
    el de CloudFormation."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class WebhookException(SandboxException):
    """`LifecycleEvents.register_webhook/list_webhooks/delete_webhook`
    (m15-events-webhooks) falló; el mensaje nunca repite una URL ni un
    secreto."""


class GatewayException(SandboxException):
    """Un `SecretGateway` (m15-secrets-gateway) rechazó o no pudo enrutar
    una petición: `code` es `not_allowed` (método/ruta fuera de la
    allowlist), `rate_limited` o `upstream_unreachable`."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class CustomDomainException(SandboxException):
    """`CustomDomain` (m15-custom-domain) falló: deploy/status/destroy de
    la pila, o un `expose()`/`get_host()` sin ruta válida en el KVS."""

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
    def __init__(
        self,
        message: str,
        *,
        exit_code: int,
        stdout: str = "",
        stderr: str = "",
        error: str | None = None,
        grpc_code: grpc.StatusCode | None = None,
    ) -> None:
        super().__init__(message, grpc_code=grpc_code)
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.error = error


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


class QuotaExceededException(Exception):
    def __init__(self, message: str, *, quota_code: str | None = None) -> None:
        super().__init__(message)
        self.quota_code = quota_code


class CapacityException(Exception):
    pass

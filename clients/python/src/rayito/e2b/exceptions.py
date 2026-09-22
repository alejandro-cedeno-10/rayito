"""Excepciones con los nombres de `e2b.exceptions`.

Las nativas se re-exportan tal cual (son las mismas clases, no copias);
`NotEnoughSpaceException` y `TemplateException` existen para que los
`except` de un programa E2B sigan compilando, y `UnimplementedError` es el
patrón Dormice del SDK: lo que AWS no puede hacer falla nombrando la
feature y el motivo, nunca se aproxima en silencio.
"""

from __future__ import annotations

from rayito.exceptions import (
    AuthenticationException,
    CommandExitException,
    InvalidArgumentException,
    NotFoundException,
    RateLimitException,
    SandboxException,
    TimeoutException,
)

COMPAT_DOC_PATH = "docs/site/docs/e2b-compat.md"


class NotEnoughSpaceException(SandboxException):
    """Nombre de E2B para el disco lleno. Rayito nunca la lanza: un disco
    lleno llega como `SandboxException` con `grpc_code RESOURCE_EXHAUSTED`."""


class TemplateException(SandboxException):
    """Nombre de E2B para un error de template. Rayito nunca la lanza: una
    imagen inexistente es `NotFoundException` del plano de control."""


class UnimplementedError(NotImplementedError):
    """Una feature de E2B sin primitiva en Lambda MicroVMs.

    No es `SandboxException` a propósito: un `except SandboxException` de E2B
    no debe tragarse una feature ausente. `feature` y `reason` describen qué
    falta y por qué; el mensaje nunca contiene datos del usuario.
    """

    def __init__(self, feature: str, reason: str) -> None:
        super().__init__(
            f"E2B {feature} no tiene equivalente en Rayito: {reason}. Ver {COMPAT_DOC_PATH}"
        )
        self.feature = feature
        self.reason = reason


class RayitoCompatWarning(UserWarning):
    """Un kwarg de E2B que Rayito ignora (`api_key`, `domain`, `debug`,
    `proxy`, `secure=False`). El aviso nombra el kwarg, nunca su valor."""


__all__ = [
    "AuthenticationException",
    "CommandExitException",
    "InvalidArgumentException",
    "NotEnoughSpaceException",
    "NotFoundException",
    "RateLimitException",
    "RayitoCompatWarning",
    "SandboxException",
    "TemplateException",
    "TimeoutException",
    "UnimplementedError",
]

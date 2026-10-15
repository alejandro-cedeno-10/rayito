"""Una sesión de `boto3` que asume un rol de otra cuenta y renueva sus
credenciales sola (ADR-026, fase 3). Construirla no llama a STS: el primer
`AssumeRole` ocurre cuando la sesión firma su primera petición, y se repite
antes de que caduquen las credenciales. El panel local la usa para la vista
multicuenta (`rayito dashboard --account`).

Un rechazo de STS sube como `AuthenticationException` con el código de AWS y
sin su mensaje (que nombra el rol y la cuenta): la API del panel lo muestra
como `aws_access_denied`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import boto3
import botocore.session
from botocore.credentials import DeferredRefreshableCredentials
from botocore.exceptions import BotoCoreError, ClientError

from rayito._aws_sanitize import sanitize_aws_error
from rayito._limits import DASHBOARD_ASSUME_ROLE_SESSION_NAME, DASHBOARD_ASSUME_ROLE_SESSION_SECONDS
from rayito.exceptions import AuthenticationException

CREDENTIALS_METHOD = "rayito-assume-role"


class AssumedRole:
    """El `AssumeRole` de un rol, con el `ExternalId` si se pidió."""

    def __init__(
        self,
        sts: Any,
        *,
        role_arn: str,
        external_id: str | None = None,
        session_name: str = DASHBOARD_ASSUME_ROLE_SESSION_NAME,
        duration_seconds: int = DASHBOARD_ASSUME_ROLE_SESSION_SECONDS,
    ) -> None:
        self._sts = sts
        self._arguments: dict[str, Any] = {
            "RoleArn": role_arn,
            "RoleSessionName": session_name,
            "DurationSeconds": duration_seconds,
        }
        if external_id:
            self._arguments["ExternalId"] = external_id

    def fetch(self) -> dict[str, Any]:
        """Las credenciales en la forma que pide `botocore`."""
        try:
            credentials = self._sts.assume_role(**self._arguments)["Credentials"]
        except (ClientError, BotoCoreError) as exc:
            summary = sanitize_aws_error(exc, include_message=False)
            raise AuthenticationException(
                "AWS no dejó asumir el rol de la cuenta", aws_code=summary.code
            ) from None
        expiration = credentials["Expiration"]
        if not isinstance(expiration, datetime):
            raise AuthenticationException(
                "AWS devolvió credenciales del rol sin fecha de caducidad válida"
            )
        return {
            "access_key": credentials["AccessKeyId"],
            "secret_key": credentials["SecretAccessKey"],
            "token": credentials["SessionToken"],
            "expiry_time": expiration.isoformat(),
        }


def assumed_session(role: AssumedRole, *, region: str) -> boto3.session.Session:
    """Una sesión cuyas credenciales salen de `role`. `botocore` no ofrece una
    forma pública de dar credenciales renovables a una sesión: se asignan al
    campo que su cadena de proveedores ya usa."""
    core = botocore.session.get_session()
    core._credentials = DeferredRefreshableCredentials(
        refresh_using=role.fetch, method=CREDENTIALS_METHOD
    )
    return boto3.session.Session(botocore_session=core, region_name=region)

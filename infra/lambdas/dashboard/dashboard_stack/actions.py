"""El rol de acciones: se asume por llamante, tras comprobar su grupo, y sus
credenciales sólo construyen el plano de control que ejecuta la acción. Una
función tiene un solo rol de ejecución (el de sólo lectura); este otro confía
únicamente en él (`infra/dashboard.yaml`) y su sesión lleva el `sub` del
llamante en el nombre, de modo que CloudTrail también dice quién fue.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from dashboard_stack.app import ActionsUnavailable
from dashboard_stack.tokens import Caller
from rayito._dashboard._api._router import DashboardApi
from rayito._limits import DASHBOARD_STACK_SESSION_SECONDS

SESSION_NAME_PREFIX: Final = "rayito-dashboard-"
#: Límite de `RoleSessionName` de STS (2 a 64 caracteres de `[\w+=,.@-]`).
SESSION_NAME_MAX_LENGTH: Final = 64
SESSION_NAME_INVALID: Final = re.compile(r"[^\w+=,.@-]")
#: Las credenciales se renuevan un poco antes de caducar.
EXPIRY_MARGIN_SECONDS: Final = 60


def session_name(subject: str) -> str:
    return (SESSION_NAME_PREFIX + SESSION_NAME_INVALID.sub("-", subject))[:SESSION_NAME_MAX_LENGTH]


@dataclass(frozen=True)
class CachedApi:
    api: DashboardApi
    expires_at: float


def boto3_session(credentials: dict[str, Any], region: str) -> boto3.session.Session:
    return boto3.session.Session(
        aws_access_key_id=credentials["AccessKeyId"],
        aws_secret_access_key=credentials["SecretAccessKey"],
        aws_session_token=credentials["SessionToken"],
        region_name=region,
    )


class AssumedRoleActions:
    def __init__(
        self,
        *,
        sts: Any,
        role_arn: str,
        region: str,
        build_api: Callable[[boto3.session.Session], DashboardApi],
        make_session: Callable[[dict[str, Any], str], boto3.session.Session] = boto3_session,
        clock: Callable[[], float] = time.time,
        session_seconds: int = DASHBOARD_STACK_SESSION_SECONDS,
    ) -> None:
        self._sts = sts
        self._role_arn = role_arn
        self._region = region
        self._build_api = build_api
        self._make_session = make_session
        self._clock = clock
        self._session_seconds = session_seconds
        self._cache: dict[str, CachedApi] = {}

    def api_for(self, caller: Caller) -> DashboardApi:
        now = self._clock()
        self._cache = {
            subject: cached for subject, cached in self._cache.items() if cached.expires_at > now
        }
        cached = self._cache.get(caller.subject)
        if cached is not None:
            return cached.api
        try:
            response = self._sts.assume_role(
                RoleArn=self._role_arn,
                RoleSessionName=session_name(caller.subject),
                DurationSeconds=self._session_seconds,
            )
        except (ClientError, BotoCoreError):
            raise ActionsUnavailable from None
        credentials = response["Credentials"]
        api = self._build_api(self._make_session(credentials, self._region))
        expires_at = credentials["Expiration"].timestamp() - EXPIRY_MARGIN_SECONDS
        self._cache[caller.subject] = CachedApi(api, expires_at)
        return api

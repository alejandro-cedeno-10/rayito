"""Adaptador boto3 de `StackProvisioner` (M15 foundations, ADR-016).

Parámetros y formas verificados offline contra botocore 1.43.103
(`AWS_API_NOTES.md` §21): `CreateStack` (`StackName`, `TemplateBody`,
`Parameters` [{ParameterKey, ParameterValue}] o, sólo en
`UpdateStack`, [{ParameterKey, UsePreviousValue}], `Tags` [{Key, Value}],
`Capabilities`), `UpdateStack` (igual, más el `ValidationError` "No updates
are to be performed" cuando no hay cambios), `DescribeStacks` (`StackName`
-> `Stacks[0].{StackStatus, StackStatusReason, Outputs, Parameters}`, o `ValidationError`
"does not exist" si no hay pila), `DeleteStack` (idempotente: no falla sobre
una pila que no existe) y `DescribeStackEvents` (no usado todavía: `wait`
sondea `DescribeStacks`, más simple y suficiente para pilas sin recursos
anidados). `put_artifact` nunca se fía de que exista un objeto con la clave
esperada: con `ExpectedBucketOwner` (la cuenta de `sts:GetCallerIdentity`)
hace `GetObject` y compara el sha256 del contenido, y sólo si coincide no
vuelve a subirlo; si falta o no coincide, `PutObject` con `ChecksumSHA256`
(AWS_API_NOTES.md §21).
"""

from __future__ import annotations

import base64
import hashlib
import logging
import time
from collections.abc import Callable
from typing import Any, Final, Protocol

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from rayito._aws import LazyClient
from rayito._aws import aws_code as _aws_code
from rayito._aws_sanitize import sanitize_aws_error
from rayito._stacks._model import StackComponent, StackStatus
from rayito._stacks._port import DeployTarget, UpdateOutcome
from rayito.exceptions import StackException

VALIDATION_ERROR: Final = "ValidationError"
NOT_EXISTS_MARKER: Final = "does not exist"
NO_UPDATES_MARKER: Final = "No updates are to be performed"
#: `GetObject` sobre una clave que no existe (con `s3:ListBucket`; sin él,
#: S3 responde `AccessDenied` y el despliegue falla, como antes).
MISSING_OBJECT_CODES: Final = frozenset({"NoSuchKey", "404"})
#: Entre sondeos de `wait`: una pila sin recursos anidados (nuestros
#: componentes) suele terminar en segundos; medio segundo no satura
#: `DescribeStacks` y no hace esperar de más.
DEFAULT_POLL_INTERVAL_SECONDS: Final = 0.5

TERMINAL_SUCCESS_STATES: Final = frozenset({"CREATE_COMPLETE", "UPDATE_COMPLETE"})
TERMINAL_DELETED_STATES: Final = frozenset({"DELETE_COMPLETE"})
TERMINAL_FAILURE_STATES: Final = frozenset(
    {
        "CREATE_FAILED",
        "ROLLBACK_COMPLETE",
        "ROLLBACK_FAILED",
        "UPDATE_FAILED",
        "UPDATE_ROLLBACK_COMPLETE",
        "UPDATE_ROLLBACK_FAILED",
        "DELETE_FAILED",
    }
)

_LOGGER = logging.getLogger("rayito.stacks")

Clock = Callable[[], float]
Sleeper = Callable[[float], None]


class _ClientSource(Protocol):
    """Lo que el adaptador necesita de `LazyClient`: un cliente boto3 bajo
    demanda. Un `Protocol`, no `LazyClient` en concreto, para que los tests
    puedan sustituirlo por un cliente ya construido (con `Stubber`) sin
    abrir una sesión real."""

    def get(self) -> Any: ...


def _wrap(exc: BotoCoreError | ClientError) -> StackException:
    """El `StackException` de un error de botocore; quien lo lanza lo
    encadena `from sanitize_aws_error(exc)`, nunca `from exc`, para que el
    `ClientError` crudo (con la cadena canónica de un error de firma en su
    `Message`) no llegue a ningún traceback."""
    return StackException(str(sanitize_aws_error(exc)), code="failed")


def _is_missing_stack(exc: BaseException) -> bool:
    return _aws_code(exc) == VALIDATION_ERROR and NOT_EXISTS_MARKER in str(exc)


def _is_no_updates(exc: BaseException) -> bool:
    return _aws_code(exc) == VALIDATION_ERROR and NO_UPDATES_MARKER in str(exc)


class CloudFormationProvisioner:
    """`StackProvisioner` real: construido perezosamente (ningún cliente
    boto3 hasta el primer uso), testable sustituyendo `clock`/`sleep`."""

    def __init__(
        self,
        *,
        region: str | None = None,
        session: boto3.session.Session | None = None,
        clock: Clock = time.monotonic,
        sleep: Sleeper = time.sleep,
        poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
    ) -> None:
        self._cloudformation: _ClientSource = LazyClient(
            "cloudformation", region=region, session=session
        )
        self._s3: _ClientSource = LazyClient("s3", region=region, session=session)
        self._sts: _ClientSource = LazyClient("sts", region=region, session=session)
        self._clock = clock
        self._sleep = sleep
        self._poll_interval = poll_interval

    def describe(self, stack_name: str) -> StackStatus | None:
        try:
            response = self._cloudformation.get().describe_stacks(StackName=stack_name)
        except (BotoCoreError, ClientError) as exc:
            if _is_missing_stack(exc):
                return None
            raise _wrap(exc) from sanitize_aws_error(exc)
        stacks = response.get("Stacks") or []
        if not stacks:
            return None
        stack = stacks[0]
        outputs = {
            output["OutputKey"]: output["OutputValue"] for output in stack.get("Outputs") or []
        }
        parameters = {
            parameter["ParameterKey"]: parameter.get("ParameterValue", "")
            for parameter in stack.get("Parameters") or []
        }
        return StackStatus(
            name=stack_name,
            state=stack.get("StackStatus"),
            outputs=outputs,
            reason_code=stack.get("StackStatusReason"),
            parameters=parameters,
        )

    def create(
        self,
        component: StackComponent,
        *,
        stack_name: str,
        template_body: str,
        parameters: dict[str, str],
        tags: dict[str, str],
    ) -> None:
        try:
            self._cloudformation.get().create_stack(
                StackName=stack_name,
                TemplateBody=template_body,
                Parameters=_proto_parameters(parameters),
                Tags=_proto_tags(tags),
                Capabilities=list(component.capabilities),
            )
        except (BotoCoreError, ClientError) as exc:
            raise _wrap(exc) from sanitize_aws_error(exc)

    def update(
        self,
        component: StackComponent,
        *,
        stack_name: str,
        template_body: str,
        parameters: dict[str, str],
        tags: dict[str, str],
        keep_previous: tuple[str, ...] = (),
    ) -> UpdateOutcome:
        try:
            self._cloudformation.get().update_stack(
                StackName=stack_name,
                TemplateBody=template_body,
                Parameters=_proto_parameters(parameters, keep_previous),
                Tags=_proto_tags(tags),
                Capabilities=list(component.capabilities),
            )
        except ClientError as exc:
            if _is_no_updates(exc):
                return "no_changes"
            raise _wrap(exc) from sanitize_aws_error(exc)
        except BotoCoreError as exc:
            raise _wrap(exc) from sanitize_aws_error(exc)
        return "changed"

    def delete(self, stack_name: str) -> None:
        try:
            self._cloudformation.get().delete_stack(StackName=stack_name)
        except (BotoCoreError, ClientError) as exc:
            raise _wrap(exc) from sanitize_aws_error(exc)

    def wait(self, stack_name: str, target: DeployTarget, timeout: float) -> None:
        deadline = self._clock() + timeout
        while True:
            status = self.describe(stack_name)
            if target == "deleted" and (status is None or status.state in TERMINAL_DELETED_STATES):
                return
            if (
                target == "deployed"
                and status is not None
                and status.state in TERMINAL_SUCCESS_STATES
            ):
                return
            if status is not None and status.state in TERMINAL_FAILURE_STATES:
                raise StackException(
                    f"la pila {stack_name!r} terminó en {status.state}", code="failed"
                )
            if self._clock() >= deadline:
                raise StackException(
                    f"tiempo agotado esperando la pila {stack_name!r} ({target})",
                    code="in_progress",
                )
            self._sleep(self._poll_interval)

    def put_artifact(self, bucket: str, key: str, data: bytes) -> None:
        """Sube `data` salvo que el objeto ya tenga exactamente ese
        contenido. Cada llamada a S3 lleva `ExpectedBucketOwner`: un bucket
        de otra cuenta (un nombre ocupado por un tercero) falla en vez de
        recibir o servir el código de las Lambdas."""
        client = self._s3.get()
        located = {"Bucket": bucket, "Key": key, "ExpectedBucketOwner": self._account_id()}
        digest = hashlib.sha256(data).digest()
        stored = self._stored_digest(client, located)
        if stored == digest:
            return
        if stored is not None:
            _LOGGER.warning(
                "el artefacto del componente ya subido no coincide con el del SDK: se sobrescribe"
            )
        try:
            client.put_object(
                **located, Body=data, ChecksumSHA256=base64.b64encode(digest).decode("ascii")
            )
        except (BotoCoreError, ClientError) as exc:
            raise _wrap(exc) from sanitize_aws_error(exc)

    def _stored_digest(self, client: Any, located: dict[str, str]) -> bytes | None:
        """sha256 del objeto ya subido, o `None` si no existe. La clave es el
        sha256 del contenido: uno distinto sólo puede ser un objeto
        manipulado o corrupto, y `put_artifact` lo sobrescribe."""
        try:
            stored = client.get_object(**located)["Body"].read()
        except ClientError as exc:
            if _aws_code(exc) in MISSING_OBJECT_CODES:
                return None
            raise _wrap(exc) from sanitize_aws_error(exc)
        except BotoCoreError as exc:
            raise _wrap(exc) from sanitize_aws_error(exc)
        return hashlib.sha256(stored).digest()

    def _account_id(self) -> str:
        try:
            return str(self._sts.get().get_caller_identity()["Account"])
        except (BotoCoreError, ClientError) as exc:
            raise _wrap(exc) from sanitize_aws_error(exc)

    def failure_reason(self, stack_name: str) -> str | None:
        status = self.describe(stack_name)
        return None if status is None else status.reason_code


def _proto_parameters(
    parameters: dict[str, str], keep_previous: tuple[str, ...] = ()
) -> list[dict[str, str | bool]]:
    entries: dict[str, dict[str, str | bool]] = {
        key: {"ParameterKey": key, "ParameterValue": value} for key, value in parameters.items()
    }
    for key in keep_previous:
        entries[key] = {"ParameterKey": key, "UsePreviousValue": True}
    return [entries[key] for key in sorted(entries)]


def _proto_tags(tags: dict[str, str]) -> list[dict[str, str]]:
    return [{"Key": key, "Value": value} for key, value in sorted(tags.items())]

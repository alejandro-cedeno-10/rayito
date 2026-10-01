"""Adaptador boto3 de `StackProvisioner` (M15 foundations, ADR-016).

Parámetros y formas verificados offline contra botocore 1.43.103
(`AWS_API_NOTES.md` §21): `CreateStack` (`StackName`, `TemplateBody`,
`Parameters` [{ParameterKey, ParameterValue}], `Tags` [{Key, Value}],
`Capabilities`), `UpdateStack` (igual, más el `ValidationError` "No updates
are to be performed" cuando no hay cambios), `DescribeStacks` (`StackName`
-> `Stacks[0].{StackStatus, StackStatusReason, Outputs}`, o `ValidationError`
"does not exist" si no hay pila), `DeleteStack` (idempotente: no falla sobre
una pila que no existe) y `DescribeStackEvents` (no usado todavía: `wait`
sondea `DescribeStacks`, más simple y suficiente para pilas sin recursos
anidados). El cliente `s3` de `put_artifact` sólo hace `HeadObject` +
`PutObject`, igual que el resto del SDK.
"""

from __future__ import annotations

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
HEAD_NOT_FOUND_CODE: Final = "404"
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

Clock = Callable[[], float]
Sleeper = Callable[[float], None]


class _ClientSource(Protocol):
    """Lo que el adaptador necesita de `LazyClient`: un cliente boto3 bajo
    demanda. Un `Protocol`, no `LazyClient` en concreto, para que los tests
    puedan sustituirlo por un cliente ya construido (con `Stubber`) sin
    abrir una sesión real."""

    def get(self) -> Any: ...


def _wrap(exc: BotoCoreError | ClientError) -> StackException:
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
        self._clock = clock
        self._sleep = sleep
        self._poll_interval = poll_interval

    def describe(self, stack_name: str) -> StackStatus | None:
        try:
            response = self._cloudformation.get().describe_stacks(StackName=stack_name)
        except (BotoCoreError, ClientError) as exc:
            if _is_missing_stack(exc):
                return None
            raise _wrap(exc) from exc
        stacks = response.get("Stacks") or []
        if not stacks:
            return None
        stack = stacks[0]
        outputs = {
            output["OutputKey"]: output["OutputValue"] for output in stack.get("Outputs") or []
        }
        return StackStatus(
            name=stack_name,
            state=stack.get("StackStatus"),
            outputs=outputs,
            reason_code=stack.get("StackStatusReason"),
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
            raise _wrap(exc) from exc

    def update(
        self,
        component: StackComponent,
        *,
        stack_name: str,
        template_body: str,
        parameters: dict[str, str],
        tags: dict[str, str],
    ) -> UpdateOutcome:
        try:
            self._cloudformation.get().update_stack(
                StackName=stack_name,
                TemplateBody=template_body,
                Parameters=_proto_parameters(parameters),
                Tags=_proto_tags(tags),
                Capabilities=list(component.capabilities),
            )
        except ClientError as exc:
            if _is_no_updates(exc):
                return "no_changes"
            raise _wrap(exc) from exc
        except BotoCoreError as exc:
            raise _wrap(exc) from exc
        return "changed"

    def delete(self, stack_name: str) -> None:
        try:
            self._cloudformation.get().delete_stack(StackName=stack_name)
        except (BotoCoreError, ClientError) as exc:
            raise _wrap(exc) from exc

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
        client = self._s3.get()
        try:
            client.head_object(Bucket=bucket, Key=key)
            return
        except ClientError as exc:
            if _aws_code(exc) != HEAD_NOT_FOUND_CODE:
                raise _wrap(exc) from exc
        try:
            client.put_object(Bucket=bucket, Key=key, Body=data)
        except (BotoCoreError, ClientError) as exc:
            raise _wrap(exc) from exc

    def failure_reason(self, stack_name: str) -> str | None:
        status = self.describe(stack_name)
        return None if status is None else status.reason_code


def _proto_parameters(parameters: dict[str, str]) -> list[dict[str, str]]:
    return [
        {"ParameterKey": key, "ParameterValue": value} for key, value in sorted(parameters.items())
    ]


def _proto_tags(tags: dict[str, str]) -> list[dict[str, str]]:
    return [{"Key": key, "Value": value} for key, value in sorted(tags.items())]

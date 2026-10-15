"""La vista multicuenta del panel local (ADR-026, fase 3), opt-in: cada
`--account NOMBRE=<arn de rol>` añade una cuenta que se ve con un rol de
sólo lectura (`infra/dashboard-read-role.yaml`). La cuenta del panel sigue
siendo la principal; las demás son siempre de sólo lectura, sin coste
facturado ni logs.

`AccountsApi` envuelve las `DashboardApi` y reparte cada petición por
`?account=NOMBRE` (sin él, la principal). La SPA sólo ve etiquetas: el ARN del
rol y el id de la cuenta no salen por la API.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from rayito._dashboard._api._contract import (
    API_PREFIX,
    HTTP_OK,
    ApiBackend,
    ApiRequest,
    ApiResponse,
)
from rayito._dashboard._api._router import ApiError, error_response, is_action_route
from rayito._limits import DASHBOARD_ACCOUNT_LABEL_MAX_CHARS, DASHBOARD_ACCOUNTS_MAX
from rayito.exceptions import InvalidArgumentException

ACCOUNT_PARAM: Final = "account"
ACCOUNTS_PATH: Final = f"{API_PREFIX}/accounts"
GET: Final = "GET"
LABEL_PATTERN: Final = re.compile(
    rf"[A-Za-z0-9][A-Za-z0-9 _.-]{{0,{DASHBOARD_ACCOUNT_LABEL_MAX_CHARS - 1}}}"
)
ROLE_ARN_PATTERN: Final = re.compile(r"arn:aws[a-z-]*:iam::[0-9]{12}:role/[\w+=,.@/-]{1,512}")
SPEC_SEPARATOR: Final = "="
READ_ONLY_ACCOUNT_MESSAGE: Final = (
    "las cuentas adicionales son de sólo lectura: las acciones se piden en la cuenta principal"
)


@dataclass(frozen=True)
class AccountSpec:
    """Una cuenta adicional: la etiqueta que ve el usuario y el rol que se
    asume. `__repr__` oculta el rol: lleva el id de la cuenta."""

    label: str
    role_arn: str

    def __repr__(self) -> str:
        return f"AccountSpec(label={self.label!r})"


def parse_account_specs(values: Sequence[str]) -> tuple[AccountSpec, ...]:
    """`NOMBRE=ARN` (repetible) a especificaciones validadas. Los mensajes
    nombran la opción, nunca el valor recibido."""
    if len(values) > DASHBOARD_ACCOUNTS_MAX:
        raise InvalidArgumentException(
            f"--account: como mucho {DASHBOARD_ACCOUNTS_MAX} cuentas adicionales"
        )
    specs: list[AccountSpec] = []
    for value in values:
        label, separator, role_arn = value.partition(SPEC_SEPARATOR)
        if not separator or LABEL_PATTERN.fullmatch(label) is None:
            raise InvalidArgumentException(
                "--account: formato NOMBRE=ARN_DEL_ROL (el nombre, de hasta "
                f"{DASHBOARD_ACCOUNT_LABEL_MAX_CHARS} letras, números, espacios, «_», «.» o «-»)"
            )
        if ROLE_ARN_PATTERN.fullmatch(role_arn) is None:
            raise InvalidArgumentException("--account: el valor tras «=» debe ser el ARN de un rol")
        specs.append(AccountSpec(label, role_arn))
    labels = [spec.label.casefold() for spec in specs]
    if len(set(labels)) != len(labels):
        raise InvalidArgumentException("--account: los nombres de cuenta no se pueden repetir")
    return tuple(specs)


class AccountsApi:
    """El contrato `/api/v1` sobre varias cuentas: la principal y las
    adicionales, todas de sólo lectura salvo, si lo tiene, la principal."""

    def __init__(self, primary: ApiBackend, others: Mapping[str, ApiBackend]) -> None:
        self._primary = primary
        self._others = dict(others)

    def requires_csrf(self, request: ApiRequest) -> bool:
        return self._primary.requires_csrf(request)

    def close(self) -> None:
        self._primary.close()
        for api in self._others.values():
            api.close()

    def handle(self, request: ApiRequest) -> ApiResponse:
        if request.method == GET and request.path == ACCOUNTS_PATH:
            return ApiResponse(HTTP_OK, {"accounts": [{"name": name} for name in self._others]})
        names = request.query.get(ACCOUNT_PARAM, [])
        if not names:
            return self._primary.handle(request)
        api = self._others.get(names[0])
        if api is None:
            return error_response(ApiError("invalid_argument", "account: cuenta desconocida"))
        if is_action_route(request.method, request.path):
            return error_response(ApiError("actions_disabled", READ_ONLY_ACCOUNT_MESSAGE))
        remaining: dict[str, Any] = {k: v for k, v in request.query.items() if k != ACCOUNT_PARAM}
        return api.handle(ApiRequest(request.method, request.path, remaining, request.body))

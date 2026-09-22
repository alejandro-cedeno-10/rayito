"""Sesión de AWS de la CLI: una sesión `boto3`, sus clientes perezosos y el
plano de control compartido del SDK.

`Clients` viaja en `ctx.obj`; los tests construyen uno con clientes
envueltos en `botocore.stub.Stubber` y un plano de control falso, y lo
inyectan con `CliRunner.invoke(app, args, obj=clients)`.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import boto3
from botocore.config import Config
from botocore.exceptions import ProfileNotFound

from rayito._aws import ControlPlane, client_config, shared_control_plane
from rayito._transport import TransportSettings
from rayito._version import __version__
from rayito.sandbox_sync.main import Sandbox

SERVICE_NAMES: dict[str, str] = {
    "microvms": "lambda-microvms",
    "s3": "s3",
    "sts": "sts",
    "iam": "iam",
    "quotas": "service-quotas",
    "logs": "logs",
    "cloudformation": "cloudformation",
}
REGION_ENV_VAR = "AWS_REGION"
NO_REGION_MESSAGE = f"sin región: pasa --region o exporta {REGION_ENV_VAR}"
NO_CREDENTIALS_MESSAGE = (
    "sin credenciales de AWS: configura un perfil (--profile / AWS_PROFILE), "
    "variables de entorno o el rol de la máquina"
)


class UsageError(Exception):
    """Error de uso o de entorno (código de salida 2): región, perfil,
    credenciales o bucket ausentes. Se lanza antes de cualquier llamada a AWS."""


SandboxFactory = Callable[..., Sandbox]


def cli_client_config(user_agent_suffix: str) -> Config:
    return client_config().merge(
        Config(user_agent_extra=f"rayito/{__version__} {user_agent_suffix}")
    )


@dataclass
class Clients:
    """Clientes boto3 construidos bajo demanda sobre una sesión y una región.

    `clients` admite clientes ya construidos (los stubs de los tests);
    `control_plane_override`, `sandbox_factory` y `transport` son las
    costuras del `doctor` y de los comandos `sandbox` (un plano falso, un
    `Sandbox` falso para `--launch`, un `rayd` falso en loopback).
    """

    session: boto3.session.Session
    region: str
    user_agent_suffix: str = "cli"
    clients: dict[str, Any] = field(default_factory=dict)
    control_plane_override: ControlPlane | None = None
    sandbox_factory: SandboxFactory = Sandbox.create
    account_id_override: str | None = None
    transport: TransportSettings = field(default_factory=TransportSettings)

    def client(self, name: str) -> Any:
        if name not in self.clients:
            self.clients[name] = self.session.client(
                SERVICE_NAMES[name],
                region_name=self.region,
                config=cli_client_config(self.user_agent_suffix),
            )
        return self.clients[name]

    @property
    def microvms(self) -> Any:
        return self.client("microvms")

    @property
    def s3(self) -> Any:
        return self.client("s3")

    @property
    def sts(self) -> Any:
        return self.client("sts")

    @property
    def iam(self) -> Any:
        return self.client("iam")

    @property
    def quotas(self) -> Any:
        return self.client("quotas")

    @property
    def logs(self) -> Any:
        return self.client("logs")

    @property
    def cloudformation(self) -> Any:
        return self.client("cloudformation")

    @property
    def account_id(self) -> str:
        if self.account_id_override is None:
            self.account_id_override = str(self.sts.get_caller_identity()["Account"])
        return self.account_id_override

    @property
    def control_plane(self) -> ControlPlane:
        if self.control_plane_override is None:
            self.control_plane_override = shared_control_plane(self.session, region=self.region)
        return self.control_plane_override


def resolve_session(profile: str | None, region: str | None) -> Clients:
    """Sesión desde `--profile`/`--region` o la cadena por defecto de boto3.
    `AWS_REGION` se honra explícitamente (botocore sólo lee
    `AWS_DEFAULT_REGION` y el perfil). Sin región o sin credenciales
    resolubles es un `UsageError` antes de tocar AWS."""
    wanted_region = region or os.environ.get(REGION_ENV_VAR) or None
    try:
        session = boto3.session.Session(profile_name=profile, region_name=wanted_region)
    except ProfileNotFound as exc:
        raise UsageError(f"perfil desconocido: {profile!r} ({exc})") from exc
    resolved_region = session.region_name
    if not resolved_region:
        raise UsageError(NO_REGION_MESSAGE)
    if session.get_credentials() is None:
        raise UsageError(NO_CREDENTIALS_MESSAGE)
    return Clients(session=session, region=str(resolved_region))


class CommandContext(Protocol):
    """Lo que la CLI usa de un `typer.Context`: `obj` y `meta`."""

    obj: Any

    @property
    def meta(self) -> dict[str, Any]: ...


def clients_of(ctx: CommandContext) -> Clients:
    """`ctx.obj` inyectado por los tests o la sesión resuelta de las opciones
    globales, construida sólo cuando un comando necesita AWS (`image zip`
    no la necesita)."""
    if ctx.obj is None:
        ctx.obj = resolve_session(ctx.meta.get("profile"), ctx.meta.get("region"))
    clients: Clients = ctx.obj
    return clients


def json_mode(ctx: CommandContext) -> bool:
    return bool(ctx.meta.get("json", False))

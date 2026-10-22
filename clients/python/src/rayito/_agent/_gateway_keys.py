"""Renovación opcional de la clave de corta duración de Bedrock de las
pasarelas de una flota (`renew_gateway_keys`, ADR-034 D7, design D6).

La decisión es pura (`testdata/fleet-recipe/renewal.json`): una pasarela con
preset `bedrock` se renueva si este proceso no la renovó en los últimos
`gatewayKeyRenewAfterSeconds` y su secreto tampoco cambió en ese plazo
(`DescribeSecret.LastChangedDate`). Renovar es firmar en local una clave con
las credenciales del llamante (el generador oficial de AWS, sin llamada a
AWS) y escribir `Bearer <clave>` en el secreto (`PutSecretValue`).

Ni la clave ni el nombre del secreto aparecen en logs, errores, `repr` ni en
la receta.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, Protocol
from urllib.parse import urlsplit

from botocore.credentials import CredentialProvider
from botocore.exceptions import BotoCoreError, ClientError

from rayito._aws import LazyClient, aws_code
from rayito._aws_region import aws_session
from rayito._aws_sanitize import sanitize_aws_error
from rayito._limits import GATEWAY_KEY_EXPIRY_SECONDS, GATEWAY_KEY_RENEW_AFTER_SECONDS
from rayito._optional import require_module
from rayito._secrets import DEFAULT_SECRET_PREFIX, SecretRef, resolve_secret_id
from rayito.exceptions import InvalidArgumentException, SecretException

if TYPE_CHECKING:
    from rayito._agent._project_domain import AgentProject

#: El preset de las pasarelas cuya clave se puede renovar.
BEDROCK_PRESET: Final = "bedrock"
RENEW_NEEDS_BEDROCK: Final = "renew_gateway_keys necesita una pasarela con preset bedrock"
#: El valor que espera `bedrock_gateway`: `Bearer <clave>`.
BEARER_PREFIX: Final = "Bearer "
#: El host de Bedrock Runtime de una región (`bedrock_gateway`).
BEDROCK_RUNTIME_HOST_PREFIX: Final = "bedrock-runtime."
AMAZONAWS_HOST_SUFFIX: Final = ".amazonaws.com"
#: El paquete del generador oficial y el extra que lo instala.
TOKEN_GENERATOR_MODULE: Final = "aws_bedrock_token_generator"
TOKEN_GENERATOR_EXTRA: Final = "bedrock-keys"
RENEWAL_FEATURE: Final = "renew_gateway_keys"
#: El código propio de un permiso que falta, con el nombre de la pasarela.
PERMISSION_DENIED_CODE: Final = "permission_denied"
ACCESS_DENIED_PREFIX: Final = "AccessDenied"

TokenProvider = Callable[[str], str]


@dataclass(frozen=True)
class RenewableGateway:
    """Una pasarela `bedrock` de la flota: su nombre, el `SecretId` que lee
    en tiempo de ejecución y la región de su upstream."""

    name: str
    secret_id: str
    region: str

    def __repr__(self) -> str:
        return f"RenewableGateway(name={self.name!r}, region={self.region!r})"


def gateway_region(upstream: str) -> str | None:
    """La región de un upstream `https://bedrock-runtime.<región>.amazonaws.com`;
    `None` para cualquier otro."""
    host = urlsplit(upstream).hostname or ""
    if not host.startswith(BEDROCK_RUNTIME_HOST_PREFIX) or not host.endswith(AMAZONAWS_HOST_SUFFIX):
        return None
    region = host[len(BEDROCK_RUNTIME_HOST_PREFIX) : -len(AMAZONAWS_HOST_SUFFIX)]
    return region or None


def should_describe(process_renewed_ago: float | None) -> bool:
    """Si hay que preguntar a AWS: este proceso no la renovó nunca, o hace al
    menos `gatewayKeyRenewAfterSeconds`."""
    return process_renewed_ago is None or process_renewed_ago >= GATEWAY_KEY_RENEW_AFTER_SECONDS


def should_renew(secret_changed_ago: float) -> bool:
    """Si hay que escribir una clave nueva: el secreto cambió hace al menos
    `gatewayKeyRenewAfterSeconds` (por encima de los 10 minutos que pide
    `PutSecretValue`)."""
    return secret_changed_ago >= GATEWAY_KEY_RENEW_AFTER_SECONDS


def bearer_value(token: str) -> str:
    return f"{BEARER_PREFIX}{token}"


def renewable_gateways(project: AgentProject) -> tuple[RenewableGateway, ...]:
    """Las pasarelas `bedrock` del proyecto (`gateway_presets`). Sin ninguna,
    `InvalidArgumentException`: la opción no tiene nada que renovar."""
    found: list[RenewableGateway] = []
    for name, preset in sorted(project.gateway_presets.items()):
        gateway = project.gateways.get(name)
        if preset != BEDROCK_PRESET or gateway is None:
            continue
        region = gateway_region(gateway.upstream)
        secret = next(iter(gateway.headers.values()), None)
        if region is None or secret is None:
            continue
        found.append(RenewableGateway(name, _secret_id(secret), region))
    if not found:
        raise InvalidArgumentException(RENEW_NEEDS_BEDROCK)
    return tuple(found)


def _secret_id(secret: str | SecretRef) -> str:
    name = secret.name if isinstance(secret, SecretRef) else secret
    return resolve_secret_id(name, DEFAULT_SECRET_PREFIX)


class SecretsWriter(Protocol):
    """Las dos operaciones de Secrets Manager de la renovación."""

    def describe_secret(self, **params: Any) -> Mapping[str, Any]: ...

    def put_secret_value(self, **params: Any) -> Mapping[str, Any]: ...


class SessionCredentials(CredentialProvider):  # type: ignore[misc]
    """Las credenciales de la sesión boto3 de la flota, como el
    `CredentialProvider` que pide `provide_token`."""

    METHOD = "rayito-fleet-session"

    def __init__(self, session: Any) -> None:
        super().__init__()
        self._session = session

    def load(self) -> Any:
        return self._session.get_credentials()


def default_token_provider(session: Any, region: str | None) -> TokenProvider:
    """El generador oficial con las credenciales de la sesión de la flota.
    Importarlo sin el extra es `InvalidArgumentException` con la instrucción
    de instalación."""
    generator = require_module(
        TOKEN_GENERATOR_MODULE, extra=TOKEN_GENERATOR_EXTRA, feature=RENEWAL_FEATURE
    )
    credentials = SessionCredentials(aws_session(session, region))

    def provide(gateway_region_name: str) -> str:
        token: str = generator.provide_token(
            region=gateway_region_name,
            aws_credentials_provider=credentials,
            expiry=timedelta(seconds=GATEWAY_KEY_EXPIRY_SECONDS),
        )
        return token

    return provide


class LazySecretsWriter:
    """`SecretsWriter` sobre un cliente `secretsmanager` que se crea en la
    primera llamada."""

    def __init__(self, *, region: str | None, session: Any) -> None:
        self._client = LazyClient("secretsmanager", region=region, session=session)

    def describe_secret(self, **params: Any) -> Mapping[str, Any]:
        return self._client.get().describe_secret(**params)  # type: ignore[no-any-return]

    def put_secret_value(self, **params: Any) -> Mapping[str, Any]:
        return self._client.get().put_secret_value(**params)  # type: ignore[no-any-return]


class GatewayKeyRenewer:
    """Renueva, como mucho una vez por ventana, la clave de corta duración de
    cada pasarela `bedrock` de una flota.

    Coste y activación
    -------------------
    Activa: `renew_gateway_keys=True` en `AgentFleet`, `AgentFleet.connect`,
        `AsyncAgentFleet` o `AgentDeployOptions` (`--renew-gateway-keys`).
        Apagada por defecto: sin ella no se importa el generador ni se llama
        a Secrets Manager.
    Recursos y llamadas AWS: la firma de la clave es local (SigV4, sin
        llamada); `DescribeSecret` antes de crear un VM o abrir una plaza
        (salvo si este proceso ya renovó en la ventana) y `PutSecretValue`
        como mucho una vez cada `gatewayKeyRenewAfterSeconds` por pasarela.
    Coste aproximado: $0,05 por 10 000 llamadas a Secrets Manager; la clave
        no cuesta (us-east-1, consultado 2026-10-22 en
        https://aws.amazon.com/secrets-manager/pricing/).
    IAM: `secretsmanager:DescribeSecret` y `secretsmanager:PutSecretValue`
        sobre el secreto de la pasarela, y `bedrock:CallWithBearerToken` en
        el principal que firma (la clave hereda sus permisos de Bedrock).
    Cómo apagarla: no pases `renew_gateway_keys` (por defecto `False`).
    Ejemplo:
        fleet = AgentFleet.connect("my-agent", access_token=token,
                                   renew_gateway_keys=True)
    """

    def __init__(
        self,
        gateways: tuple[RenewableGateway, ...],
        *,
        token_provider: TokenProvider,
        secrets: SecretsWriter,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._gateways = gateways
        self._token_provider = token_provider
        self._secrets = secrets
        self._clock = clock
        self._now = now
        self._renewed_at: dict[str, float] = {}

    def __repr__(self) -> str:
        names = [gateway.name for gateway in self._gateways]
        return f"GatewayKeyRenewer(gateways={names!r})"

    def renew(self) -> bool:
        """Renueva las pasarelas que lo necesitan; `True` si escribió alguna
        clave (quien llama refresca las pasarelas de los VMs abiertos)."""
        renewed = False
        for gateway in self._gateways:
            renewed = self._renew_one(gateway) or renewed
        return renewed

    def _renew_one(self, gateway: RenewableGateway) -> bool:
        last = self._renewed_at.get(gateway.name)
        if not should_describe(None if last is None else self._clock() - last):
            return False
        described = self._call(gateway, "describe_secret", SecretId=gateway.secret_id)
        changed = described.get("LastChangedDate")
        if isinstance(changed, datetime) and not should_renew(
            (self._now() - changed).total_seconds()
        ):
            self._renewed_at[gateway.name] = self._clock()
            return False
        token = self._token_provider(gateway.region)
        self._call(
            gateway,
            "put_secret_value",
            SecretId=gateway.secret_id,
            SecretString=bearer_value(token),
            ClientRequestToken=str(uuid.uuid4()),
        )
        self._renewed_at[gateway.name] = self._clock()
        return True

    def _call(self, gateway: RenewableGateway, operation: str, **params: Any) -> Mapping[str, Any]:
        try:
            return getattr(self._secrets, operation)(**params)  # type: ignore[no-any-return]
        except (ClientError, BotoCoreError) as exc:
            raise _renewal_error(gateway, operation, exc) from sanitize_aws_error(
                exc, include_message=False
            )


def _renewal_error(gateway: RenewableGateway, operation: str, exc: BaseException) -> Exception:
    """Por código: un permiso que falta es `permission_denied` y nombra la
    pasarela, nunca el secreto."""
    code = aws_code(exc)
    action = "secretsmanager:DescribeSecret"
    if operation == "put_secret_value":
        action = "secretsmanager:PutSecretValue"
    if code and code.startswith(ACCESS_DENIED_PREFIX):
        return SecretException(
            f"sin permiso {action} para renovar la clave de la pasarela {gateway.name!r}",
            aws_code=code,
            code=PERMISSION_DENIED_CODE,
        )
    label = code or type(exc).__name__
    return SecretException(
        f"no se pudo renovar la clave de la pasarela {gateway.name!r} ({action}: {label})",
        aws_code=code,
    )


def gateway_key_renewer(
    project: AgentProject,
    *,
    region: str | None,
    session: Any,
    token_provider: TokenProvider | None = None,
    secrets: SecretsWriter | None = None,
) -> GatewayKeyRenewer:
    """El renovador de producción de `project`: valida que haya una pasarela
    `bedrock` y carga el generador (sin llamar a AWS)."""
    gateways = renewable_gateways(project)
    provider = token_provider or default_token_provider(session, region)
    writer = secrets or LazySecretsWriter(region=region, session=session)
    return GatewayKeyRenewer(gateways, token_provider=provider, secrets=writer)


__all__ = [
    "BEDROCK_PRESET",
    "RENEW_NEEDS_BEDROCK",
    "GatewayKeyRenewer",
    "RenewableGateway",
    "bearer_value",
    "gateway_key_renewer",
    "gateway_region",
    "renewable_gateways",
    "should_describe",
    "should_renew",
]

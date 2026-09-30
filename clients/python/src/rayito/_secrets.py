"""Secretos del usuario sobre AWS Secrets Manager, en su propia cuenta (M13a).

Dos funciones opcionales (ADR-014), apagadas por defecto:

- **Secret CRUD**: `SecretStore` crea, actualiza, describe, lista y borra
  secretos bajo un prefijo (`rayito/` por defecto). Es lo que usa el shim
  `Secret`/`AsyncSecret` de E2B.
- **Inyección**: `secrets={"ENV": "nombre"}` en `Sandbox.create()`,
  `connect()`, `SandboxPool.take()`, `commands.run`, `pty.create`,
  `run_code` y `create_code_context` entrega el valor como variable de
  entorno del proceso que lo pide, por los `envs` que esas llamadas ya
  mandan a `rayd` por el canal autenticado. Nunca viaja en el
  `runHookPayload`, `metadata`, etiquetas ni en el entorno de la imagen.

`SecretCache` es lo que evita traer el secreto en cada llamada: guarda el
valor en la memoria del proceso durante `ttl_seconds` (300 por defecto) por
(región, credenciales, secreto, versión), con una sola petición en vuelo por
clave; un acierto no hace ninguna llamada a AWS.

Coste y activación
-------------------
Activa: `SecretStore(...)` (CRUD explícito) y `secrets=` / `secret_cache=`
    (inyección como variable de entorno, con caché). Sin ninguna de las dos,
    Rayito no construye ningún cliente `secretsmanager` ni hace ninguna llamada
    a Secrets Manager (el camino de 0.4.0).
Recursos y llamadas AWS: `secretsmanager:CreateSecret`, `PutSecretValue`,
    `UpdateSecret`, `DescribeSecret`, `ListSecrets`, `DeleteSecret` (sólo
    desde `SecretStore`); `GetSecretValue` una vez por secreto y TTL
    (`secrets=`), nunca en cada comando. Ver `AWS_API_NOTES.md` §19.
Coste aproximado: $0,40 por secreto y mes (prorrateado, se paga hasta
    `destroy`) + $0,05 por 10 000 llamadas (us-east-1, consultado 2026-09-30,
    https://aws.amazon.com/secrets-manager/pricing/). Con el TTL por defecto,
    ≤ 12 `GetSecretValue`/hora por secreto y proceso ≈ $0,04/mes.
IAM: lector (`secrets=`): `secretsmanager:GetSecretValue` y
    `secretsmanager:DescribeSecret` sobre
    `arn:aws:secretsmanager:<región>:<cuenta>:secret:rayito/*`; administrador
    (`SecretStore`): además `CreateSecret`, `PutSecretValue`, `UpdateSecret`,
    `DeleteSecret` en ese ARN y `ListSecrets` en `*`; `kms:Decrypt` (y
    `kms:GenerateDataKey` para escribir) si usas una CMK. Plantilla opcional:
    `infra/secrets-access.yaml`. Son permisos de las credenciales del
    LLAMANTE, no del execution role del MicroVM.
Cómo apagarla: no pases `secrets=` ni `secret_cache=` (o pásalos a `None`) y
    no instancies `SecretStore`. Para dejar de pagar, `SecretStore().destroy(
    nombre)` de cada secreto: la facturación sigue hasta que se borran.
Ejemplo:
    from rayito import Sandbox, SecretStore
    SecretStore().create("openai", "sk-...")
    sbx = Sandbox.create(secrets={"OPENAI_API_KEY": "openai"})
    sbx.commands.run("python agent.py")  # 1 GetSecretValue por TTL
    SecretStore().destroy("openai")

Fase 1: el código del sandbox PUEDE leer un secreto inyectado (entorno del
proceso). Para código no confiable inyecta sólo tokens de vida corta y
mínimo privilegio (docs/site/docs/secrets.md).
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
import warnings
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, Final, Protocol, TypeAlias

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from rayito._aws import client_config
from rayito._aws_sanitize import sanitize_aws_error
from rayito.exceptions import (
    InvalidArgumentException,
    RateLimitException,
    RayitoCompatWarning,
    SecretException,
    SecretNotFoundException,
)

DEFAULT_SECRET_PREFIX: Final = "rayito/"
DEFAULT_TTL_SECONDS: Final = 300
MAX_TTL_SECONDS: Final = 86_400
VERSION_TOKEN_PREFIX: Final = "rayito-secret-version-"
METADATA_PREFIX: Final = "rayito:v1:"
DESCRIPTION_MAX_CHARS: Final = 2048
SECRET_STRING_MAX_BYTES: Final = 65_536
SECRET_ID_MAX_CHARS: Final = 512
LIST_MAX_RESULTS: Final = 100
CURRENT_STAGE: Final = "AWSCURRENT"
UPDATE_WARNING_INTERVAL_SECONDS: Final = 600.0
CREATE_RETRY_BUDGET_SECONDS: Final = 30.0
CREATE_RETRY_FIRST_DELAY_SECONDS: Final = 0.5
CREATE_RETRY_MAX_DELAY_SECONDS: Final = 8.0
MASK: Final = "***"

SECRET_NAME_PATTERN: Final = re.compile(r"[A-Za-z0-9/_+=.@-]+")
VERSION_TOKEN_PATTERN: Final = re.compile(rf"{re.escape(VERSION_TOKEN_PREFIX)}(\d{{20}})")

SECRET_VISIBILITY_WARNING: Final = (
    "secrets= entrega el valor como variable de entorno: el valor es visible para el código "
    "del sandbox (fase 1). Para código no confiable inyecta sólo tokens de vida corta y "
    "mínimo privilegio (docs/site/docs/secrets.md)"
)
UPDATE_FREQUENCY_WARNING: Final = (
    "SecretStore.update se llamó más de una vez en 600 s para el mismo secreto: Secrets "
    "Manager recomienda no escribir más de una vez cada 10 minutos de forma sostenida y "
    "conserva como mucho 100 versiones más las de las últimas 24 h (LimitExceededException)"
)

# ------------------------------------------------------------------ dominio


@dataclass(frozen=True)
class SecretRef:
    """Una referencia a un secreto, nunca su valor: `name` es un nombre bajo el
    prefijo del `SecretStore` (`"openai"` → `rayito/openai`) o un ARN
    completo (`arn:...`, que se usa tal cual). `version_id` o
    `version_stage` (como mucho uno) fijan la versión; sin ninguno se lee
    `AWSCURRENT`. Una cadena simple en `secrets=` equivale a `SecretRef(name)`.
    """

    name: str
    version_id: str | None = None
    version_stage: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise InvalidArgumentException("SecretRef.name debe ser una cadena no vacía")
        if self.version_id is not None and self.version_stage is not None:
            raise InvalidArgumentException(
                "SecretRef admite version_id o version_stage, no los dos"
            )
        versions = (("version_id", self.version_id), ("version_stage", self.version_stage))
        for label, value in versions:
            if value is not None and (not isinstance(value, str) or not value):
                raise InvalidArgumentException(f"SecretRef.{label} debe ser una cadena no vacía")

    @property
    def selector(self) -> tuple[str, str]:
        """La versión pedida como parte de la clave de caché."""
        if self.version_id is not None:
            return ("VersionId", self.version_id)
        return ("VersionStage", self.version_stage or CURRENT_STAGE)


SecretLike: TypeAlias = "str | SecretRef"
SecretsOption: TypeAlias = "Mapping[str, str | SecretRef]"


def as_ref(secret: str | SecretRef) -> SecretRef:
    if isinstance(secret, SecretRef):
        return secret
    if isinstance(secret, str):
        return SecretRef(secret)
    raise InvalidArgumentException("un secreto se referencia con un str o un SecretRef")


@dataclass(frozen=True)
class SecretInfo:
    """Metadatos de un secreto (los mismos campos que `e2b.SecretInfo`). El
    valor es de sólo escritura y nunca vuelve en ninguna lectura.

    `secret_id` es el ARN de Secrets Manager (E2B usa `sec_…`); `version` es
    el entero que Rayito codifica en el `ClientRequestToken` (1 al crear, +1
    en cada `update`; 0 si la versión actual no la escribió Rayito);
    `metadata` vive en `Description` (≤ 2048 caracteres codificados)."""

    secret_id: str
    name: str
    version: int
    metadata: dict[str, str]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class SecretPage:
    """Una página de `SecretStore.list()`: `next_token` es `None` en la última."""

    items: list[SecretInfo]
    next_token: str | None


def version_token(version: int) -> str:
    """`rayito-secret-version-{n:020d}`: 42 caracteres, dentro de los 32-64 de
    `ClientRequestToken` (AWS_API_NOTES.md §19)."""
    if version < 1:
        raise InvalidArgumentException("la versión de un secreto empieza en 1")
    return f"{VERSION_TOKEN_PREFIX}{version:020d}"


def version_from_id(version_id: str | None) -> int:
    """El entero de un `VersionId` escrito por Rayito; 0 para cualquier otro."""
    if not version_id:
        return 0
    match = VERSION_TOKEN_PATTERN.fullmatch(version_id)
    return int(match.group(1)) if match else 0


def current_version(stages: Mapping[str, Any] | None) -> int:
    """La versión que lleva `AWSCURRENT` en `VersionIdsToStages` (o en el
    `SecretVersionsToStages` de `ListSecrets`)."""
    for version_id, labels in (stages or {}).items():
        if isinstance(labels, list) and CURRENT_STAGE in labels:
            return version_from_id(str(version_id))
    return 0


def encode_metadata(metadata: Mapping[str, str] | None) -> str:
    """`rayito:v1:` + JSON compacto con claves ordenadas, validado contra los
    2048 caracteres de `Description` antes de llamar a AWS."""
    values = dict(metadata or {})
    for key, value in values.items():
        if not isinstance(key, str) or not key or not isinstance(value, str):
            raise InvalidArgumentException("metadata de un secreto: claves y valores str")
    encoded = METADATA_PREFIX + json.dumps(
        values, separators=(",", ":"), sort_keys=True, ensure_ascii=False
    )
    if len(encoded) > DESCRIPTION_MAX_CHARS:
        raise InvalidArgumentException(
            f"metadata de un secreto: {len(encoded)} caracteres codificados, máximo "
            f"{DESCRIPTION_MAX_CHARS} (Description de Secrets Manager)"
        )
    return encoded


def decode_metadata(description: str | None) -> dict[str, str]:
    if not description or not description.startswith(METADATA_PREFIX):
        return {}
    try:
        decoded = json.loads(description[len(METADATA_PREFIX) :])
    except ValueError:
        return {}
    if not isinstance(decoded, dict):
        return {}
    return {str(key): str(value) for key, value in decoded.items()}


def validate_prefix(prefix: str) -> str:
    if not isinstance(prefix, str):
        raise InvalidArgumentException("prefix debe ser str ('' permitido)")
    if prefix and not SECRET_NAME_PATTERN.fullmatch(prefix):
        raise InvalidArgumentException(
            "prefix sólo admite letras, dígitos y /_+=.@- (nombres de Secrets Manager)"
        )
    return prefix


def resolve_secret_id(name: str, prefix: str) -> str:
    """El `SecretId` de un nombre: un ARN tal cual; si no, `prefix + name`.
    El error nunca repite el nombre (es un selector confidencial)."""
    if not isinstance(name, str) or not name:
        raise InvalidArgumentException("el nombre de un secreto debe ser una cadena no vacía")
    if name.startswith("arn:"):
        return name
    secret_id = prefix + name
    if len(secret_id) > SECRET_ID_MAX_CHARS or not SECRET_NAME_PATTERN.fullmatch(secret_id):
        raise InvalidArgumentException(
            "nombre de secreto inválido: 1-512 caracteres (con el prefijo) de letras, "
            "dígitos y /_+=.@-"
        )
    return secret_id


def display_name(aws_name: str, prefix: str) -> str:
    return aws_name[len(prefix) :] if prefix and aws_name.startswith(prefix) else aws_name


def _as_datetime(value: Any) -> datetime:
    return value if isinstance(value, datetime) else datetime.now(UTC)


def info_from_description(described: Mapping[str, Any], prefix: str) -> SecretInfo:
    created = _as_datetime(described.get("CreatedDate"))
    stages = described.get("VersionIdsToStages", described.get("SecretVersionsToStages"))
    return SecretInfo(
        secret_id=str(described.get("ARN", "")),
        name=display_name(str(described.get("Name", "")), prefix),
        version=current_version(stages),
        metadata=decode_metadata(described.get("Description")),
        created_at=created,
        updated_at=_as_datetime(described.get("LastChangedDate", created)),
    )


# ------------------------------------------------------------------ puerto


class SecretsManagerApi(Protocol):
    """Lo que Rayito usa de un cliente boto3 `secretsmanager` (y sólo esto:
    AWS_API_NOTES.md §19). El adaptador real es el propio cliente."""

    def create_secret(self, **params: Any) -> dict[str, Any]: ...
    def put_secret_value(self, **params: Any) -> dict[str, Any]: ...
    def get_secret_value(self, **params: Any) -> dict[str, Any]: ...
    def describe_secret(self, **params: Any) -> dict[str, Any]: ...
    def update_secret(self, **params: Any) -> dict[str, Any]: ...
    def list_secrets(self, **params: Any) -> dict[str, Any]: ...
    def delete_secret(self, **params: Any) -> dict[str, Any]: ...


IAM_ACTIONS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "create_secret": "secretsmanager:CreateSecret",
        "put_secret_value": "secretsmanager:PutSecretValue",
        "get_secret_value": "secretsmanager:GetSecretValue",
        "describe_secret": "secretsmanager:DescribeSecret",
        "update_secret": "secretsmanager:UpdateSecret",
        "list_secrets": "secretsmanager:ListSecrets",
        "delete_secret": "secretsmanager:DeleteSecret",
    }
)


def aws_code(exc: BaseException) -> str | None:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return None
    error = response.get("Error")
    code = error.get("Code") if isinstance(error, dict) else None
    return code if isinstance(code, str) else None


def translate_error(operation: str, exc: BaseException) -> Exception:
    """El `ClientError`/`BotoCoreError` como excepción propia, sin el mensaje
    de AWS (puede nombrar el secreto). Quien la lanza la encadena con
    `from sanitize_aws_error(exc, include_message=False)`."""
    code = aws_code(exc)
    error: Exception
    if code == "ResourceNotFoundException":
        error = SecretNotFoundException(
            "el secreto no existe o está programado para borrarse", aws_code=code
        )
    elif code == "ThrottlingException":
        error = RateLimitException(
            f"Secrets Manager limitó la tasa de {IAM_ACTIONS[operation]}", aws_code=code
        )
    elif code == "AccessDeniedException":
        error = SecretException(
            f"sin permiso IAM {IAM_ACTIONS[operation]} sobre el secreto (credenciales del "
            "llamante; ver infra/secrets-access.yaml)",
            aws_code=code,
        )
    elif code == "ResourceExistsException" and operation == "create_secret":
        error = SecretException("ya existe un secreto con ese nombre", aws_code=code)
    elif code == "ResourceExistsException":
        error = SecretException(
            "otro escritor creó ya esa versión con un valor distinto: vuelve a llamar a update",
            aws_code=code,
        )
    elif code == "LimitExceededException":
        error = SecretException(
            "Secrets Manager rechazó la escritura por límite de versiones: escribe como mucho "
            "una vez cada 10 minutos",
            aws_code=code,
        )
    else:
        label = code or type(exc).__name__
        error = SecretException(
            f"Secrets Manager falló en {IAM_ACTIONS[operation]} ({label})", aws_code=code
        )
    return error


def is_scheduled_for_deletion(exc: ClientError) -> bool:
    """El `InvalidRequestException` de `CreateSecret` sobre un nombre cuyo
    borrado aún no terminó (AWS_API_NOTES.md §19, SEC-9)."""
    if aws_code(exc) != "InvalidRequestException":
        return False
    message = str(exc.response.get("Error", {}).get("Message", "")).lower()
    return "delet" in message


# ------------------------------------------------------------------ SecretStore

_update_lock = threading.Lock()
_last_update: dict[str, float] = {}
_update_warned: set[str] = set()


class SecretStore:
    """CRUD de secretos de Rayito sobre AWS Secrets Manager, en tu cuenta.

    Coste y activación
    -------------------
    Activa: `SecretStore(...)` es la opción explícita del CRUD; construirlo no
        llama a AWS: el cliente boto3 `secretsmanager` se crea en la primera
        llamada a un método.
    Recursos y llamadas AWS: un secreto de Secrets Manager por `create`
        (`CreateSecret`); `update` = `DescribeSecret` + `PutSecretValue`
        (+ `UpdateSecret` si cambias `metadata`); `get_info`/`exists` =
        `DescribeSecret`; `list` = `ListSecrets`; `destroy` = `DeleteSecret`
        (sin ventana de recuperación).
    Coste aproximado: $0,40 por secreto y mes hasta `destroy` + $0,05 por
        10 000 llamadas (us-east-1, 2026-09-30).
    IAM: `secretsmanager:CreateSecret`, `PutSecretValue`, `UpdateSecret`,
        `DescribeSecret`, `DeleteSecret` sobre `...:secret:rayito/*` y
        `secretsmanager:ListSecrets` sobre `*` (política `RayitoSecretsAdmin`
        de `infra/secrets-access.yaml`); con `kms_key_id=`, `kms:GenerateDataKey`
        y `kms:Decrypt` sobre esa clave.
    Cómo apagarla: no instancies `SecretStore`; borra con `destroy()` los
        secretos que ya no uses (se facturan hasta entonces).
    Ejemplo:
        store = SecretStore(region="us-east-1")
        info = store.create("openai", "sk-...", metadata={"team": "ml"})
        store.update("openai", "sk-nuevo")
        page = store.list(limit=20)
        store.destroy("openai")

    `name` es un nombre bajo `prefix` (`rayito/` por defecto; `''` para
    ninguno) o un ARN completo. Los errores nunca repiten el nombre ni el
    valor. Una instancia es reutilizable y segura entre hilos.
    """

    def __init__(
        self,
        *,
        region: str | None = None,
        session: boto3.session.Session | None = None,
        prefix: str = DEFAULT_SECRET_PREFIX,
        kms_key_id: str | None = None,
    ) -> None:
        self._region = region
        self._session = session
        self._prefix = validate_prefix(prefix)
        if kms_key_id is not None and (not isinstance(kms_key_id, str) or not kms_key_id):
            raise InvalidArgumentException("kms_key_id debe ser un id, ARN o alias de KMS")
        self._kms_key_id = kms_key_id
        self._client: SecretsManagerApi | None = None
        self._client_lock = threading.Lock()
        self._sleep: Callable[[float], None] = time.sleep
        self._clock: Callable[[], float] = time.monotonic

    @property
    def prefix(self) -> str:
        return self._prefix

    @property
    def region(self) -> str | None:
        """La región pedida (o la de la sesión); `None` = la cadena por defecto."""
        if self._region is not None:
            return self._region
        return getattr(self._session, "region_name", None)

    @property
    def session(self) -> boto3.session.Session | None:
        return self._session

    def __repr__(self) -> str:
        return f"SecretStore(region={self.region!r}, prefix={self._prefix!r})"

    # ---------------------------------------------------------------- CRUD

    def create(
        self, name: str, value: str, *, metadata: Mapping[str, str] | None = None
    ) -> SecretInfo:
        """`CreateSecret` con la versión 1. Si el nombre aún se está borrando
        (`destroy` reciente), reintenta con backoff hasta 30 s con el mismo
        `ClientRequestToken` y después lanza `SecretException`.
        `created_at`/`updated_at` son el reloj del cliente (`get_info` da los
        de AWS)."""
        if isinstance(name, str) and name.startswith("arn:"):
            raise InvalidArgumentException("create necesita un nombre, no un ARN")
        secret_id = resolve_secret_id(name, self._prefix)
        params: dict[str, Any] = {
            "Name": secret_id,
            "SecretString": validate_value(value),
            "Description": encode_metadata(metadata),
            "ClientRequestToken": version_token(1),
        }
        if self._kms_key_id is not None:
            params["KmsKeyId"] = self._kms_key_id
        response = self._create_with_retry(params)
        now = datetime.now(UTC)
        return SecretInfo(
            secret_id=str(response.get("ARN", "")),
            name=display_name(str(response.get("Name", secret_id)), self._prefix),
            version=1,
            metadata=dict(metadata or {}),
            created_at=now,
            updated_at=now,
        )

    def update(
        self, name: str, value: str, *, metadata: Mapping[str, str] | None = None
    ) -> SecretInfo:
        """Nueva versión `n + 1` (`DescribeSecret` + `PutSecretValue`); con
        `metadata`, además la sustituye (`UpdateSecret`). Los lectores con
        `SecretCache` ven el valor nuevo al vencer su TTL o con `refresh()`.
        Más de una llamada cada 600 s para el mismo secreto avisa una vez
        (`UserWarning`)."""
        secret_id = resolve_secret_id(name, self._prefix)
        secret_value = validate_value(value)
        description = None if metadata is None else encode_metadata(metadata)
        described = self._describe(secret_id)
        version = current_version(described.get("VersionIdsToStages")) + 1
        self._warn_if_frequent(secret_id)
        self._call(
            "put_secret_value",
            SecretId=secret_id,
            SecretString=secret_value,
            ClientRequestToken=version_token(version),
        )
        if description is not None:
            self._call("update_secret", SecretId=secret_id, Description=description)
        created = _as_datetime(described.get("CreatedDate"))
        return SecretInfo(
            secret_id=str(described.get("ARN", "")),
            name=display_name(str(described.get("Name", secret_id)), self._prefix),
            version=version,
            metadata=(
                decode_metadata(described.get("Description"))
                if metadata is None
                else dict(metadata)
            ),
            created_at=created,
            updated_at=datetime.now(UTC),
        )

    def get_info(self, name: str) -> SecretInfo:
        """`DescribeSecret`. Un secreto programado para borrarse es
        `SecretNotFoundException`."""
        return info_from_description(
            self._describe(resolve_secret_id(name, self._prefix)), self._prefix
        )

    def exists(self, name: str) -> bool:
        try:
            self.get_info(name)
        except SecretNotFoundException:
            return False
        return True

    def list(self, *, limit: int | None = None, next_token: str | None = None) -> SecretPage:
        """Una página de `ListSecrets` filtrada por el prefijo (sin los
        programados para borrarse). `limit` 1-100."""
        params: dict[str, Any] = {"IncludePlannedDeletion": False}
        if self._prefix:
            params["Filters"] = [{"Key": "name", "Values": [self._prefix]}]
        if limit is not None:
            if not isinstance(limit, int) or not 1 <= limit <= LIST_MAX_RESULTS:
                raise InvalidArgumentException(f"limit debe estar en 1..{LIST_MAX_RESULTS}")
            params["MaxResults"] = limit
        if next_token:
            params["NextToken"] = next_token
        response = self._call("list_secrets", **params)
        items = [
            info_from_description(entry, self._prefix)
            for entry in response.get("SecretList", [])
            if str(entry.get("Name", "")).startswith(self._prefix)
        ]
        token = response.get("NextToken")
        return SecretPage(items=items, next_token=str(token) if token else None)

    def destroy(self, name: str) -> bool:
        """`DeleteSecret(ForceDeleteWithoutRecovery=True)`: deja de facturarse
        y no se puede recuperar. `False` si no existía."""
        try:
            self._call(
                "delete_secret",
                SecretId=resolve_secret_id(name, self._prefix),
                ForceDeleteWithoutRecovery=True,
            )
        except SecretNotFoundException:
            return False
        return True

    # ------------------------------------------------------------ internals

    def read_value(self, ref: SecretRef) -> str:
        """`GetSecretValue` de una referencia; lo usa `SecretCache` (y sólo
        ella: nadie más debería leer valores sin caché)."""
        params: dict[str, Any] = {"SecretId": resolve_secret_id(ref.name, self._prefix)}
        if ref.version_id is not None:
            params["VersionId"] = ref.version_id
        elif ref.version_stage is not None:
            params["VersionStage"] = ref.version_stage
        response = self._call("get_secret_value", **params)
        value = response.get("SecretString")
        if not isinstance(value, str):
            raise SecretException("el secreto no tiene SecretString (Rayito sólo lee texto)")
        return value

    def cache_identity(self) -> tuple[str | None, object | None]:
        """La parte (región, credenciales) de la clave de `SecretCache`."""
        return (self.region, self._session)

    def api(self) -> SecretsManagerApi:
        """El cliente `secretsmanager`, construido en el primer uso."""
        with self._client_lock:
            if self._client is None:
                session = self._session or boto3.session.Session(region_name=self._region)
                self._client = session.client(
                    "secretsmanager", region_name=self._region, config=client_config()
                )
            return self._client

    def _call(self, operation: str, **params: Any) -> dict[str, Any]:
        try:
            response = getattr(self.api(), operation)(**params)
        except (ClientError, BotoCoreError) as exc:
            raise translate_error(operation, exc) from sanitize_aws_error(
                exc, include_message=False
            )
        return dict(response)

    def _describe(self, secret_id: str) -> dict[str, Any]:
        described = self._call("describe_secret", SecretId=secret_id)
        if described.get("DeletedDate") is not None:
            raise SecretNotFoundException(
                "el secreto no existe o está programado para borrarse",
                aws_code="ResourceNotFoundException",
            )
        return described

    def _create_with_retry(self, params: dict[str, Any]) -> dict[str, Any]:
        deadline = self._clock() + CREATE_RETRY_BUDGET_SECONDS
        delay = CREATE_RETRY_FIRST_DELAY_SECONDS
        while True:
            try:
                return dict(self.api().create_secret(**params))
            except ClientError as exc:
                if not is_scheduled_for_deletion(exc):
                    raise translate_error("create_secret", exc) from sanitize_aws_error(
                        exc, include_message=False
                    )
                if self._clock() + delay > deadline:
                    error = SecretException(
                        "el nombre sigue programado para borrarse tras 30 s de reintentos "
                        "(DeleteSecret es asíncrono): vuelve a intentarlo más tarde",
                        aws_code="InvalidRequestException",
                    )
                    raise error from sanitize_aws_error(exc, include_message=False)
            except BotoCoreError as exc:
                raise translate_error("create_secret", exc) from sanitize_aws_error(
                    exc, include_message=False
                )
            self._sleep(delay)
            delay = min(delay * 2, CREATE_RETRY_MAX_DELAY_SECONDS)

    def _warn_if_frequent(self, secret_id: str) -> None:
        key = f"{self.region}|{secret_id}"
        now = self._clock()
        with _update_lock:
            previous = _last_update.get(key)
            _last_update[key] = now
            frequent = previous is not None and now - previous < UPDATE_WARNING_INTERVAL_SECONDS
            first = frequent and key not in _update_warned
            if first:
                _update_warned.add(key)
        if first:
            warnings.warn(UPDATE_FREQUENCY_WARNING, UserWarning, stacklevel=3)


def validate_value(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise InvalidArgumentException("el valor de un secreto debe ser un str no vacío")
    if len(value.encode("utf-8")) > SECRET_STRING_MAX_BYTES:
        raise InvalidArgumentException(
            f"el valor de un secreto no puede pasar de {SECRET_STRING_MAX_BYTES} bytes"
        )
    return value


# ------------------------------------------------------------------ SecretCache

CacheKey: TypeAlias = "tuple[str | None, object | None, str, tuple[str, str]]"


@dataclass
class _Entry:
    value: str = field(repr=False)
    expires_at: float


class SecretCache:
    """Caché en memoria de valores de secretos: lo que hace que `secrets=`
    nunca traiga el secreto en cada llamada.

    Coste y activación
    -------------------
    Activa: `secret_cache=SecretCache(ttl_seconds=300)` fija la caché (y su
        TTL) que usa `secrets=`; sin él, `secrets=` usa una caché compartida
        del proceso por (región, sesión) con TTL 300, creada la primera vez.
        Construirla no llama a AWS.
    Recursos y llamadas AWS: `GetSecretValue` en el primer uso de cada
        (región, credenciales, secreto, versión) y otra vez sólo al vencer el
        TTL o con `refresh()`; un acierto hace 0 llamadas. Una sola petición
        en vuelo por clave (10 hilos a la vez = 1 llamada).
    Coste aproximado: con TTL 300, ≤ 12 llamadas/hora por secreto y proceso
        ≈ $0,04/mes ($0,05 por 10 000 llamadas, us-east-1, 2026-09-30); el
        secreto en sí cuesta $0,40/mes aparte.
    IAM: `secretsmanager:GetSecretValue` (política `RayitoSecretsReader` de
        `infra/secrets-access.yaml`) y `kms:Decrypt` si el secreto usa una CMK.
    Cómo apagarla: no pases `secrets=` ni `secret_cache=`. `ttl_seconds=0` no
        existe (sería traer en cada llamada): 1..86400.
    Ejemplo:
        cache = SecretCache(ttl_seconds=600, region="us-east-1")
        sbx = Sandbox.create(secrets={"OPENAI_API_KEY": "openai"}, secret_cache=cache)
        sbx.commands.run("python agent.py")   # 1 GetSecretValue
        sbx.commands.run("python agent.py")   # 0: acierto
        cache.refresh("openai")               # fuerza una lectura nueva

    Los valores viven sólo en la memoria de este proceso; `repr`/`str` nunca
    los muestran. Un secreto que no existe no se guarda.
    """

    def __init__(
        self,
        *,
        ttl_seconds: int | float = DEFAULT_TTL_SECONDS,
        store: SecretStore | None = None,
        region: str | None = None,
        session: boto3.session.Session | None = None,
        prefix: str | None = None,
    ) -> None:
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int | float):
            raise InvalidArgumentException("ttl_seconds debe ser un número de segundos")
        if not 1 <= ttl_seconds <= MAX_TTL_SECONDS:
            raise InvalidArgumentException(
                f"ttl_seconds debe estar en 1..{MAX_TTL_SECONDS}: una caché de 0 s traería el "
                "secreto en cada llamada"
            )
        if store is not None and (region is not None or session is not None or prefix is not None):
            raise InvalidArgumentException(
                "SecretCache: pasa store= o region=/session=/prefix=, no ambos"
            )
        if store is not None and not isinstance(store, SecretStore):
            raise InvalidArgumentException("store debe ser un SecretStore")
        self._ttl = float(ttl_seconds)
        self._store = store or SecretStore(
            region=region,
            session=session,
            prefix=DEFAULT_SECRET_PREFIX if prefix is None else prefix,
        )
        self._entries: dict[CacheKey, _Entry] = {}
        self._flights: dict[CacheKey, threading.Lock] = {}
        self._guard = threading.Lock()
        self._generation = 0
        self._clock: Callable[[], float] = time.monotonic

    @property
    def ttl_seconds(self) -> float:
        return self._ttl

    @property
    def store(self) -> SecretStore:
        return self._store

    def __repr__(self) -> str:
        with self._guard:
            count = len(self._entries)
        return f"SecretCache(ttl_seconds={self._ttl:g}, entries={count}, values={MASK!r})"

    __str__ = __repr__

    def get(self, secret: str | SecretRef) -> str:
        """El valor (de la caché si sigue vigente; si no, `GetSecretValue`)."""
        ref = as_ref(secret)
        key = self._key(ref)
        hit = self._fresh(key)
        if hit is not None:
            return hit
        with self._flight(key):
            hit = self._fresh(key)
            if hit is not None:
                return hit
            with self._guard:
                generation = self._generation
            value = self._store.read_value(ref)
            with self._guard:
                if generation == self._generation:
                    self._entries[key] = _Entry(value, self._clock() + self._ttl)
            return value

    async def aget(self, secret: str | SecretRef) -> str:
        """`get` para asyncio: un acierto no sale del bucle; un fallo corre en
        `asyncio.to_thread` con la misma petición única por clave."""
        ref = as_ref(secret)
        hit = self._fresh(self._key(ref))
        if hit is not None:
            return hit
        return await asyncio.to_thread(self.get, ref)

    def refresh(self, name: str | SecretRef | None = None) -> None:
        """Fuerza una lectura nueva: con `name`, la hace ya (y lanza si el
        secreto no existe); sin él, descarta todo y cada secreto se relee en
        su siguiente uso."""
        self.invalidate(name)
        if name is not None:
            self.get(name)

    def invalidate(self, name: str | SecretRef | None = None) -> None:
        """Descarta los valores de `name` (todas sus versiones si es un str) o
        de todos."""
        with self._guard:
            self._generation += 1
            if name is None:
                self._entries.clear()
                return
            ref = as_ref(name)
            secret_id = resolve_secret_id(ref.name, self._store.prefix)
            exact = ref.selector if isinstance(name, SecretRef) else None
            for key in [
                key
                for key in self._entries
                if key[2] == secret_id and (exact is None or key[3] == exact)
            ]:
                del self._entries[key]

    def _key(self, ref: SecretRef) -> CacheKey:
        region, identity = self._store.cache_identity()
        return (region, identity, resolve_secret_id(ref.name, self._store.prefix), ref.selector)

    def _fresh(self, key: CacheKey) -> str | None:
        with self._guard:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if entry.expires_at <= self._clock():
                del self._entries[key]
                return None
            return entry.value

    def _flight(self, key: CacheKey) -> threading.Lock:
        with self._guard:
            return self._flights.setdefault(key, threading.Lock())


_shared_lock = threading.Lock()
_shared_caches: dict[tuple[str | None, object | None], SecretCache] = {}


def shared_secret_cache(region: str | None, session: boto3.session.Session | None) -> SecretCache:
    """La caché del proceso para `secrets=` sin `secret_cache=`: una por
    (región, sesión), TTL 300, creada la primera vez que se usa."""
    key = (region, session)
    with _shared_lock:
        cache = _shared_caches.get(key)
        if cache is None:
            cache = SecretCache(region=region, session=session)
            _shared_caches[key] = cache
        return cache


# ------------------------------------------------------------------ inyección

_visibility_warned = threading.Event()


def warn_visibility_once() -> None:
    if not _visibility_warned.is_set():
        _visibility_warned.set()
        warnings.warn(SECRET_VISIBILITY_WARNING, RayitoCompatWarning, stacklevel=4)


def normalize_secrets(secrets: Mapping[str, str | SecretRef] | None) -> dict[str, SecretRef]:
    """`{ENV: str | SecretRef}` → `{ENV: SecretRef}`. Los errores nombran la
    clave de la variable, nunca el nombre del secreto."""
    if secrets is None:
        return {}
    if not isinstance(secrets, Mapping):
        raise InvalidArgumentException("secrets debe ser un dict {VARIABLE: nombre | SecretRef}")
    refs: dict[str, SecretRef] = {}
    for key, secret in secrets.items():
        if not isinstance(key, str) or not key or "=" in key or "\x00" in key:
            raise InvalidArgumentException(
                "clave de secrets inválida: una variable de entorno no vacía, sin '=' ni NUL"
            )
        if not isinstance(secret, str | SecretRef) or (isinstance(secret, str) and not secret):
            raise InvalidArgumentException(
                f"secrets[{key!r}] debe ser un nombre no vacío o un SecretRef"
            )
        refs[key] = as_ref(secret)
    if refs:
        warn_visibility_once()
    return refs


def validate_secret_cache(secret_cache: object) -> SecretCache | None:
    if secret_cache is None or isinstance(secret_cache, SecretCache):
        return secret_cache
    raise InvalidArgumentException("secret_cache debe ser un SecretCache")


@dataclass(frozen=True, repr=False)
class SecretBinding:
    """Lo que guarda un handle (`Sandbox`) de `secrets=`/`secret_cache=`:
    sólo las referencias ENV → `SecretRef`, nunca valores. `repr` enseña
    sólo cuántas variables hay."""

    refs: Mapping[str, SecretRef]
    cache: SecretCache | None

    def __repr__(self) -> str:
        return f"SecretBinding(envs=<{len(self.refs)} keys>, values={MASK!r})"


def bind_secrets(
    secrets: Mapping[str, str | SecretRef] | None, secret_cache: SecretCache | None
) -> SecretBinding | None:
    """`None` cuando no se pidió nada: el camino de 0.4.0."""
    cache = validate_secret_cache(secret_cache)
    refs = normalize_secrets(secrets)
    if not refs and cache is None:
        return None
    return SecretBinding(refs=MappingProxyType(refs), cache=cache)


CacheFactory: TypeAlias = "Callable[[], SecretCache]"


@dataclass(frozen=True, repr=False)
class InjectionPlan:
    """Qué variables hay que resolver para una llamada y con qué caché."""

    refs: Mapping[str, SecretRef]
    cache: SecretCache

    def __repr__(self) -> str:
        return f"InjectionPlan(envs=<{len(self.refs)} keys>)"

    def resolve(self) -> dict[str, str]:
        return {key: self.cache.get(ref) for key, ref in self.refs.items()}

    async def aresolve(self) -> dict[str, str]:
        return {key: await self.cache.aget(ref) for key, ref in self.refs.items()}


def plan_injection(
    *,
    envs: Mapping[str, str] | None,
    bound: SecretBinding | None,
    secrets: Mapping[str, str | SecretRef] | None,
    default_cache: CacheFactory,
    include_bound: bool = True,
) -> InjectionPlan | None:
    """Secretos del handle más los de la llamada (la llamada gana en una clave
    repetida). Una clave que además está en `envs` es
    `InvalidArgumentException` (todo o nada: nunca se pisa en silencio).
    `None` si no hay nada que inyectar: la llamada queda como en 0.4.0."""
    refs = dict(bound.refs) if bound is not None and include_bound else {}
    refs.update(normalize_secrets(secrets))
    if not refs:
        return None
    clashes = sorted(set(refs) & set(envs or {}))
    if clashes:
        raise InvalidArgumentException(
            f"la variable {clashes[0]!r} está a la vez en envs= y en secrets=: pásala sólo en uno"
        )
    cache = bound.cache if bound is not None and bound.cache is not None else default_cache()
    return InjectionPlan(refs=MappingProxyType(refs), cache=cache)


def merge_envs(envs: Mapping[str, str] | None, values: Mapping[str, str]) -> dict[str, str]:
    merged = dict(envs or {})
    merged.update(values)
    return merged


def warm(binding: SecretBinding | None, default_cache: CacheFactory) -> SecretBinding | None:
    """Fija la caché del handle (la de `secret_cache=` o la compartida) y
    resuelve sus secretos antes de lanzar, conectar o tomar una plaza: un
    secreto que falta falla aquí, sin haber facturado un VM."""
    if binding is None:
        return None
    bound = binding if binding.cache is not None else SecretBinding(binding.refs, default_cache())
    cache = bound.cache
    assert cache is not None
    for ref in bound.refs.values():
        cache.get(ref)
    return bound


async def awarm(binding: SecretBinding | None, default_cache: CacheFactory) -> SecretBinding | None:
    """`warm` para asyncio."""
    if binding is None:
        return None
    bound = binding if binding.cache is not None else SecretBinding(binding.refs, default_cache())
    cache = bound.cache
    assert cache is not None
    for ref in bound.refs.values():
        await cache.aget(ref)
    return bound


def secret_envs(
    envs: Mapping[str, str] | None,
    *,
    bound: SecretBinding | None,
    secrets: Mapping[str, str | SecretRef] | None,
    default_cache: CacheFactory,
    include_bound: bool = True,
) -> Mapping[str, str] | None:
    """Los `envs` de una llamada con los secretos ya resueltos (sync)."""
    plan = plan_injection(
        envs=envs,
        bound=bound,
        secrets=secrets,
        default_cache=default_cache,
        include_bound=include_bound,
    )
    return envs if plan is None else merge_envs(envs, plan.resolve())


async def asecret_envs(
    envs: Mapping[str, str] | None,
    *,
    bound: SecretBinding | None,
    secrets: Mapping[str, str | SecretRef] | None,
    default_cache: CacheFactory,
    include_bound: bool = True,
) -> Mapping[str, str] | None:
    """`secret_envs` para asyncio."""
    plan = plan_injection(
        envs=envs,
        bound=bound,
        secrets=secrets,
        default_cache=default_cache,
        include_bound=include_bound,
    )
    return envs if plan is None else merge_envs(envs, await plan.aresolve())


PYTHON_LANGUAGES: Final = frozenset({"python", "python3"})


def code_secrets_scope(language: str | None, context_language: str | None, secrets: object) -> bool:
    """Si una celda de `run_code` puede llevar secretos: `ExecuteRequest.envs`
    sólo existe en contextos Python (`rayd` responde `INVALID_ARGUMENT` en los
    demás). Con `secrets=` en la propia llamada sobre otro lenguaje es
    `InvalidArgumentException` apuntando a `create_code_context(secrets=)`;
    los secretos del handle sólo se añaden a celdas Python (devuelve False)."""
    target = (language or context_language or "python").lower()
    if target in PYTHON_LANGUAGES:
        return True
    if secrets:
        raise InvalidArgumentException(
            "run_code(secrets=) sólo vale en contextos Python (ExecuteRequest.envs); para "
            "otros lenguajes usa create_code_context(language=..., secrets=...)"
        )
    return False


__all__ = [
    "DEFAULT_SECRET_PREFIX",
    "DEFAULT_TTL_SECONDS",
    "SecretBinding",
    "SecretCache",
    "SecretInfo",
    "SecretPage",
    "SecretRef",
    "SecretStore",
    "SecretsManagerApi",
    "shared_secret_cache",
]

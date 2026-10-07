"""Índice opcional de metadatos sobre DynamoDB, en la cuenta del cliente (M14).

`list-microvms` no conoce los metadatos de un sandbox: sin índice,
`Sandbox.list(metadata=...)` los lee del agente con una sonda de `Health`
por sandbox y sólo puede mirar sandboxes `RUNNING` (sondear uno suspendido
lo despertaría). Con `index=DynamoDbIndex("tabla")`, `create()` escribe una
fila inmutable por sandbox y `list(metadata=..., index=...)` la une con
`list-microvms`: filtra también sandboxes `SUSPENDED` sin sondear ninguno.

El núcleo es puro (`IndexRecord`, `record_for`, `joined`); la única E/S
es `DynamoDbIndex`, que construye su cliente boto3 `dynamodb` en su primer
uso (ADR-014) y sólo llama a `PutItem` y `BatchGetItem`
(`AWS_API_NOTES.md` §20).

Reglas de la unión (una fila nunca inventa un sandbox): el ESTADO sale
siempre de `list-microvms`; un item sólo se queda si hay una fila con su
mismo `sandbox_id`, su mismo ARN de imagen y su mismo `startedAt` (±1 s),
no caducada, y cuyos metadatos contienen cada par pedido. Un item sin fila
(creado sin índice, o con `on_write_failure='warn'` y una escritura
fallida) se excluye: sus metadatos son desconocidos y nunca se sondea.

La fila guarda sólo datos inmutables escritos una vez: nunca el access
token (ni su hash), `envs`, referencias o valores de secretos, el
`runHookPayload` ni el JWE.

Coste y activación
-------------------
Activa: `index=DynamoDbIndex("rayito-sandboxes")` en `Sandbox.create()`,
    `Sandbox.list()`, `Sandbox.paginate()`, `PoolConfig`, el shim
    `rayito.e2b` (`Sandbox.list(..., index=)` / `E2B(index=)`) y la CLI
    (`--index-table`). Sin esa opción no se construye ningún cliente
    `dynamodb` ni se hace ninguna llamada a DynamoDB (el camino de 0.4.0).
Recursos y llamadas AWS: ninguno se crea desde el SDK; la tabla la
    despliegas tú con `infra/metadata-index.yaml`. `dynamodb:PutItem` una vez
    por sandbox creado (con `ConditionExpression attribute_not_exists(pk)`);
    `dynamodb:BatchGetItem` una vez por página de `list-microvms` (≤ 50
    claves) al listar con `metadata=` e `index=`. Nunca `DeleteItem`: el TTL
    (`expires_at`) borra las filas gratis.
Coste aproximado: DynamoDB on-demand (us-east-1, consultado 2026-09-30,
    https://aws.amazon.com/dynamodb/pricing/on-demand/): $0,625 por millón de
    WRU (1 WRU por `create`, ítem ≤ 1 KB ≈ $0,000000625) + $0,125 por millón
    de RRU (0,5 RRU por ítem leído con lectura eventualmente consistente) +
    $0,25/GB-mes. 10 000 sandboxes/mes < $0,10. Tabla vacía: $0.
IAM: escritor (`create`, relleno del pool): `dynamodb:PutItem`; lector
    (`list`/`paginate`): `dynamodb:BatchGetItem`; ambos sobre el ARN de la
    tabla (políticas `RayitoIndexWriter`/`RayitoIndexReader` de
    `infra/metadata-index.yaml`), en las credenciales del LLAMANTE.
Cómo apagarla: no pases `index=` (o pásalo a `None`, el valor por defecto);
    para dejar de pagar el almacenamiento, borra el stack de la tabla.
Ejemplo:
    from rayito import DynamoDbIndex, Sandbox
    idx = DynamoDbIndex("rayito-sandboxes")
    sbx = Sandbox.create(metadata={"user": "42"}, index=idx)
    sbx.pause()
    paused = list(Sandbox.list(metadata={"user": "42"}, states=["SUSPENDED"], index=idx))
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any, Final, Literal, Protocol

import boto3
from botocore.exceptions import BotoCoreError, ClientError, NoRegionError

from rayito._aws import LazyClient, aws_code
from rayito._aws_sanitize import sanitize_aws_error
from rayito._models import SandboxInfo, SandboxListItem
from rayito._version import __version__
from rayito.exceptions import (
    IndexWriteException,
    InvalidArgumentException,
    SandboxIndexException,
)

WriteFailurePolicy = Literal["terminate", "warn"]

DEFAULT_TTL_MARGIN_SECONDS: Final = 3600
BATCH_GET_MAX_KEYS: Final = 100
STARTED_AT_TOLERANCE_MS: Final = 1000
UNPROCESSED_RETRY_ATTEMPTS: Final = 5
UNPROCESSED_FIRST_DELAY_SECONDS: Final = 0.05
UNPROCESSED_MAX_DELAY_SECONDS: Final = 1.0
PUT_CONDITION: Final = "attribute_not_exists(pk)"
SDK_TAG: Final = f"py/{__version__}"
WRITE_FAILURE_POLICIES: Final = ("terminate", "warn")
TABLE_NAME_PATTERN: Final = re.compile(r"[A-Za-z0-9_.-]{3,255}")
RECORD_ATTRIBUTES: Final = frozenset(
    {"pk", "image_arn", "image_version", "started_at_ms", "metadata", "sdk", "expires_at"}
)
IAM_ACTIONS: Final[Mapping[str, str]] = MappingProxyType(
    {"put_item": "dynamodb:PutItem", "batch_get_item": "dynamodb:BatchGetItem"}
)

logger = logging.getLogger("rayito.index")

# ------------------------------------------------------------------ dominio


@dataclass(frozen=True, repr=False)
class IndexRecord:
    """La fila de un sandbox en el índice: sólo datos inmutables, escritos
    una vez tras `run-microvm`. `started_at_ms` es el `startedAt` de
    `run-microvm`; `expires_at` (segundos epoch, el atributo TTL de la
    tabla) es `startedAt + maximumDurationInSeconds + margen`: el sandbox ya
    no puede existir después. El `repr` sólo nombra las claves de
    `metadata`, nunca sus valores."""

    sandbox_id: str
    image_arn: str
    image_version: str
    started_at_ms: int
    metadata: Mapping[str, str]
    sdk: str
    expires_at: int

    def __repr__(self) -> str:
        keys = sorted(self.metadata)
        return f"IndexRecord(sandbox_id={self.sandbox_id!r}, metadata_keys={keys!r})"

    def to_item(self) -> dict[str, Any]:
        """El `Item` de `PutItem` en el formato de atributos de DynamoDB."""
        return {
            "pk": {"S": self.sandbox_id},
            "image_arn": {"S": self.image_arn},
            "image_version": {"S": self.image_version},
            "started_at_ms": {"N": str(self.started_at_ms)},
            "metadata": {"M": {key: {"S": value} for key, value in self.metadata.items()}},
            "sdk": {"S": self.sdk},
            "expires_at": {"N": str(self.expires_at)},
        }

    @classmethod
    def from_item(cls, item: Mapping[str, Any]) -> IndexRecord | None:
        """Una fila leída con `BatchGetItem`, o `None` si no tiene la forma
        que escribe Rayito (se trata como "sin fila": el item se excluye)."""
        try:
            metadata = {key: value["S"] for key, value in item["metadata"]["M"].items()}
            record = cls(
                sandbox_id=item["pk"]["S"],
                image_arn=item["image_arn"]["S"],
                image_version=item["image_version"]["S"],
                started_at_ms=int(item["started_at_ms"]["N"]),
                metadata=metadata,
                sdk=item["sdk"]["S"],
                expires_at=int(item["expires_at"]["N"]),
            )
        except (KeyError, TypeError, ValueError, AttributeError):
            return None
        if not all(isinstance(value, str) for value in metadata.values()):
            return None
        return record


def unix_ms(info: SandboxInfo | SandboxListItem) -> int:
    return round(info.started_at.timestamp() * 1000)


def record_for(
    info: SandboxInfo,
    metadata: Mapping[str, str] | None,
    margin: int = DEFAULT_TTL_MARGIN_SECONDS,
    *,
    sdk: str = SDK_TAG,
) -> IndexRecord:
    """La fila de un sandbox recién lanzado, determinista: sólo depende de la
    respuesta de `run-microvm`, de los metadatos del `create()` y del margen."""
    started_ms = unix_ms(info)
    return IndexRecord(
        sandbox_id=info.sandbox_id,
        image_arn=info.template,
        image_version=info.template_version,
        started_at_ms=started_ms,
        metadata=MappingProxyType(dict(metadata or {})),
        sdk=sdk,
        expires_at=started_ms // 1000 + info.maximum_duration_seconds + margin,
    )


def joined(
    item: SandboxListItem,
    record: IndexRecord | None,
    wanted: Mapping[str, str],
    now_seconds: float,
) -> SandboxListItem | None:
    """El item con `metadata` relleno si su fila casa (misma imagen, mismo
    `startedAt` ±1 s, no caducada, metadatos ⊇ `wanted`); si no, `None`. El
    estado es siempre el de `list-microvms`."""
    if record is None or record.sandbox_id != item.sandbox_id:
        return None
    if record.expires_at < now_seconds:
        return None
    if record.image_arn != item.template:
        return None
    if abs(record.started_at_ms - unix_ms(item)) > STARTED_AT_TOLERANCE_MS:
        return None
    if not all(record.metadata.get(key) == value for key, value in wanted.items()):
        return None
    return replace(item, metadata=dict(record.metadata))


def chunks(ids: Sequence[str], size: int = BATCH_GET_MAX_KEYS) -> list[list[str]]:
    unique = list(dict.fromkeys(ids))
    return [unique[start : start + size] for start in range(0, len(unique), size)]


# ------------------------------------------------------------------ puerto


class DynamoDbApi(Protocol):
    """Lo que Rayito usa de un cliente boto3 `dynamodb` (y sólo esto:
    `AWS_API_NOTES.md` §20). El adaptador real es el propio cliente."""

    def put_item(self, **params: Any) -> dict[str, Any]: ...
    def batch_get_item(self, **params: Any) -> dict[str, Any]: ...


def index_error(
    operation: str, exc: BaseException, error_type: type[SandboxIndexException]
) -> SandboxIndexException:
    """El error de DynamoDB como excepción propia, sin el mensaje de AWS
    (puede nombrar la tabla o la clave)."""
    code = aws_code(exc)
    action = IAM_ACTIONS[operation]
    if code == "ConditionalCheckFailedException":
        message = "ya había una fila con ese sandbox_id en el índice; no se sobrescribe"
    elif code == "ResourceNotFoundException":
        message = (
            "la tabla del índice no existe en esa región (despliega infra/metadata-index.yaml)"
        )
    elif code == "AccessDeniedException":
        message = (
            f"sin permiso IAM {action} sobre la tabla del índice (credenciales del llamante; "
            "ver infra/metadata-index.yaml)"
        )
    elif code in ("ProvisionedThroughputExceededException", "ThrottlingException"):
        message = f"DynamoDB limitó la tasa de {action} tras los reintentos del SDK"
    else:
        message = f"DynamoDB falló en {action} ({code or type(exc).__name__})"
    return error_type(message, aws_code=code)


# ------------------------------------------------------------------ adaptador


class DynamoDbIndex:
    """Índice de metadatos de sandboxes sobre una tabla DynamoDB de tu cuenta.

    Coste y activación
    -------------------
    Activa: `index=DynamoDbIndex("rayito-sandboxes")` en `Sandbox.create()`
        (escribe una fila por sandbox), `Sandbox.list()`/`paginate()` con
        `metadata=` (filtra también `SUSPENDED` sin sondear `Health`) y
        `PoolConfig(index=)` (escribe al rellenar). Construirlo no llama a
        AWS: el cliente boto3 `dynamodb` se crea en su primer uso.
    Recursos y llamadas AWS: `dynamodb:PutItem` (1 por sandbox creado,
        condicional `attribute_not_exists(pk)`) y `dynamodb:BatchGetItem` (1
        por página de `list-microvms`, ≤ 100 claves, lectura eventualmente
        consistente). Ningún recurso: la tabla la despliegas tú
        (`infra/metadata-index.yaml`). Nunca `DeleteItem` (el TTL borra).
    Coste aproximado: on-demand, us-east-1, consultado 2026-09-30: ~1 WRU por
        `create` ≈ $0,000000625; 0,5 RRU por ítem listado; $0,25/GB-mes;
        10 000 sandboxes/mes < $0,10; tabla vacía $0
        (https://aws.amazon.com/dynamodb/pricing/on-demand/).
    IAM: `dynamodb:PutItem` (escritor) y `dynamodb:BatchGetItem` (lector)
        sobre el ARN de la tabla, en las credenciales del llamante.
    Cómo apagarla: no pases `index=` (`None` por defecto) y borra el stack
        de la tabla si ya no la usas.
    Ejemplo:
        idx = DynamoDbIndex("rayito-sandboxes", region="us-east-1")
        sbx = Sandbox.create(metadata={"user": "42"}, index=idx)
        sbx.pause()
        items = list(Sandbox.list(metadata={"user": "42"}, states=["SUSPENDED"], index=idx))

    `on_write_failure` decide qué hace `create()` si `PutItem` falla:
    `'terminate'` (por defecto) termina el MicroVM recién lanzado (salvo
    `keep_on_failure=True`) y lanza `IndexWriteException`; `'warn'` avisa en
    el logger `rayito.index` y devuelve el sandbox, que no aparecerá en los
    listados con índice. `ttl_margin_seconds` es el margen sobre la vida
    máxima del sandbox antes de que la fila caduque. `region`/`session` son
    los de la tabla (por defecto la cadena de boto3); sin región,
    `create()` lanza `InvalidArgumentException` antes de `run-microvm`, sin
    lanzar ningún MicroVM. Reutilizable y seguro entre hilos.
    """

    def __init__(
        self,
        table_name: str,
        *,
        region: str | None = None,
        session: boto3.session.Session | None = None,
        on_write_failure: WriteFailurePolicy = "terminate",
        ttl_margin_seconds: int = DEFAULT_TTL_MARGIN_SECONDS,
    ) -> None:
        if not isinstance(table_name, str) or not TABLE_NAME_PATTERN.fullmatch(table_name):
            raise InvalidArgumentException(
                "table_name debe ser un nombre de tabla DynamoDB (3-255 caracteres [A-Za-z0-9_.-])"
            )
        if on_write_failure not in WRITE_FAILURE_POLICIES:
            raise InvalidArgumentException(
                f"on_write_failure debe ser 'terminate' o 'warn', recibido {on_write_failure!r}"
            )
        if (
            isinstance(ttl_margin_seconds, bool)
            or not isinstance(ttl_margin_seconds, int)
            or ttl_margin_seconds < 0
        ):
            raise InvalidArgumentException("ttl_margin_seconds debe ser un entero >= 0")
        self._table_name = table_name
        self._region = region
        self._session = session
        self._on_write_failure: WriteFailurePolicy = on_write_failure
        self._ttl_margin_seconds = ttl_margin_seconds
        self._client = LazyClient("dynamodb", region=region, session=session)
        self._sleep: Callable[[float], None] = time.sleep
        self._clock: Callable[[], float] = time.time

    @property
    def table_name(self) -> str:
        return self._table_name

    @property
    def on_write_failure(self) -> WriteFailurePolicy:
        return self._on_write_failure

    @property
    def ttl_margin_seconds(self) -> int:
        return self._ttl_margin_seconds

    def __repr__(self) -> str:
        return (
            f"DynamoDbIndex(table_name={self._table_name!r}, "
            f"on_write_failure={self._on_write_failure!r})"
        )

    def now(self) -> float:
        """Segundos epoch con los que se descartan filas caducadas."""
        return self._clock()

    def record(self, info: SandboxInfo, metadata: Mapping[str, str] | None) -> IndexRecord:
        return record_for(info, metadata, self._ttl_margin_seconds)

    def put(self, record: IndexRecord) -> None:
        """`PutItem` condicional: nunca sobrescribe una fila existente."""
        try:
            self.api().put_item(
                TableName=self._table_name,
                Item=record.to_item(),
                ConditionExpression=PUT_CONDITION,
            )
        except (ClientError, BotoCoreError) as exc:
            raise index_error("put_item", exc, IndexWriteException) from sanitize_aws_error(
                exc, include_message=False
            )

    def batch_get(self, sandbox_ids: Sequence[str]) -> dict[str, IndexRecord]:
        """Las filas vigentes de `sandbox_ids`, en trozos de 100 con lectura
        eventualmente consistente; reintenta `UnprocessedKeys` con un backoff
        acotado y, si siguen sin procesar, lanza (nunca una lista incompleta
        en silencio). Las filas caducadas o con otra forma se descartan."""
        found: dict[str, IndexRecord] = {}
        now = self.now()
        for chunk in chunks(sandbox_ids):
            for item in self._batch_get_chunk(chunk):
                record = IndexRecord.from_item(item)
                if record is not None and record.expires_at >= now:
                    found[record.sandbox_id] = record
        return found

    def prepare(self) -> None:
        """Comprobación previa sin llamadas a AWS que `create()` hace antes de
        `run-microvm`: construye el cliente `dynamodb` (construirlo no llama a
        AWS) y lanza `InvalidArgumentException` si no hay región, así un
        error de configuración nunca lanza (ni factura) un MicroVM."""
        try:
            self.api()
        except NoRegionError as exc:
            raise InvalidArgumentException(
                "falta la región del índice: pasa DynamoDbIndex(region=...) o define AWS_REGION"
            ) from exc

    def api(self) -> DynamoDbApi:
        """El cliente `dynamodb`, construido en el primer uso."""
        client: DynamoDbApi = self._client.get()
        return client

    def _batch_get_chunk(self, ids: list[str]) -> list[Mapping[str, Any]]:
        request: dict[str, Any] = {
            self._table_name: {
                "Keys": [{"pk": {"S": sandbox_id}} for sandbox_id in ids],
                "ConsistentRead": False,
            }
        }
        items: list[Mapping[str, Any]] = []
        delay = UNPROCESSED_FIRST_DELAY_SECONDS
        for attempt in range(UNPROCESSED_RETRY_ATTEMPTS + 1):
            if attempt > 0:
                self._sleep(delay)
                delay = min(delay * 2, UNPROCESSED_MAX_DELAY_SECONDS)
            try:
                response = self.api().batch_get_item(RequestItems=request)
            except (ClientError, BotoCoreError) as exc:
                raise index_error(
                    "batch_get_item", exc, SandboxIndexException
                ) from sanitize_aws_error(exc, include_message=False)
            items.extend(response.get("Responses", {}).get(self._table_name, []))
            unprocessed = response.get("UnprocessedKeys") or {}
            pending = unprocessed.get(self._table_name)
            if not pending or not pending.get("Keys"):
                return items
            request = {self._table_name: pending}
        raise SandboxIndexException(
            "DynamoDB dejó claves sin procesar en BatchGetItem tras "
            f"{UNPROCESSED_RETRY_ATTEMPTS} reintentos: vuelve a listar más tarde",
            aws_code="UnprocessedKeys",
        )


def validate_index(index: object) -> DynamoDbIndex | None:
    if index is None or isinstance(index, DynamoDbIndex):
        return index
    raise InvalidArgumentException(
        f"index debe ser un DynamoDbIndex o None, recibido {type(index).__name__}"
    )


def write_failure(
    index: DynamoDbIndex, sandbox_id: str, exc: Exception, log: logging.Logger
) -> IndexWriteException | None:
    """Qué hace `create()` cuando `PutItem` falla: con `'warn'`, avisa (sin
    metadatos) y devuelve `None`; con `'terminate'`, el error a lanzar (el
    llamante termina antes el VM salvo `keep_on_failure`)."""
    code = getattr(exc, "aws_code", None)
    if index.on_write_failure == "warn":
        log.warning(
            "índice de metadatos: no se pudo escribir la fila de %s (%s); el sandbox sigue "
            "vivo pero no aparecerá en list(metadata=, index=)",
            sandbox_id,
            code or type(exc).__name__,
        )
        return None
    if isinstance(exc, IndexWriteException):
        return exc
    return IndexWriteException(
        f"no se pudo escribir la fila del índice ({type(exc).__name__})", aws_code=code
    )


__all__ = [
    "BATCH_GET_MAX_KEYS",
    "DEFAULT_TTL_MARGIN_SECONDS",
    "PUT_CONDITION",
    "RECORD_ATTRIBUTES",
    "SDK_TAG",
    "DynamoDbApi",
    "DynamoDbIndex",
    "IndexRecord",
    "WriteFailurePolicy",
    "joined",
    "record_for",
    "validate_index",
    "write_failure",
]

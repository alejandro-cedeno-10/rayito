"""`CustomDomain` (m15-custom-domain, ADR-024): dominio propio sobre
CloudFront para `sbx.get_host(port)`/`sbx.expose(port)`. `deploy`/`status`/
`destroy` son una fachada fina sobre `OptionalStacks` (M15 foundations,
ADR-016), tal como describe `_stacks/_service.py`; `register`/`unregister`/
`host_for` son el contrato propio de esta función: qué ruta escribe en el
`KeyValueStore` de la distribución y cómo se calcula su hostname.

**Integración pendiente con `Sandbox.create(domain=)`/`get_host()`/
`expose()`:** `sandbox_{sync,async}/main.py` sólo expone kwargs,
delegaciones y exports de M15 foundations (`_feature_options.py`:
`"domain="` sigue lanzando `UnimplementedError` mientras esto no cambie).
Cablear `get_host()`/`expose()` para que usen esta clase necesita una
`HostResolver` que foundations no llegó a pre-crear (la arquitectura de
M15 §1(g) lo daba por hecho); hacerlo aquí, dentro de un fichero que ese
mismo documento marca "sólo foundations", se consideró más arriesgado que
dejarlo como seguimiento explícito — el mismo criterio que usó
`v06-foundations` para no activar el reaper de zombies a medias. Mientras
tanto, `CustomDomain` es utilizable de forma independiente: desplegar la
pila, y registrar/desregistrar rutas a mano con el JWE que ya emite
`create-microvm-auth-token` (`AWS_API_NOTES.md` §8) y el `endpoint` del
sandbox.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

import boto3

from rayito._custom_domain._domain import (
    RouteMetadata,
    check_kvs_value_size,
    kvs_json_key,
    kvs_meta_key,
    route_host,
    route_label,
    traffic_token_digest,
    validate_public_domain,
)
from rayito._custom_domain._kvs import (
    RESOURCE_NOT_FOUND_CODE,
    CloudFrontKvsWriter,
    KeyValueStoreWriter,
)
from rayito._stacks._model import StackStatus
from rayito._stacks._port import StackProvisioner
from rayito._stacks._service import DEFAULT_WAIT_TIMEOUT_SECONDS, OptionalStacks
from rayito.exceptions import CustomDomainException, InvalidArgumentException

#: Nombre fijo del componente `OptionalStack` que despliega esta función
#: (`infra/custom-domain.yaml`, `_stacks/components/custom_domain.py`).
STACK_COMPONENT: Final = "custom-domain"

#: La salida `KvsArn` de la plantilla (`infra/custom-domain.yaml`), leída
#: tras `deploy()`/`status()` para no pedírsela al llamante aparte.
KVS_ARN_OUTPUT_KEY: Final = "KvsArn"


@dataclass(frozen=True)
class CustomDomainRoute:
    """Una ruta ya registrada: su hostname y cuándo caduca el JWE que
    cubre (`refresh()` la extiende sin cambiar ninguno de estos campos
    salvo `expires_at`)."""

    alias: str
    port: int
    public_domain: str
    expires_at: datetime

    @property
    def label(self) -> str:
        return route_label(self.alias, self.port)

    @property
    def host(self) -> str:
        return route_host(self.alias, self.port, self.public_domain)


def _resolve_kvs_arn(explicit: str | None, status: StackStatus | None) -> str | None:
    if explicit:
        return explicit
    if status is not None:
        return status.outputs.get(KVS_ARN_OUTPUT_KEY)
    return None


class CustomDomain:
    """Construirlo no hace ninguna llamada a AWS.

    Coste y activación
    -------------------
    Activa: `CustomDomain(public_domain=...).deploy(certificate_arn=...)`.
    Recursos y llamadas AWS: una distribución CloudFront, su CloudFront
        Function de enrutado y un KeyValueStore (`infra/custom-domain.yaml`,
        `rayito stack deploy custom-domain`). En uso, `register()`/
        `unregister()`/`refresh()` llaman a `DescribeKeyValueStore`/`PutKey`/
        `DeleteKey` (`AWS_API_NOTES.md` §29); nada de esto se llama sin
        invocarlos explícitamente. No hay refresher automático en este
        cambio (seguimiento no bloqueante, ver `_stacks/components/
        custom_domain.py`): llama a `refresh()` tú mismo antes de que
        caduque el JWE de una ruta (DOM-7).
    Coste aproximado: CloudFront ~$0,085/GB + $0,0075/10 000 peticiones
        HTTPS (datos salientes, `us-east-1`, acceso 2026-09-30); KeyValueStore
        $0 en reposo y $0,0000004 por lectura/escritura.
    IAM: `cloudformation:*Stack*` para `deploy`/`destroy` (vía `OptionalStacks`);
        `cloudfront-keyvaluestore:DescribeKeyValueStore/PutKey/DeleteKey` sobre
        el KVS de la pila para `register`/`unregister`/`refresh`.
    Cómo apagarla: no llames a `deploy()`; `destroy()` borra la distribución,
        la Function y el KVS — ningún dato de sandbox se conserva, las
        rutas son efímeras.
    Ejemplo:
        domain = CustomDomain(public_domain="sbx.example.com")
        domain.deploy(certificate_arn="arn:aws:acm:us-east-1:...:certificate/...")
        route = domain.register("ws-7", 8000, endpoint=sbx.endpoint, jwe=jwe, ttl_seconds=2400)
        route.host  # "8000-ws-7.sbx.example.com"
        domain.unregister("ws-7", 8000)
    """

    def __init__(
        self,
        *,
        public_domain: str,
        stack_name: str | None = None,
        kvs_arn: str | None = None,
        region: str | None = None,
        session: boto3.session.Session | None = None,
        provisioner: StackProvisioner | None = None,
        kvs_writer: KeyValueStoreWriter | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.public_domain = validate_public_domain(public_domain)
        self._stack_name = stack_name
        self._explicit_kvs_arn = kvs_arn
        self._stacks = OptionalStacks(region=region, session=session, provisioner=provisioner)
        self._kvs: KeyValueStoreWriter = kvs_writer or CloudFrontKvsWriter(
            region=region, session=session
        )
        self._clock = clock
        self._cached_kvs_arn: str | None = None

    def deploy(
        self,
        *,
        certificate_arn: str,
        tags: dict[str, str] | None = None,
        wait: bool = True,
        wait_timeout: float = DEFAULT_WAIT_TIMEOUT_SECONDS,
    ) -> StackStatus:
        """Despliega (o actualiza) `infra/custom-domain.yaml`. `certificate_arn`
        debe estar en `us-east-1` (requisito de CloudFront, D3: lo aporta
        el mantenedor)."""
        status = self._stacks.deploy(
            STACK_COMPONENT,
            stack_name=self._stack_name,
            parameters={
                "PublicDomain": self.public_domain,
                "CertificateArn": certificate_arn,
            },
            tags=tags,
            wait=wait,
            wait_timeout=wait_timeout,
        )
        self._cached_kvs_arn = _resolve_kvs_arn(None, status)
        return status

    def status(self) -> StackStatus | None:
        status = self._stacks.status(STACK_COMPONENT, stack_name=self._stack_name)
        self._cached_kvs_arn = _resolve_kvs_arn(None, status)
        return status

    def destroy(
        self, *, wait: bool = True, wait_timeout: float = DEFAULT_WAIT_TIMEOUT_SECONDS
    ) -> None:
        self._stacks.destroy(
            STACK_COMPONENT, stack_name=self._stack_name, wait=wait, wait_timeout=wait_timeout
        )
        self._cached_kvs_arn = None

    def kvs_arn(self) -> str:
        """El ARN del `KeyValueStore` de la pila: el pasado al construir
        `CustomDomain`, o el de la última `deploy()`/`status()`. Nunca llama
        a AWS por sí sola: si no hay ninguno, llama a `status()` primero."""
        resolved = _resolve_kvs_arn(self._explicit_kvs_arn, None) or self._cached_kvs_arn
        if resolved is None:
            raise CustomDomainException(
                "no se conoce el KvsArn de la pila: pasa kvs_arn= al construir CustomDomain "
                "o llama a status()/deploy() primero"
            )
        return resolved

    def host_for(self, alias: str, port: int) -> str:
        """El hostname público de una ruta; no llama a AWS ni exige que la
        ruta esté ya registrada (pura, `_domain.route_host`)."""
        return route_host(alias, port, self.public_domain)

    def register(
        self,
        alias: str,
        port: int,
        *,
        endpoint: str,
        jwe: str,
        traffic_token: str | None = None,
        ttl_seconds: int,
    ) -> CustomDomainRoute:
        """Escribe las dos claves de la ruta (`j:`/`m:`) con `ETag`
        encadenado: dos `PutKey`, nunca uno sin el otro a medias si el
        primero falla (la excepción deja el KVS sin la ruta, no con sólo el
        JWE y sin metadatos)."""
        if ttl_seconds <= 0:
            raise InvalidArgumentException(f"ttl_seconds debe ser positivo: {ttl_seconds}")
        check_kvs_value_size(jwe)
        label = route_label(alias, port)
        expires_at = int(self._clock()) + ttl_seconds
        metadata = RouteMetadata(
            endpoint=endpoint,
            traffic_token_sha256=traffic_token_digest(traffic_token),
            expires_at=expires_at,
        )
        kvs_arn = self.kvs_arn()
        etag = self._kvs.describe(kvs_arn)
        etag = self._kvs.put(kvs_arn, kvs_json_key(label), jwe, if_match=etag)
        self._kvs.put(kvs_arn, kvs_meta_key(label), metadata.encode(), if_match=etag)
        return CustomDomainRoute(
            alias=alias,
            port=port,
            public_domain=self.public_domain,
            expires_at=datetime.fromtimestamp(expires_at, tz=UTC),
        )

    def refresh(self, route: CustomDomainRoute, *, jwe: str, ttl_seconds: int) -> CustomDomainRoute:
        """Reescribe sólo `j:<label>` con un JWE nuevo (el `TokenRefresher`
        del transporte ya renueva el suyo a los 45 min,
        `_transport.TOKEN_REFRESH_AFTER_MINUTES`; esto hace lo mismo para la
        copia que vive en el KVS). `m:<label>` no cambia: el endpoint y el
        hash del `traffic_token` siguen siendo los mismos."""
        if ttl_seconds <= 0:
            raise InvalidArgumentException(f"ttl_seconds debe ser positivo: {ttl_seconds}")
        check_kvs_value_size(jwe)
        label = route.label
        kvs_arn = self.kvs_arn()
        etag = self._kvs.describe(kvs_arn)
        self._kvs.put(kvs_arn, kvs_json_key(label), jwe, if_match=etag)
        expires_at = int(self._clock()) + ttl_seconds
        return CustomDomainRoute(
            alias=route.alias,
            port=route.port,
            public_domain=route.public_domain,
            expires_at=datetime.fromtimestamp(expires_at, tz=UTC),
        )

    def unregister(self, alias: str, port: int) -> None:
        """Idempotente: una ruta que ya no está no es un error
        (`ResourceNotFoundException` del `DeleteKey` se traga aquí, no en
        el adaptador — llegar aquí ya exige que el almacén exista)."""
        label = route_label(alias, port)
        kvs_arn = self.kvs_arn()
        etag = self._kvs.describe(kvs_arn)
        for key in (kvs_json_key(label), kvs_meta_key(label)):
            try:
                etag = self._kvs.delete(kvs_arn, key, if_match=etag)
            except CustomDomainException as exc:
                if exc.aws_code != RESOURCE_NOT_FOUND_CODE:
                    raise
                etag = self._kvs.describe(kvs_arn)


__all__ = ["CustomDomain", "CustomDomainRoute"]

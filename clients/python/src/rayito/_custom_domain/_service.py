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
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

import boto3

from rayito._custom_domain._domain import (
    CFN_LIST_SEPARATOR,
    RouteMetadata,
    check_kvs_value_size,
    kvs_json_key,
    kvs_meta_key,
    route_host,
    route_label,
    traffic_token_digest,
    validate_alternate_domain_names,
    validate_public_domain,
)
from rayito._custom_domain._kvs import (
    RESOURCE_NOT_FOUND_CODE,
    CloudFrontKvsWriter,
    KeyValueStoreWriter,
)
from rayito._stacks._model import StackStatus
from rayito._stacks._port import StackProvisioner
from rayito._stacks._service import OptionalStacks
from rayito.exceptions import CustomDomainException, InvalidArgumentException

#: Nombre fijo del componente `OptionalStack` que despliega esta función
#: (`infra/custom-domain.yaml`, `_stacks/components/custom_domain.py`).
STACK_COMPONENT: Final = "custom-domain"

#: El parámetro `CommaDelimitedList` de `infra/custom-domain.yaml` con los
#: nombres alternativos explícitos; vacío (su valor por defecto) deja el
#: comodín `*.<PublicDomain>`.
ALTERNATE_DOMAIN_NAMES_PARAMETER: Final = "AlternateDomainNames"

#: La salida `DistributionDomainName` de la plantilla (`*.cloudfront.net`):
#: el destino del `CNAME`/alias de DNS de tu dominio.
DISTRIBUTION_DOMAIN_NAME_OUTPUT_KEY: Final = "DistributionDomainName"

#: La salida `KvsArn` de la plantilla (`infra/custom-domain.yaml`), leída
#: tras `deploy()`/`status()` para no pedírsela al llamante aparte.
KVS_ARN_OUTPUT_KEY: Final = "KvsArn"

#: Reintentos acotados de "describe + put(s) encadenados" cuando AWS
#: rechaza el `ETag` por una carrera con otro escritor de la misma ruta
#: (`register`/`refresh` concurrentes). No verificado contra una
#: distribución real: la lista de códigos de abajo es defensiva, no
#: una lista cerrada como el resto de `AWS_API_NOTES.md` §29.
MAX_ETAG_CONFLICT_RETRIES: Final = 3

#: Códigos de error que `_write_route` trata como "el `ETag` ya no es el
#: vigente, vuelve a intentarlo desde `describe()`": `ConflictException` es
#: lo que produce el fake de test (`FakeKeyValueStoreWriter`);
#: `PreconditionFailedException` se añade defensivamente por si el servicio
#: real usa ese nombre para un `IfMatch` que no coincide (sin confirmar
#: contra una distribución real).
_ETAG_CONFLICT_AWS_CODES: Final = ("ConflictException", "PreconditionFailedException")

#: Prefijo del recurso de `infra/custom-domain.yaml` con el nombre más largo
#: de los dos que llevan `${AWS::StackName}` (`RouterFunction`; el
#: `KeyValueStore` usa uno más corto, así que no es el que limita). Debe
#: mantenerse igual que esa plantilla a mano: no hay aserción cruzada,
#: `scripts/tests/test_custom_domain_function_sync.py` no cubre nombres de
#: recurso.
_ROUTER_FUNCTION_NAME_PREFIX: Final = "rayito-custom-domain-router-"

#: Límite de `AWS::CloudFront::Function`'s `Name` (64 caracteres,
#: https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/cloudfront-limits.html).
#: Un `--stack-name` que lo supere hace que CloudFormation rechace la
#: plantilla con un error que no menciona el límite en absoluto (hallazgo
#: del review de PR #74) — mejor fallar aquí con un mensaje claro.
_CLOUDFRONT_FUNCTION_NAME_MAX_LENGTH: Final = 64

#: Longitud máxima de `stack_name` para que `RouterFunction`'s `Name` no
#: supere `_CLOUDFRONT_FUNCTION_NAME_MAX_LENGTH` (36 con el prefijo de
#: arriba: por defecto, "rayito-custom-domain" deja de sobrar margen a
#: partir de los 37 caracteres).
MAX_STACK_NAME_LENGTH: Final = _CLOUDFRONT_FUNCTION_NAME_MAX_LENGTH - len(
    _ROUTER_FUNCTION_NAME_PREFIX
)

#: `deploy()`/`destroy()` esperan a que CloudFormation termine; el valor por
#: defecto de `OptionalStacks` (`DEFAULT_WAIT_TIMEOUT_SECONDS`, 600 s) basta
#: para la mayoría de componentes pero no para éste: una distribución
#: CloudFront tarda ~15 min sólo en deshabilitarse antes de poder borrarse
#: (`_stacks/components/custom_domain.py::COMPONENT.cost.removal`,
#: `dominio-propio.md`), y crearla/actualizarla también puede superar los
#: 10 min. 1800 s (30 min) de margen; cifra exacta por confirmar como DOM-15
#: en la etapa de aceptación contra AWS real (`design.md` de este cambio).
CUSTOM_DOMAIN_WAIT_TIMEOUT_SECONDS: Final = 1800.0


@dataclass(frozen=True)
class CustomDomainRoute:
    """Una ruta ya registrada: su hostname y cuándo caduca el JWE que
    cubre. `endpoint`/`traffic_token_sha256` son los metadatos que
    `register()` ya escribió en `m:<label>`; `refresh()` los necesita de
    vuelta para reescribir esa clave con la misma `endpoint`/hash y sólo
    `expires_at` al día."""

    alias: str
    port: int
    public_domain: str
    endpoint: str
    traffic_token_sha256: str
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
        HTTPS (datos salientes, `us-east-1`); CloudFront Functions ~$0,10 por
        1 000 000 de invocaciones (una por petición); KeyValueStore $0 en
        reposo, ~$0,50 por 1 000 000 de lecturas (las de la Function) y ~$5
        por 1 000 000 de llamadas de gestión (`PutKey`/`DeleteKey` de
        `register`/`unregister`/`refresh`) — tres líneas separadas, no una
        cifra combinada (`_stacks/components/custom_domain.py`, cifras de
        lista de CloudFront Functions/KeyValueStore desde su lanzamiento,
        reconfirmar en la etapa de aceptación AWS).
    IAM: `cloudformation:*Stack*` para `deploy`/`destroy` (vía `OptionalStacks`);
        `cloudfront-keyvaluestore:DescribeKeyValueStore/PutKey/DeleteKey` sobre
        el KVS de la pila para `register`/`unregister`/`refresh`.
    Cómo apagarla: no llames a `deploy()`; `destroy()` borra la distribución,
        la Function y el KVS — ningún dato de sandbox se conserva, las
        rutas son efímeras.
    Ejemplo:
        domain = CustomDomain(public_domain="sbx.example.com")
        domain.deploy(certificate_arn="arn:aws:acm:us-east-1:...:certificate/...")
        route = domain.register(
            "ws-7", 8000, endpoint=sbx.endpoint, jwe=jwe,
            traffic_token=secrets.token_urlsafe(32), ttl_seconds=2400,
        )
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
        if stack_name is not None and len(stack_name) > MAX_STACK_NAME_LENGTH:
            raise InvalidArgumentException(
                f"stack_name demasiado largo ({len(stack_name)} car., máximo "
                f"{MAX_STACK_NAME_LENGTH}): el nombre de RouterFunction "
                f"({_ROUTER_FUNCTION_NAME_PREFIX}<stack_name>) no puede superar "
                f"{_CLOUDFRONT_FUNCTION_NAME_MAX_LENGTH} caracteres"
            )
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
        alternate_domain_names: Sequence[str] | None = None,
        tags: dict[str, str] | None = None,
        wait: bool = True,
        wait_timeout: float = CUSTOM_DOMAIN_WAIT_TIMEOUT_SECONDS,
    ) -> StackStatus:
        """Despliega (o actualiza) `infra/custom-domain.yaml`. `certificate_arn`
        debe estar en `us-east-1` (requisito de CloudFront) y cubrir los
        nombres de la distribución: `*.<public_domain>` por defecto.

        `alternate_domain_names` sustituye ese comodín por una lista
        explícita (`_domain.validate_alternate_domain_names`; normalmente
        `host_for(alias, port)` de rutas conocidas), p. ej. si otra
        distribución ya tiene `*.<public_domain>`. Las rutas cuyo host no
        esté en la lista no llegan a esta distribución. No cambia el coste.
        Tras desplegar, apunta un `CNAME`/alias de DNS de cada nombre al
        `DistributionDomainName` de la salida."""
        parameters = {"PublicDomain": self.public_domain, "CertificateArn": certificate_arn}
        if alternate_domain_names is not None:
            parameters[ALTERNATE_DOMAIN_NAMES_PARAMETER] = CFN_LIST_SEPARATOR.join(
                validate_alternate_domain_names(alternate_domain_names, self.public_domain)
            )
        status = self._stacks.deploy(
            STACK_COMPONENT,
            stack_name=self._stack_name,
            parameters=parameters,
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
        self, *, wait: bool = True, wait_timeout: float = CUSTOM_DOMAIN_WAIT_TIMEOUT_SECONDS
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

    def _delete_best_effort(self, kvs_arn: str, key: str) -> None:
        """Borra `key` sin dejar que un fallo tape la excepción original de
        quien llama: `_write_route` lo usa para no dejar un JWE vivo sin
        sus metadatos (o viceversa) cuando una escritura encadenada falla a
        medias."""
        try:
            etag = self._kvs.describe(kvs_arn)
            self._kvs.delete(kvs_arn, key, if_match=etag)
        except CustomDomainException:
            pass

    def _write_route(
        self, kvs_arn: str, writes: tuple[tuple[str, str], ...], *, rollback: bool
    ) -> None:
        """Escribe `writes` (clave, valor) encadenando el `ETag` de un
        `describe()` inicial. Reintenta la secuencia completa desde
        `describe()` hasta `MAX_ETAG_CONFLICT_RETRIES` veces si AWS rechaza
        el `ETag` encadenado por una carrera con otro escritor de la misma
        ruta.

        `rollback` distingue a quién pertenecían las claves antes de esta
        llamada: `register()` de una ruta nueva pasa `True` (si la segunda
        escritura falla, borra en reversa, best-effort, lo que la propia
        llamada acababa de escribir, para no dejar un `j:` vivo sin su
        `m:`). `refresh()` pasa `False`: ahí las claves ya existían con una
        ruta sana sirviendo tráfico, así que un fallo a medias (p. ej. el
        `PutKey` de `m:` tras el de `j:`) debe dejar lo que sí se escribió
        tal cual, nunca borrarlo — borrar un `j:`/`m:` que ya eran la ruta
        en producción convertiría un error transitorio del KVS en una
        caída, aunque fuera breve, de una ruta que funcionaba. La Function
        ignora `m:` huérfano (nunca lo lee), así que dejarlo así es
        inocuo."""
        last_error: CustomDomainException | None = None
        for _ in range(MAX_ETAG_CONFLICT_RETRIES):
            etag = self._kvs.describe(kvs_arn)
            written: list[str] = []
            try:
                for key, value in writes:
                    etag = self._kvs.put(kvs_arn, key, value, if_match=etag)
                    written.append(key)
            except CustomDomainException as exc:
                if rollback:
                    for key in reversed(written):
                        self._delete_best_effort(kvs_arn, key)
                last_error = exc
                if exc.aws_code in _ETAG_CONFLICT_AWS_CODES:
                    continue
                raise
            return
        assert last_error is not None  # el bucle sólo termina así tras agotar los reintentos
        raise last_error

    def register(
        self,
        alias: str,
        port: int,
        *,
        endpoint: str,
        jwe: str,
        traffic_token: str | None = None,
        public: bool = False,
        ttl_seconds: int,
    ) -> CustomDomainRoute:
        """Escribe las dos claves de la ruta (`j:`/`m:`) con `ETag`
        encadenado (`_write_route(..., rollback=True)`: ninguna de las dos
        claves existía antes para este `label`, así que si la segunda
        escritura falla, la primera se borra best-effort antes de relanzar
        — nunca se deja un JWE vivo sin sus metadatos). Una ruta es pública
        (sin `traffic_token` que comprobar) sólo con `public=True`, nunca
        por omisión: sin `traffic_token` ni `public=True` se rechaza antes
        de tocar el KVS (SEC-T25 — el valor por defecto nunca es
        "pública")."""
        if not public and traffic_token is None:
            raise InvalidArgumentException(
                "register() necesita traffic_token (o public=True para una ruta pública "
                "a propósito: nunca es el valor por defecto implícito)"
            )
        if ttl_seconds <= 0:
            raise InvalidArgumentException(f"ttl_seconds debe ser positivo: {ttl_seconds}")
        check_kvs_value_size(jwe)
        label = route_label(alias, port)
        expires_at = int(self._clock()) + ttl_seconds
        traffic_token_sha256 = traffic_token_digest(traffic_token)
        metadata = RouteMetadata(
            endpoint=endpoint, traffic_token_sha256=traffic_token_sha256, expires_at=expires_at
        ).encode()
        self._write_route(
            self.kvs_arn(),
            ((kvs_json_key(label), jwe), (kvs_meta_key(label), metadata)),
            rollback=True,
        )
        return CustomDomainRoute(
            alias=alias,
            port=port,
            public_domain=self.public_domain,
            endpoint=endpoint,
            traffic_token_sha256=traffic_token_sha256,
            expires_at=datetime.fromtimestamp(expires_at, tz=UTC),
        )

    def refresh(self, route: CustomDomainRoute, *, jwe: str, ttl_seconds: int) -> CustomDomainRoute:
        """Reescribe `j:<label>` con un JWE nuevo (el `TokenRefresher` del
        transporte ya renueva el suyo a los 45 min,
        `_transport.TOKEN_REFRESH_AFTER_MINUTES`; esto hace lo mismo para la
        copia que vive en el KVS) y también `m:<label>`, para que su
        expiración no quede obsoleta: mismo `endpoint`/hash de
        `traffic_token` que `route` ya tenía, sólo `expires_at` al día.
        `_write_route(..., rollback=False)`: a diferencia de `register()`,
        aquí ambas claves ya existían con una ruta sana sirviendo tráfico,
        así que un fallo a medias (p. ej. el `PutKey` de `m:` tras el de
        `j:`) no debe borrar lo que sí se escribió — eso dejaría una ruta
        que funcionaba sin su `j:` o su `m:` por un error transitorio del
        KVS."""
        if ttl_seconds <= 0:
            raise InvalidArgumentException(f"ttl_seconds debe ser positivo: {ttl_seconds}")
        check_kvs_value_size(jwe)
        label = route.label
        expires_at = int(self._clock()) + ttl_seconds
        metadata = RouteMetadata(
            endpoint=route.endpoint,
            traffic_token_sha256=route.traffic_token_sha256,
            expires_at=expires_at,
        ).encode()
        self._write_route(
            self.kvs_arn(),
            ((kvs_json_key(label), jwe), (kvs_meta_key(label), metadata)),
            rollback=False,
        )
        return CustomDomainRoute(
            alias=route.alias,
            port=route.port,
            public_domain=route.public_domain,
            endpoint=route.endpoint,
            traffic_token_sha256=route.traffic_token_sha256,
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

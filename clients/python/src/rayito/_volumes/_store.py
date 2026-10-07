"""`VolumeStore`: CRUD de volúmenes EFS (access points) sobre AWS, con las
credenciales del llamante (`m15-efs-volumes`, ADR-018, experimental). El
bloque "Coste y activación" está en el docstring de la propia clase (lo que
enseña `help(VolumeStore)`), como en `rayito._secrets.SecretStore`.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from rayito._aws import LazyClient, aws_code
from rayito._aws_region import resolve_region
from rayito._volumes._base import (
    ACCESS_POINT_ALREADY_EXISTS,
    LIST_VISIBILITY_BUDGET_SECONDS,
    LIST_VISIBILITY_POLL_SECONDS,
    EfsApi,
    create_access_point_params,
    translate_error,
    volume_from_description,
)
from rayito._volumes._domain import EfsVolume, validate_file_system_id, validate_volume_name
from rayito.exceptions import VolumeException, VolumeNotFoundException


class VolumeStore:
    """CRUD de volúmenes (access points) de un sistema de ficheros EFS que
    ya existe (desplegado con `rayito stack deploy efs-volumes` o uno
    propio). Una instancia es reutilizable y segura entre hilos.

    Coste y activación
    -------------------
    Activa: `VolumeStore(...)`; construirlo no llama a AWS (el cliente boto3
        `efs` se crea en el primer método). Experimental: lo que devuelve se
        monta con `Sandbox.create(volumes={ruta: vol})` sobre la imagen
        opcional `rayito-base-caps-efs` (`rayito image publish --with-efs`);
        ese `create()` añade una `DescribeMountTargets` por sistema de
        ficheros si el volumen no trae `mount_target_ip`.
    Recursos y llamadas AWS: ningún recurso nuevo (el sistema de ficheros lo
        crea `infra/efs-volumes.yaml`, por separado); `create` = `CreateAccessPoint`,
        `get`/`list` = `DescribeAccessPoints`, `destroy` = `DeleteAccessPoint`.
    Coste aproximado: los access points no tienen cargo propio listado; el
        sistema de ficheros se factura por `infra/efs-volumes.yaml` (ver ese
        componente en `rayito stack list`).
    IAM: `elasticfilesystem:CreateAccessPoint`, `DescribeAccessPoints`,
        `DeleteAccessPoint` (y `DescribeMountTargets` para montar sin
        `mount_target_ip`) sobre el sistema de ficheros (credenciales del
        LLAMANTE, no del execution role del MicroVM).
    Cómo apagarla: no instancies `VolumeStore`; borra con `destroy()` los
        volúmenes que ya no uses (el directorio que cubrían no se borra, sólo el
        access point: borrar los datos exige montar el sistema de ficheros desde
        un sandbox, fuera de alcance en 0.6).
    Ejemplo:
        store = VolumeStore(file_system_id="fs-0123abcd", region="us-east-1")
        vol = store.create("datos-agente-7")
        store.get("datos-agente-7")
        store.list()
        store.destroy("datos-agente-7")
    """

    def __init__(
        self,
        *,
        file_system_id: str,
        region: str | None = None,
        session: boto3.session.Session | None = None,
    ) -> None:
        self._file_system_id = validate_file_system_id(file_system_id)
        self._region = region
        self._session = session
        self._client = LazyClient("efs", region=region, session=session)
        # Reloj y espera del reintento de `create` (Q125): sólo los tests los
        # sustituyen, como en `SecretStore`.
        self._sleep: Callable[[float], None] = time.sleep
        self._clock: Callable[[], float] = time.monotonic

    @property
    def file_system_id(self) -> str:
        return self._file_system_id

    @property
    def region(self) -> str | None:
        return resolve_region(self._region, self._session)

    def __repr__(self) -> str:
        return f"VolumeStore(file_system_id={self._file_system_id!r}, region={self.region!r})"

    # ---------------------------------------------------------------- CRUD

    def create(self, name: str) -> EfsVolume:
        """`CreateAccessPoint` con uid/gid 1000:1000 y la ruta raíz
        `/rayito-volumes/<name>` (research doc §4.5). Idempotente: llamarla
        dos veces con el mismo nombre no crea dos access points
        (`ClientToken` es un hash de `file_system_id`+nombre). AWS no
        devuelve el access point ya creado para un `ClientToken` repetido:
        responde `AccessPointAlreadyExists`, que esta función atrapa para
        devolver `get(name)` en su lugar, reintentado durante
        `LIST_VISIBILITY_BUDGET_SECONDS` porque el listado de EFS tarda en
        mostrar un access point recién creado (Q125)."""
        validate_volume_name(name)
        params = create_access_point_params(self._file_system_id, name)
        try:
            described = self._call("create_access_point", **params)
        except VolumeException as exc:
            if exc.__cause__ is not None and aws_code(exc.__cause__) == ACCESS_POINT_ALREADY_EXISTS:
                return self._get_once_listed(name)
            raise
        return EfsVolume(
            file_system_id=self._file_system_id,
            access_point_id=str(described.get("AccessPointId", "")),
            name=name,
            region=self.region,
        )

    def get(self, name: str) -> EfsVolume:
        """`DescribeAccessPoints(FileSystemId=...)` filtrado por la
        etiqueta `rayito:volume=<name>`. `VolumeNotFoundException` si no
        existe ninguno con ese nombre."""
        validate_volume_name(name)
        for volume in self.list():
            if volume.name == name:
                return volume
        raise VolumeNotFoundException(f"no existe un volumen llamado {name!r}")

    def list(self) -> tuple[EfsVolume, ...]:
        """Todos los access points del sistema de ficheros con la etiqueta
        de volumen de Rayito, paginando `DescribeAccessPoints` hasta
        agotar `NextToken`. Eventualmente consistente, como el propio
        `DescribeAccessPoints` (Q125): un volumen recién creado puede tardar
        unos segundos en aparecer y uno recién borrado seguir apareciendo
        (también en `get`)."""
        volumes: list[EfsVolume] = []
        next_token: str | None = None
        while True:
            params: dict[str, Any] = {"FileSystemId": self._file_system_id}
            if next_token:
                params["NextToken"] = next_token
            response = self._call("describe_access_points", **params)
            for described in response.get("AccessPoints", []):
                volume = volume_from_description(described, region=self.region)
                if volume.name is not None:
                    volumes.append(volume)
            next_token = response.get("NextToken")
            if not next_token:
                break
        return tuple(volumes)

    def destroy(self, name: str) -> bool:
        """`DeleteAccessPoint`; los ficheros bajo su directorio raíz no se
        borran (honesto: borrarlos exigiría montar el sistema de ficheros,
        fuera de alcance en 0.6). `True` si existía y se borró; `False` si
        no existía ningún volumen con ese nombre."""
        try:
            volume = self.get(name)
        except VolumeNotFoundException:
            return False
        try:
            self._call("delete_access_point", AccessPointId=volume.access_point_id)
        except VolumeNotFoundException:
            # El listado aún mostraba un access point ya borrado (Q125).
            return False
        return True

    def _get_once_listed(self, name: str) -> EfsVolume:
        """`get(name)` de un access point que EFS acaba de confirmar que
        existe (`AccessPointAlreadyExists`) pero que su listado todavía
        puede no mostrar: reintenta hasta `LIST_VISIBILITY_BUDGET_SECONDS`."""
        deadline = self._clock() + LIST_VISIBILITY_BUDGET_SECONDS
        while True:
            try:
                return self.get(name)
            except VolumeNotFoundException:
                if self._clock() >= deadline:
                    raise
            self._sleep(LIST_VISIBILITY_POLL_SECONDS)

    # ------------------------------------------------------------ internals

    def api(self) -> EfsApi:
        """El cliente `efs`, construido en el primer uso."""
        client: EfsApi = self._client.get()
        return client

    def _call(self, operation: str, **params: Any) -> dict[str, Any]:
        method = getattr(self.api(), operation)
        try:
            return dict(method(**params))
        except (ClientError, BotoCoreError) as exc:
            raise translate_error(operation, exc) from exc

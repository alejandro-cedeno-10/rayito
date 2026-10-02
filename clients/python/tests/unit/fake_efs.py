"""Cliente `efs` falso para los tests de `m15-efs-volumes` (ADR-018,
experimental): guarda access points en memoria, indexados por
`AccessPointId`, filtrables por `FileSystemId` y por la etiqueta
`rayito:volume`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from botocore.exceptions import ClientError

REGION = "us-east-1"


def client_error(code: str, message: str, operation: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": message}, "ResponseMetadata": {"HTTPStatusCode": 400}},
        operation,
    )


@dataclass(eq=False)
class FakeEfsApi:
    access_points: dict[str, dict[str, Any]] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    #: Simula el listado eventualmente consistente de EFS (Q99): los
    #: access points de `unlisted` existen pero `describe_access_points` aún
    #: no los devuelve, y los de `stale` ya se borraron pero sí los devuelve.
    unlisted: set[str] = field(default_factory=set)
    stale: dict[str, dict[str, Any]] = field(default_factory=dict)
    _next_id: int = 1

    def create_access_point(self, **params: Any) -> dict[str, Any]:
        """Como EFS de verdad: un `ClientToken` ya usado por un access point
        que sigue vivo es `AccessPointAlreadyExists` (409), nunca el access
        point devuelto directamente — eso lo resuelve `VolumeStore.create`
        llamando a `get(name)`."""
        self.calls.append("create_access_point")
        token = params["ClientToken"]
        for existing in self.access_points.values():
            if existing.get("_token") == token:
                raise client_error("AccessPointAlreadyExists", "ya existe", "CreateAccessPoint")
        name = next(
            (tag["Value"] for tag in params.get("Tags", []) if tag.get("Key") == "rayito:volume"),
            None,
        )
        access_point_id = f"fsap-{self._next_id:08d}"
        self._next_id += 1
        described = {
            "AccessPointId": access_point_id,
            "FileSystemId": params["FileSystemId"],
            "_name": name,
            "_token": token,
        }
        self.access_points[access_point_id] = described
        return dict(described)

    def describe_access_points(self, **params: Any) -> dict[str, Any]:
        self.calls.append("describe_access_points")
        file_system_id = params.get("FileSystemId")
        points = [
            {
                "AccessPointId": ap["AccessPointId"],
                "FileSystemId": ap["FileSystemId"],
                "Tags": [{"Key": "rayito:volume", "Value": ap["_name"]}],
            }
            for ap in [*self.access_points.values(), *self.stale.values()]
            if ap["FileSystemId"] == file_system_id and ap["AccessPointId"] not in self.unlisted
        ]
        return {"AccessPoints": points}

    def delete_access_point(self, **params: Any) -> dict[str, Any]:
        self.calls.append("delete_access_point")
        access_point_id = params["AccessPointId"]
        if access_point_id not in self.access_points:
            raise client_error("AccessPointNotFound", "no existe", "DeleteAccessPoint")
        del self.access_points[access_point_id]
        return {}


@dataclass(eq=False)
class SpySession:
    """Sesión boto3 falsa: `client("efs")` devuelve `api` y queda apuntado;
    cualquier otro servicio es un error del test."""

    api: FakeEfsApi = field(default_factory=FakeEfsApi)
    region_name: str | None = REGION
    built: list[str] = field(default_factory=list)

    def client(self, service: str, **kwargs: Any) -> Any:
        self.built.append(service)
        assert service == "efs", service
        return self.api

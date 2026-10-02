"""La tabla de features de E2B 2.51 sin primitiva en Lambda MicroVMs y sus
motivos (una sola cadena por feature; `src/e2b/unimplemented.ts` lleva las
mismas). Puro: sin I/O.

`unimplemented(feature)` construye el `UnimplementedError` nativo con el
motivo de la tabla y la página de compatibilidad; una clave ausente es un
error de programación que el test de la tabla impide. Las features que no
están en la tabla (kernels, filtros de `list`, imágenes anteriores a M9...)
pasan su motivo explícito.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from types import MappingProxyType
from typing import Any, Final, NoReturn

from rayito.e2b.exceptions import COMPAT_DOC_PATH, UnimplementedError

SNAPSHOT_REASON: Final = (
    "ninguna operación de Lambda MicroVMs copia la memoria de un MicroVM en marcha "
    "(AWS_API_NOTES.md §1 y §15); el análogo de sólo ficheros es checkpoint_files() + "
    "create(persist=)"
)
KEEP_MEMORY_REASON: Final = (
    "suspend-microvm siempre guarda memoria y disco (AWS_API_NOTES.md §5); el análogo es "
    "checkpoint_files() + kill()"
)
MCP_REASON: Final = (
    "cada petición al endpoint necesita además un JWE en cabecera con TTL de 60 min como "
    "máximo (AWS_API_NOTES.md §3 y §7), así que una URL con token fijo no sirve; usa el "
    "servidor rayito-mcp"
)
VOLUME_REASON: Final = (
    "sin un volume_store/volumeStore configurado en el cliente E2B no hay volumen; incluso "
    "configurado, volumes=/volume_mounts sigue en UnimplementedError hasta que la campaña de "
    "medición EFS-1..EFS-20 (AWS_API_NOTES.md §22, m15-efs-volumes) decida un adaptador de "
    "montaje real; usa persist= (S3) o upload_url/download_url mientras tanto"
)
VOLUME_CONTENT_REASON: Final = (
    "no hay plano de datos de ficheros fuera de un MicroVM (SPEC.md §4); conecta un sandbox y "
    "monta el volumen, o usa upload_url/download_url sobre persist="
)

UNIMPLEMENTED_REASONS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "fork": SNAPSHOT_REASON,
        "create_snapshot": SNAPSHOT_REASON,
        "list_snapshots": SNAPSHOT_REASON,
        "delete_snapshot": SNAPSHOT_REASON,
        "connect(on_resume='reboot')": (
            "resume-microvm siempre restaura memoria y disco (AWS_API_NOTES.md §5); el "
            "análogo es reincarnate(), con un id nuevo"
        ),
        "pause(keep_memory=False)": KEEP_MEMORY_REASON,
        "lifecycle.on_timeout.keep_memory=False": KEEP_MEMORY_REASON,
        "network.rules": (
            "no hay un proxy de egress fuera del VM donde inyectar cabeceras: el proxy de "
            "Lambda MicroVMs sólo gestiona el ingress (AWS_API_NOTES.md §7)"
        ),
        "network.mask_request_host": (
            "el proxy de Lambda MicroVMs siempre reenvía Host: <endpoint> y no lo reescribe "
            "(AWS_API_NOTES.md §7)"
        ),
        "network.allow_public_traffic=True": (
            "no existe acceso sin autenticar: toda petición al endpoint exige X-aws-proxy-auth "
            "(AWS_API_NOTES.md §3 y §7); allow_public_traffic=False es el comportamiento "
            "permanente"
        ),
        "iam": (
            "los MicroVMs no emiten tokens con audiencia: la única identidad es el execution "
            "role por IMDSv2 (AWS_API_NOTES.md §9)"
        ),
        "mcp": MCP_REASON,
        "get_mcp_url": MCP_REASON,
        "get_mcp_token": MCP_REASON,
        "Volume": VOLUME_REASON,
        "volume.content": VOLUME_CONTENT_REASON,
        "get_signature": (
            "una firma de envd no autentica en el proxy: el JWE sólo viaja en cabecera o en el "
            "subprotocolo WebSocket (AWS_API_NOTES.md §7); usa upload_url/download_url, que "
            "firman en S3"
        ),
        "Template": (
            "SPEC.md §4 deja fuera los templates declarativos; construye la imagen con un "
            "Dockerfile y rayito image publish"
        ),
    }
)

TEMPLATE_METHODS: Final = (
    "build",
    "build_in_background",
    "get_build_status",
    "exists",
    "alias_exists",
    "assign_tags",
    "remove_tags",
    "get_tags",
    "to_json",
    "to_dockerfile",
)


def unimplemented(feature: str, reason: str | None = None) -> UnimplementedError:
    """El `UnimplementedError` del shim: el motivo de la tabla D14 cuando no
    se da uno explícito."""
    resolved = UNIMPLEMENTED_REASONS[feature] if reason is None else reason
    return UnimplementedError(feature, resolved, doc=COMPAT_DOC_PATH)


def raiser(feature: str) -> Callable[..., NoReturn]:
    def raise_unimplemented(*args: Any, **kwargs: Any) -> NoReturn:
        raise unimplemented(feature)

    return raise_unimplemented


class UnimplementedMember:
    """Un miembro de E2B que siempre lanza, accedido desde la instancia o
    desde la clase (`sbx.fork()` y `Sandbox.fork(sandbox_id)`), con
    cualquier forma de llamada."""

    def __init__(self, feature: str) -> None:
        self._feature = feature

    def __get__(
        self, instance: object | None, owner: type | None = None
    ) -> Callable[..., NoReturn]:
        return raiser(self._feature)


def unimplemented_resource(name: str, feature: str, methods: Sequence[str]) -> type[Any]:
    """Una clase de E2B (`Template`/`AsyncTemplate`) cuyo constructor y cuyos
    classmethods públicos lanzan `unimplemented(feature)`: nunca
    `AttributeError`. `Volume`/`AsyncVolume` usan su propio patrón en
    `e2b/_volume.py`, porque a diferencia de `Template` sí llegan a tener
    una implementación real (configurando `E2B(volume_store=...)`)."""

    def refuse_instance(cls: type, *args: Any, **kwargs: Any) -> NoReturn:
        raise unimplemented(feature)

    namespace: dict[str, Any] = {
        "__doc__": f"`{name}` de E2B: sin equivalente en Rayito ({feature}).",
        "__module__": "rayito.e2b",
        "__new__": refuse_instance,
    }
    namespace.update({method: UnimplementedMember(feature) for method in methods})
    return type(name, (), namespace)


Template: type[Any] = unimplemented_resource("Template", "Template", TEMPLATE_METHODS)
AsyncTemplate: type[Any] = unimplemented_resource("AsyncTemplate", "Template", TEMPLATE_METHODS)
# Volume/AsyncVolume live in e2b/_volume.py (m15-efs-volumes): real CRUD once
# E2B(volume_store=...) configures one, UnimplementedError("Volume") otherwise.


def get_signature(*args: Any, **kwargs: Any) -> NoReturn:
    """`e2b.get_signature`: una firma de envd no sirve con el proxy de AWS."""
    raise unimplemented("get_signature")

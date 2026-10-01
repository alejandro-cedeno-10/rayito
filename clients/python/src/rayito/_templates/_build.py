"""Adaptador de AWS de `Template.build()` (m15-templates, investigación §3.4
paso 2: "mover el núcleo de `cli/_publish.py` a un módulo del SDK
compartido"; aquí no se reusa ese módulo porque pasa a ser de
`sizes-catalog` tras su extracción — la forma de las llamadas es la misma
`create-microvm-image`/`update-microvm-image` y el mismo gate de tres
estados).

Pipeline: resuelve la versión de la imagen base -> descarga su
`codeArtifact` (`s3:GetObject`) -> compone el Dockerfile y el zip
(`_dockerfile.py`/`_artifact.py`) -> sube el artefacto por hash de
contenido (como `cli/_publish.py` `artifact_key`, se salta la subida si ya
existe) -> `create`/`update-microvm-image` -> sondea el gate de tres
estados -> en caso de fallo, relee el grupo de logs
(`logs:GetLogEvents`) y traduce a `BuildException`.

Cada nombre de parámetro AWS usado aquí aparece en `AWS_API_NOTES.md` §27.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final, Protocol

import boto3
from botocore.exceptions import ClientError

from rayito._aws import LazyClient
from rayito._limits import SUPPORTED_MEMORY_MIB
from rayito._templates._artifact import assemble_artifact
from rayito._templates._concurrency import build_slot
from rayito._templates._context import collect_context_files
from rayito._templates._instructions import BASE_IMAGE_KIND, BaseImageRef, CopyStep, TemplateSpec
from rayito._templates._logs import classify_ready_failure, parse_build_failure
from rayito._templates._models import BuildHandle, BuildInfo, BuildStatus
from rayito.exceptions import BuildException, InvalidArgumentException, NotFoundException

#: Clave S3 del artefacto: `rayito/templates/<sha256-del-zip>.zip`, igual
#: de contenido-direccionada que `cli/_publish.py` `artifact_key` (reusar
#: una versión exige el mismo artefacto y la misma configuración).
S3_KEY_PREFIX: Final = "rayito/templates"
#: Grupo de logs de la imagen que compone este template; `AWS_API_NOTES.md`
#: §27 ("`rayito image publish`" usa el mismo prefijo `/rayito/<nombre>`).
LOG_GROUP_PREFIX: Final = "/rayito"
POLL_INTERVAL_SECONDS: Final = 10.0
DEFAULT_BUILD_TIMEOUT_SECONDS: Final = 1800.0
LAUNCHABLE_IMAGE_STATES: Final = frozenset({"CREATED", "UPDATED"})
SETTLED_VERSION_STATES: Final = frozenset({"SUCCESSFUL", "FAILED"})
MISSING_OBJECT_CODES: Final = frozenset({"404", "NoSuchKey", "NotFound", "403"})
ACTIVE_VERSION_STATUS: Final = "ACTIVE"
SUCCESSFUL_VERSION_STATE: Final = "SUCCESSFUL"

#: `rayito image publish` fija el `cpuConfigurations` a ARM64 (único
#: soportado por Lambda MicroVMs hoy): `Template.build()` hereda la misma
#: restricción, documentada como divergencia de E2B (ADR-022).
CPU_ARCHITECTURE: Final = "ARM_64"


class BuildClients(Protocol):
    """Lo que el pipeline de `Template.build()` necesita (Protocol, igual
    que `cli/_publish.py` `PublishClients`): `_Clients` lo implementa con
    clientes `boto3` perezosos; los tests pasan un doble con clientes
    envueltos en `botocore.stub.Stubber`."""

    @property
    def microvms(self) -> Any: ...

    @property
    def s3(self) -> Any: ...

    @property
    def logs(self) -> Any: ...

    @property
    def region(self) -> str: ...

    @property
    def account_id(self) -> str: ...


class _Clients:
    """Clientes `boto3` perezosos de `Template.build()`: construirlo no
    llama a AWS (la prueba de coste cero de m15-templates lo comprueba)."""

    def __init__(self, *, region: str | None, session: boto3.session.Session | None) -> None:
        self._region = region
        self._session = session
        self._microvms = LazyClient("lambda-microvms", region=region, session=session)
        self._s3 = LazyClient("s3", region=region, session=session)
        self._logs = LazyClient("logs", region=region, session=session)
        self._sts = LazyClient("sts", region=region, session=session)
        self._account_id: str | None = None

    @property
    def microvms(self) -> Any:
        return self._microvms.get()

    @property
    def s3(self) -> Any:
        return self._s3.get()

    @property
    def logs(self) -> Any:
        return self._logs.get()

    @property
    def region(self) -> str:
        region = self._region or getattr(self._session, "region_name", None)
        if not region:
            raise InvalidArgumentException(
                "Template.build: sin región: pasa region= o exporta AWS_REGION"
            )
        return region

    @property
    def account_id(self) -> str:
        if self._account_id is None:
            self._account_id = str(self._sts.get().get_caller_identity()["Account"])
        return self._account_id


def _client_error_code(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Code", ""))


def image_arn(clients: BuildClients, name: str) -> str:
    if name.startswith("arn:"):
        return name
    return f"arn:aws:lambda:{clients.region}:{clients.account_id}:microvm-image:{name}"


def _image_exists(clients: BuildClients, arn: str) -> bool:
    try:
        clients.microvms.get_microvm_image(imageIdentifier=arn)
    except ClientError as exc:
        if _client_error_code(exc) == "ResourceNotFoundException":
            return False
        raise
    return True


def resolve_base_version(
    clients: BuildClients, base_arn: str, version: str | None
) -> dict[str, Any]:
    """La versión de la imagen base a componer: la pedida, o la más
    reciente `SUCCESSFUL`/`ACTIVE` (como `run-microvm` sin `imageVersion`
    explícita)."""
    if version is not None:
        return dict(
            clients.microvms.get_microvm_image_version(
                imageIdentifier=base_arn, imageVersion=version
            )
        )
    paginator = clients.microvms.get_paginator("list_microvm_image_versions")
    candidates = [
        item
        for page in paginator.paginate(imageIdentifier=base_arn)
        for item in page["items"]
        if item["state"] == SUCCESSFUL_VERSION_STATE and item["status"] == ACTIVE_VERSION_STATUS
    ]
    if not candidates:
        raise NotFoundException(
            f"la imagen base {base_arn!r} no tiene ninguna versión activa que componer"
        )
    latest = max(candidates, key=lambda item: item["createdAt"])
    return dict(
        clients.microvms.get_microvm_image_version(
            imageIdentifier=base_arn, imageVersion=str(latest["imageVersion"])
        )
    )


def _parse_s3_uri(uri: str) -> tuple[str, str]:
    if not uri.startswith("s3://"):
        raise BuildException(
            "el codeArtifact de la imagen base no es una URI s3:// (Q84 de la investigación: "
            "Lambda MicroVMs sólo acepta zips en S3)",
            reason="base_image_not_s3",
        )
    bucket, _, key = uri[len("s3://") :].partition("/")
    return bucket, key


def fetch_base_artifact(clients: BuildClients, base_version: dict[str, Any]) -> bytes:
    code_artifact = base_version.get("codeArtifact") or {}
    uri = code_artifact.get("uri")
    if not uri:
        raise BuildException(
            "la versión de la imagen base no tiene codeArtifact.uri",
            reason="base_image_missing_artifact",
        )
    bucket, key = _parse_s3_uri(uri)
    content: bytes = clients.s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    return content


def _object_exists(clients: BuildClients, bucket: str, key: str) -> bool:
    try:
        clients.s3.head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        if _client_error_code(exc) in MISSING_OBJECT_CODES:
            return False
        raise
    return True


def upload_artifact(clients: BuildClients, bucket: str, payload: bytes) -> str:
    key = f"{S3_KEY_PREFIX}/{hashlib.sha256(payload).hexdigest()}.zip"
    if not _object_exists(clients, bucket, key):
        clients.s3.put_object(Bucket=bucket, Key=key, Body=payload)
    return f"s3://{bucket}/{key}"


def _validate_memory(memory_mb: int) -> None:
    if memory_mb not in SUPPORTED_MEMORY_MIB:
        raise InvalidArgumentException(
            f"memory_mb debe ser uno de {SUPPORTED_MEMORY_MIB} (RES-1), se pidió {memory_mb}"
        )


def desired_configuration(
    *,
    artifact_uri: str,
    memory_mb: int,
    base_image_version: dict[str, Any],
    log_group: str,
) -> dict[str, Any]:
    """El cuerpo de `create`/`update-microvm-image` para la nueva imagen del
    template. `baseImageArn`/`baseImageVersion`/`buildRoleArn`/`hooks` se
    copian de la versión de `rayito-base` que se está componiendo (**no**
    es su propio `imageArn`/`imageVersion`: esos identifican a
    `rayito-base` misma, que sólo sirve aquí para traer su `codeArtifact`;
    el `baseImageArn` que importa es el que `rayito-base` declaró al
    publicarse, la imagen gestionada de AWS, por ejemplo `al2023-1`)."""
    return {
        "baseImageArn": base_image_version["baseImageArn"],
        "baseImageVersion": str(base_image_version["baseImageVersion"]),
        "buildRoleArn": base_image_version["buildRoleArn"],
        "codeArtifact": {"uri": artifact_uri},
        "resources": [{"minimumMemoryInMiB": memory_mb}],
        "cpuConfigurations": [{"architecture": CPU_ARCHITECTURE}],
        "hooks": base_image_version["hooks"],
        "logging": {"cloudWatch": {"logGroup": log_group}},
    }


def _configuration_matches(version: dict[str, Any], desired: dict[str, Any]) -> bool:
    return all(version.get(key) == value for key, value in desired.items())


def _reusable_version(clients: BuildClients, arn: str, desired: dict[str, Any]) -> str | None:
    paginator = clients.microvms.get_paginator("list_microvm_image_versions")
    matches = [
        item
        for page in paginator.paginate(imageIdentifier=arn)
        for item in page["items"]
        if item["state"] == SUCCESSFUL_VERSION_STATE
        and item["status"] == ACTIVE_VERSION_STATUS
        and _configuration_matches(item, desired)
    ]
    if not matches:
        return None
    return str(max(matches, key=lambda item: item["createdAt"])["imageVersion"])


def submit_build(
    clients: BuildClients, name: str, arn: str, desired: dict[str, Any]
) -> tuple[str, str]:
    request = {**desired, "description": "rayito template build"}
    if _image_exists(clients, arn):
        response = clients.microvms.update_microvm_image(imageIdentifier=arn, **request)
    else:
        response = clients.microvms.create_microvm_image(name=name, **request)
    return str(response["imageArn"]), str(response["imageVersion"])


def _read_gate(clients: BuildClients, arn: str, version: str) -> tuple[str, str, str, str | None]:
    image = clients.microvms.get_microvm_image(imageIdentifier=arn)
    detail = clients.microvms.get_microvm_image_version(imageIdentifier=arn, imageVersion=version)
    return (
        str(image["state"]),
        str(detail["state"]),
        str(detail["status"]),
        detail.get("stateReason"),
    )


def _settled(image_state: str, version_state: str) -> bool:
    return version_state in SETTLED_VERSION_STATES and (
        image_state in LAUNCHABLE_IMAGE_STATES or version_state == "FAILED"
    )


def wait_for_gate(
    clients: BuildClients,
    arn: str,
    version: str,
    timeout_seconds: float,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[str, str, str, str | None]:
    started = time.monotonic()
    while True:
        gate = _read_gate(clients, arn, version)
        if _settled(gate[0], gate[1]) or time.monotonic() - started >= timeout_seconds:
            return gate
        sleep(POLL_INTERVAL_SECONDS)


def _read_build_logs(clients: BuildClients, log_group: str, limit: int = 500) -> list[str]:
    try:
        streams = clients.logs.describe_log_streams(
            logGroupName=log_group, orderBy="LastEventTime", descending=True, limit=1
        )["logStreams"]
    except ClientError:
        return []
    if not streams:
        return []
    events = clients.logs.get_log_events(
        logGroupName=log_group, logStreamName=streams[0]["logStreamName"], limit=limit
    )["events"]
    return [str(event["message"]) for event in events]


def _raise_for_failure(
    clients: BuildClients, log_group: str, version_state: str, state_reason: str | None
) -> None:
    ready_reason = classify_ready_failure(state_reason)
    if ready_reason is not None:
        raise BuildException(state_reason or "el ready cmd falló", reason=ready_reason)
    detail = parse_build_failure(_read_build_logs(clients, log_group))
    raise BuildException(
        state_reason or f"el build terminó en estado {version_state}",
        step=detail.step,
        command=detail.command,
        exit_code=detail.exit_code,
        log_tail=detail.log_tail,
    )


def _compose(
    clients: BuildClients, spec: TemplateSpec, context_dir: Path
) -> tuple[bytes, dict[str, Any]]:
    if spec.base is None or spec.base.kind != BASE_IMAGE_KIND:
        raise InvalidArgumentException(
            "Template.build: llama a from_base_image() antes de construir (0.6 sólo compone "
            "sobre una imagen rayito-base ya publicada)"
        )
    base_arn = image_arn(clients, spec.base.name)
    base_version = resolve_base_version(clients, base_arn, spec.base.version)
    base_zip = fetch_base_artifact(clients, base_version)
    copies = [step for step in spec.steps if isinstance(step, CopyStep)]
    context_files = collect_context_files(context_dir, copies)
    return assemble_artifact(base_zip, spec, context_files), base_version


def _spec_with_base_version(spec: TemplateSpec, base_image_version: str | None) -> TemplateSpec:
    """`from_base_image(version=...)` ya fija la versión en el `TemplateSpec`;
    el `base_image_version=` de `Template.build()` la sobreescribe (sigue
    el mismo convenio que `cpu_count=`/`memory_mb=` de la llamada, no del
    builder)."""
    if spec.base is None or base_image_version is None:
        return spec
    return spec.with_base(BaseImageRef(spec.base.kind, spec.base.name, base_image_version))


def submit(
    template: Any,
    name: str,
    *,
    bucket: str,
    memory_mb: int,
    cpu_count: int | None,
    force: bool,
    base_image_version: str | None,
    build_role_arn: str | None,
    region: str | None,
    session: Any,
    context_dir: Path | None = None,
) -> BuildHandle:
    """Lo común a `build()`/`build_in_background()`: compone el artefacto,
    lo sube y envía `create`/`update-microvm-image`. `cpu_count` y
    `build_role_arn` se validan pero se ignoran (la CPU es siempre
    `ARM_64`, Q87; el rol de build es el de la imagen base,
    `baseImageVersion.buildRoleArn`, el mismo que usa `rayito image
    publish`): documentado en el ADR-022 como alcance de 0.6."""
    _validate_memory(memory_mb)
    del cpu_count, build_role_arn
    spec = _spec_with_base_version(template.spec, base_image_version)
    resolved_context_dir = context_dir if context_dir is not None else Path()
    with build_slot():
        clients = _Clients(region=region, session=session)
        artifact, base_version = _compose(clients, spec, resolved_context_dir)
        artifact_uri = upload_artifact(clients, bucket, artifact)
        arn = image_arn(clients, name)
        log_group = f"{LOG_GROUP_PREFIX}/{name}"
        desired = desired_configuration(
            artifact_uri=artifact_uri,
            memory_mb=memory_mb,
            base_image_version=base_version,
            log_group=log_group,
        )
        if not force:
            reusable = _reusable_version(clients, arn, desired)
            if reusable is not None:
                return BuildHandle(
                    arn=arn, version=reusable, name=name, region=region, session=session
                )
        submitted_arn, version = submit_build(clients, name, arn, desired)
        return BuildHandle(
            arn=submitted_arn, version=version, name=name, region=region, session=session
        )


def get_build_status(handle: BuildHandle) -> BuildStatus:
    clients = _Clients(region=handle.region, session=handle.session)
    image_state, version_state, _version_status, state_reason = _read_gate(
        clients, handle.arn, handle.version
    )
    if not _settled(image_state, version_state):
        return BuildStatus(state="IN_PROGRESS")
    if version_state == "FAILED":
        return BuildStatus(state="FAILED", error_message=state_reason)
    return BuildStatus(
        state="SUCCESSFUL",
        info=BuildInfo(
            template_id=handle.arn, build_id=f"{handle.version}/{handle.name}", alias=handle.name
        ),
    )


def await_build(
    clients: BuildClients,
    handle: BuildHandle,
    timeout: float,
    on_build_logs: Callable[[str], None] | None,
) -> BuildInfo:
    image_state, version_state, _version_status, state_reason = wait_for_gate(
        clients, handle.arn, handle.version, timeout
    )
    log_group = f"{LOG_GROUP_PREFIX}/{handle.name}"
    if on_build_logs is not None:
        for line in _read_build_logs(clients, log_group):
            on_build_logs(line)
    if version_state != SUCCESSFUL_VERSION_STATE or image_state not in LAUNCHABLE_IMAGE_STATES:
        _raise_for_failure(clients, log_group, version_state, state_reason)
    return BuildInfo(
        template_id=handle.arn, build_id=f"{handle.version}/{handle.name}", alias=handle.name
    )


def build(
    template: Any,
    name: str,
    *,
    bucket: str,
    memory_mb: int = 2048,
    cpu_count: int | None = None,
    force: bool = False,
    timeout: float = DEFAULT_BUILD_TIMEOUT_SECONDS,
    base_image_version: str | None = None,
    build_role_arn: str | None = None,
    on_build_logs: Callable[[str], None] | None = None,
    region: str | None = None,
    session: Any = None,
    context_dir: Path | None = None,
) -> BuildInfo:
    handle = submit(
        template,
        name,
        bucket=bucket,
        memory_mb=memory_mb,
        cpu_count=cpu_count,
        force=force,
        base_image_version=base_image_version,
        build_role_arn=build_role_arn,
        region=region,
        session=session,
        context_dir=context_dir,
    )
    clients = _Clients(region=region, session=session)
    return await_build(clients, handle, timeout, on_build_logs)


def build_in_background(
    template: Any,
    name: str,
    *,
    bucket: str,
    memory_mb: int = 2048,
    cpu_count: int | None = None,
    force: bool = False,
    base_image_version: str | None = None,
    build_role_arn: str | None = None,
    region: str | None = None,
    session: Any = None,
    context_dir: Path | None = None,
) -> BuildHandle:
    return submit(
        template,
        name,
        bucket=bucket,
        memory_mb=memory_mb,
        cpu_count=cpu_count,
        force=force,
        base_image_version=base_image_version,
        build_role_arn=build_role_arn,
        region=region,
        session=session,
        context_dir=context_dir,
    )


def template_exists(name: str, *, region: str | None = None, session: Any = None) -> bool:
    clients = _Clients(region=region, session=session)
    return _image_exists(clients, image_arn(clients, name))


def _latest_version(clients: BuildClients, arn: str) -> str:
    """La versión más reciente de `arn`, sin filtrar por estado (a
    diferencia de `resolve_base_version`): `rayito template status`/`logs`
    sin `--version` quiere ver el build más reciente aunque haya fallado."""
    paginator = clients.microvms.get_paginator("list_microvm_image_versions")
    items = [item for page in paginator.paginate(imageIdentifier=arn) for item in page["items"]]
    if not items:
        raise NotFoundException(f"la imagen {arn!r} no tiene ninguna versión")
    return str(max(items, key=lambda item: item["createdAt"])["imageVersion"])


def status_by_name(
    name: str, version: str | None = None, *, region: str | None = None, session: Any = None
) -> BuildStatus:
    """`rayito template status`: resuelve `version` (la más reciente si no
    se da una) y delega en `get_build_status`."""
    clients = _Clients(region=region, session=session)
    arn = image_arn(clients, name)
    resolved_version = version if version is not None else _latest_version(clients, arn)
    return get_build_status(
        BuildHandle(arn=arn, version=resolved_version, name=name, region=region, session=session)
    )


def read_recent_logs(
    name: str, *, region: str | None = None, session: Any = None, limit: int = 500
) -> list[str]:
    """`rayito template logs`: las líneas más recientes del grupo de logs
    de la imagen, sin importar si el último build terminó bien o mal."""
    clients = _Clients(region=region, session=session)
    return _read_build_logs(clients, f"{LOG_GROUP_PREFIX}/{name}", limit)

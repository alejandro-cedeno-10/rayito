"""Adaptador de AWS de `Template.build()` (m15-templates, investigación §3.4
paso 2: el núcleo de `create`/`update-microvm-image`, el gate de tres
estados y el reuso por configuración viven en `rayito._images`, compartido
con `rayito image publish`; aquí sólo queda lo propio de un template).

Pipeline: resuelve la versión de la imagen base -> descarga su
`codeArtifact` (`s3:GetObject`) -> compone el Dockerfile y el zip
(`_dockerfile.py`/`_artifact.py`) -> sube el artefacto por hash de
contenido (se salta la subida si ya existe) -> `create`/`update-microvm-image`
-> sondea el gate de tres estados -> en caso de fallo, relee el grupo de
logs (`logs:GetLogEvents`) y traduce a `BuildException`.

Los mensajes de error nunca repiten un ARN (lleva el ID de cuenta) ni el
`stateReason` crudo de AWS: sólo el nombre que pasó el llamante, el estado
y un `reason` estable (investigación §6).

Cada nombre de parámetro AWS usado aquí aparece en `AWS_API_NOTES.md` §27.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final, Protocol

import boto3
from botocore.exceptions import ClientError

from rayito._aws import LazyClient
from rayito._aws_sanitize import sanitize_aws_error
from rayito._images import (
    ACTIVE_VERSION_STATUS,
    BUILD_QUOTA_ERROR_CODE,
    DEFAULT_BUILD_TIMEOUT_SECONDS,
    DEFAULT_MEMORY_MIB,
    LOG_GROUP_PREFIX,
    SUCCESSFUL_VERSION_STATE,
    VersionGate,
    account_image_arn,
    client_error_code,
    find_reusable_version,
    image_exists,
    inherited_configuration,
    read_gate,
    submit_image_build,
    upload_if_absent,
    wait_for_gate,
)
from rayito._limits import SUPPORTED_MEMORY_MIB
from rayito._templates._artifact import assemble_artifact
from rayito._templates._concurrency import build_slot
from rayito._templates._context import collect_context_files
from rayito._templates._instructions import BASE_IMAGE_KIND, BaseImageRef, CopyStep, TemplateSpec
from rayito._templates._logs import classify_ready_failure, parse_build_failure
from rayito._templates._models import BuildHandle, BuildInfo, BuildStatus
from rayito.exceptions import (
    BuildException,
    InvalidArgumentException,
    NotFoundException,
    TemplateException,
)

#: Clave S3 del artefacto: `rayito/templates/<sha256-del-zip>.zip`,
#: direccionada por contenido como `cli/_publish.py` `artifact_key` (reusar
#: una versión exige el mismo artefacto y la misma configuración).
S3_KEY_PREFIX: Final = "rayito/templates"
#: Descripción de cada versión que crea `Template.build()`.
BUILD_DESCRIPTION: Final = "rayito template build"
#: `name` de `CreateMicrovmImageRequest`: 1-64 de `[a-zA-Z0-9-_]`
#: (`AWS_API_NOTES.md` §4). Un `"nombre:tag"` de E2B no cabe ahí.
TEMPLATE_NAME_PATTERN: Final = re.compile(r"[A-Za-z0-9_-]{1,64}")
#: Líneas de log que se releen para explicar un fallo (`GetLogEvents`
#: `limit`, `AWS_API_NOTES.md` §27).
BUILD_LOG_LINES: Final = 500
FAILED_VERSION_STATE: Final = "FAILED"


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


def validate_template_name(name: str) -> None:
    """`TemplateException` si `name` no es un nombre de imagen válido; un
    ARN completo se acepta tal cual (`Template.exists(arn)`)."""
    if name.startswith("arn:"):
        return
    if TEMPLATE_NAME_PATTERN.fullmatch(name) is None:
        raise TemplateException(
            f"nombre de template inválido {name!r}: 1-64 caracteres de [A-Za-z0-9_-] "
            "(sin ':tag'; Lambda MicroVMs versiona cada build por su cuenta)"
        )


def image_arn(clients: BuildClients, name: str) -> str:
    return account_image_arn(clients.region, clients.account_id, name)


def _newest_version(items: list[dict[str, Any]]) -> str | None:
    if not items:
        return None
    return str(max(items, key=lambda item: item["createdAt"])["imageVersion"])


def _list_versions(clients: BuildClients, arn: str) -> list[dict[str, Any]]:
    paginator = clients.microvms.get_paginator("list_microvm_image_versions")
    return [item for page in paginator.paginate(imageIdentifier=arn) for item in page["items"]]


def resolve_base_version(
    clients: BuildClients, base_name: str, version: str | None
) -> dict[str, Any]:
    """La versión de la imagen base a componer: la pedida, o la más
    reciente `SUCCESSFUL`/`ACTIVE` (como `run-microvm` sin `imageVersion`
    explícita)."""
    base_arn = image_arn(clients, base_name)
    if version is None:
        version = _newest_version(
            [
                item
                for item in _list_versions(clients, base_arn)
                if item["state"] == SUCCESSFUL_VERSION_STATE
                and item["status"] == ACTIVE_VERSION_STATUS
            ]
        )
    if version is None:
        raise NotFoundException(
            f"la imagen base {base_name!r} no tiene ninguna versión activa que componer"
        )
    return dict(
        clients.microvms.get_microvm_image_version(imageIdentifier=base_arn, imageVersion=version)
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


def upload_artifact(clients: BuildClients, bucket: str, payload: bytes) -> str:
    key = f"{S3_KEY_PREFIX}/{hashlib.sha256(payload).hexdigest()}.zip"
    upload_if_absent(clients, bucket, key, payload)
    return f"s3://{bucket}/{key}"


def _validate_memory(memory_mb: int) -> None:
    if memory_mb not in SUPPORTED_MEMORY_MIB:
        raise InvalidArgumentException(
            f"memory_mb debe ser uno de {SUPPORTED_MEMORY_MIB} (RES-1), se pidió {memory_mb}"
        )


def log_group_of(name: str) -> str:
    return f"{LOG_GROUP_PREFIX}/{name}"


def desired_configuration(
    *,
    artifact_uri: str,
    memory_mb: int,
    base_image_version: dict[str, Any],
    log_group: str,
) -> dict[str, Any]:
    """El cuerpo de `create`/`update-microvm-image` para la nueva imagen del
    template: toda la configuración que la versión de `rayito-base`
    declaró al publicarse (`inherited_configuration`: `baseImageArn` de la
    imagen gestionada, `buildRoleArn`, `hooks`, `additionalOsCapabilities`
    de la variante `caps`, ...) más el artefacto, la memoria y el grupo de
    logs propios del template. El `imageArn`/`imageVersion` de
    `rayito-base` misma no se copian: sólo sirven para traer su
    `codeArtifact`."""
    return {
        **inherited_configuration(base_image_version),
        "codeArtifact": {"uri": artifact_uri},
        "resources": [{"minimumMemoryInMiB": memory_mb}],
        "logging": {"cloudWatch": {"logGroup": log_group}},
    }


def submit_build(
    clients: BuildClients, name: str, arn: str, desired: dict[str, Any]
) -> tuple[str, str]:
    """`create`/`update-microvm-image`; la cuota de 10 builds simultáneos
    por cuenta (Q83) llega como `BuildException(reason="build_quota")`, la
    misma que da el guardia local de `_concurrency.py`, y cualquier otro
    rechazo de AWS como `BuildException(reason="aws_error")` con el
    resumen saneado (`sanitize_aws_error`) como mensaje y `__cause__`."""
    try:
        submitted = submit_image_build(
            clients, name, arn, {**desired, "description": BUILD_DESCRIPTION}
        )
    except ClientError as exc:
        if client_error_code(exc) == BUILD_QUOTA_ERROR_CODE:
            raise BuildException(
                f"AWS rechazó el build del template {name!r}: cuota de builds simultáneos "
                "de la cuenta agotada (Q83); reintenta cuando termine alguno",
                reason="build_quota",
            ) from None
        summary = sanitize_aws_error(exc)
        raise BuildException(
            f"AWS rechazó el build del template {name!r}: {summary}", reason="aws_error"
        ) from summary
    return submitted.arn, submitted.version


def _read_build_logs(
    clients: BuildClients, log_group: str, limit: int = BUILD_LOG_LINES
) -> list[str]:
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


def _raise_for_failure(clients: BuildClients, name: str, gate: VersionGate) -> None:
    """`stateReason` sólo se usa para clasificar (`classify_ready_failure`),
    nunca se copia al mensaje: es texto libre de AWS."""
    ready_reason = classify_ready_failure(gate.state_reason)
    if ready_reason is not None:
        raise BuildException(
            f"el ready cmd del template {name!r} respondió con un error HTTP durante el build",
            reason=ready_reason,
        )
    detail = parse_build_failure(_read_build_logs(clients, log_group_of(name)))
    raise BuildException(
        f"el build del template {name!r} terminó en imagen={gate.image_state} "
        f"versión={gate.version_state}",
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
    base_version = resolve_base_version(clients, spec.base.name, spec.base.version)
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


def _submit_unslotted(
    template: Any,
    name: str,
    *,
    bucket: str,
    memory_mb: int,
    force: bool,
    base_image_version: str | None,
    region: str | None,
    session: Any,
    context_dir: Path | None,
) -> BuildHandle:
    validate_template_name(name)
    _validate_memory(memory_mb)
    spec = _spec_with_base_version(template.spec, base_image_version)
    clients = _Clients(region=region, session=session)
    artifact, base_version = _compose(clients, spec, context_dir or Path())
    artifact_uri = upload_artifact(clients, bucket, artifact)
    arn = image_arn(clients, name)
    desired = desired_configuration(
        artifact_uri=artifact_uri,
        memory_mb=memory_mb,
        base_image_version=base_version,
        log_group=log_group_of(name),
    )
    # `skip_cache()` del DSL es `force=True`: E2B lo documenta como
    # "reconstruir aunque nada haya cambiado".
    if not (force or spec.skip_cache):
        reusable = find_reusable_version(clients, arn, desired)
        if reusable is not None:
            return BuildHandle(arn=arn, version=reusable, name=name, region=region, session=session)
    submitted_arn, version = submit_build(clients, name, arn, desired)
    return BuildHandle(
        arn=submitted_arn, version=version, name=name, region=region, session=session
    )


def get_build_status(handle: BuildHandle) -> BuildStatus:
    clients = _Clients(region=handle.region, session=handle.session)
    gate = read_gate(clients, handle.arn, handle.version)
    if not gate.settled:
        return BuildStatus(state="IN_PROGRESS")
    if not gate.launchable:
        return BuildStatus(
            state="FAILED",
            error_message=(
                f"imagen={gate.image_state} versión={gate.version_state} "
                f"estado={gate.version_status}"
            ),
        )
    return BuildStatus(state="SUCCESSFUL", info=_build_info(handle))


def _build_info(handle: BuildHandle) -> BuildInfo:
    return BuildInfo(
        template_id=handle.arn, build_id=f"{handle.version}/{handle.name}", alias=handle.name
    )


def await_build(
    clients: BuildClients,
    handle: BuildHandle,
    timeout: float,
    on_build_logs: Callable[[str], None] | None,
) -> BuildInfo:
    gate, timed_out = wait_for_gate(clients, handle.arn, handle.version, timeout, time.sleep)
    if on_build_logs is not None:
        for line in _read_build_logs(clients, log_group_of(handle.name)):
            on_build_logs(line)
    if timed_out:
        raise BuildException(
            f"el build del template {handle.name!r} no terminó en {timeout:.0f} s; sigue en AWS "
            "y Template.get_build_status() puede consultarlo",
            reason="build_timeout",
        )
    if not gate.launchable:
        _raise_for_failure(clients, handle.name, gate)
    return _build_info(handle)


def build(
    template: Any,
    name: str,
    *,
    bucket: str,
    memory_mb: int = DEFAULT_MEMORY_MIB,
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
    """Envía el build y espera el gate. El hueco de `build_slot()` se
    mantiene durante toda la espera: limita builds en vuelo en AWS desde
    este proceso, no sólo envíos. `cpu_count` y `build_role_arn` se
    aceptan pero se ignoran (la CPU sale de `memory_mb`, Q87; el rol de
    build es el de la imagen base): alcance de 0.6 según el ADR-022."""
    del cpu_count, build_role_arn
    with build_slot():
        handle = _submit_unslotted(
            template,
            name,
            bucket=bucket,
            memory_mb=memory_mb,
            force=force,
            base_image_version=base_image_version,
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
    memory_mb: int = DEFAULT_MEMORY_MIB,
    cpu_count: int | None = None,
    force: bool = False,
    base_image_version: str | None = None,
    build_role_arn: str | None = None,
    region: str | None = None,
    session: Any = None,
    context_dir: Path | None = None,
) -> BuildHandle:
    """Como `build()` pero vuelve tras el envío. El hueco local sólo cubre
    la composición y el envío: un build en segundo plano sigue en AWS sin
    contar contra `MAX_CONCURRENT_BUILDS` (la cuota real de AWS sí lo
    cuenta y llega como `BuildException(reason="build_quota")`)."""
    del cpu_count, build_role_arn
    with build_slot():
        return _submit_unslotted(
            template,
            name,
            bucket=bucket,
            memory_mb=memory_mb,
            force=force,
            base_image_version=base_image_version,
            region=region,
            session=session,
            context_dir=context_dir,
        )


def template_exists(name: str, *, region: str | None = None, session: Any = None) -> bool:
    validate_template_name(name)
    clients = _Clients(region=region, session=session)
    return image_exists(clients, image_arn(clients, name))


def _latest_version(clients: BuildClients, name: str) -> str:
    """La versión más reciente de `name`, sin filtrar por estado (a
    diferencia de `resolve_base_version`): `rayito template status`/`logs`
    sin `--version` quiere ver el build más reciente aunque haya fallado."""
    version = _newest_version(_list_versions(clients, image_arn(clients, name)))
    if version is None:
        raise NotFoundException(f"el template {name!r} no tiene ninguna versión")
    return version


def status_by_name(
    name: str, version: str | None = None, *, region: str | None = None, session: Any = None
) -> BuildStatus:
    """`rayito template status`: resuelve `version` (la más reciente si no
    se da una) y delega en `get_build_status`."""
    validate_template_name(name)
    clients = _Clients(region=region, session=session)
    resolved_version = version if version is not None else _latest_version(clients, name)
    return get_build_status(
        BuildHandle(
            arn=image_arn(clients, name),
            version=resolved_version,
            name=name,
            region=region,
            session=session,
        )
    )


def read_recent_logs(
    name: str, *, region: str | None = None, session: Any = None, limit: int = BUILD_LOG_LINES
) -> list[str]:
    """`rayito template logs`: las líneas más recientes del grupo de logs
    de la imagen, sin importar si el último build terminó bien o mal."""
    validate_template_name(name)
    clients = _Clients(region=region, session=session)
    return _read_build_logs(clients, log_group_of(name), limit)

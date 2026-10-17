"""`deploy_agent()` y `deploy_agent_async()`: un proyecto de agente que ya
tienes, desplegado con una llamada y sin tocar su código (ADR-029, D7 y D8).

Detectan el proyecto (`AgentProject.detect()`, sin ejecutar su código), resuelven
lo que falta de AWS (región, imagen base, secreto del modelo, bucket y access
token: `_aws_resolve.py`), construyen la imagen sólo si cambió (el hash del
contenido, como `AgentProject.build()`) y arrancan la flota
(`AgentFleet.start()`). `plan_deploy()` calcula lo mismo sin llamar a AWS, para
`rayito agent deploy --dry-run`.

Cada paso que cuesta es visible: `AgentDeployOptions.on_step` recibe un `AgentDeployStep`
por cada uno, y ninguno crea nada en silencio (ver `_aws_resolve.py`).
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

from rayito._agent._aws_resolve import (
    AwsResolver,
    DeployAws,
    SecretStorePort,
    discover_bucket,
    ensure_base_image,
    ensure_model_secrets,
    resolve_bucket,
    resolve_token,
)
from rayito._agent._deploy_plan import (
    AgentDeployOptions,
    AgentDeployPlan,
    AgentDeployStep,
    base_version_of,
    build_plan,
    project_bucket,
    project_for,
    resolved_region,
    with_default_excludes,
)
from rayito._agent._fleet import AgentFleet
from rayito._agent._fleet_async import AsyncAgentFleet
from rayito._agent._fleet_base import AGENT_FLEET_WARM_POLL_SECONDS
from rayito._agent._project_base import template_name
from rayito._agent._project_domain import AgentProject
from rayito._agent._project_service import ProjectService
from rayito._agent._project_service_async import AsyncProjectService
from rayito._secrets import SecretStore
from rayito.exceptions import InvalidArgumentException

if TYPE_CHECKING:
    from rayito._agent._detect_report import DetectOverrides

logger = logging.getLogger("rayito.agent")

STEP_BASE_IMAGE: Final = "base_image"
STEP_SECRET_CREATED: Final = "secret_created"
STEP_BUCKET: Final = "bucket"
STEP_IMAGE: Final = "image"
STEP_FLEET: Final = "fleet"
STEP_WARM: Final = "warm"


@dataclass
class DeployPorts:
    """Las dependencias de `deploy_agent()` que los tests sustituyen: AWS
    (`aws`, `store`), el entorno y lo que recibe la flota (`fleet_kwargs`:
    `service`, `directory`, `pool_factory`, `clock`, `sleep`...). En
    producción es el valor por defecto y no se pasa."""

    aws: DeployAws | None = None
    store: SecretStorePort | None = None
    environ: Mapping[str, str] | None = None
    fleet_kwargs: Mapping[str, Any] = field(default_factory=dict)
    service_kwargs: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Preflight:
    """Lo que se decidió antes de crear nada: el proyecto con los secretos
    resueltos, el bucket (o `None` si no se construye), el token y la región."""

    project: AgentProject
    bucket: str | None
    token: str
    region: str | None
    steps: tuple[AgentDeployStep, ...]


def plan_deploy(
    path: str | os.PathLike[str] = ".",
    *,
    project: AgentProject | None = None,
    options: AgentDeployOptions | None = None,
    overrides: DetectOverrides | None = None,
    region: str | None = None,
    session: Any = None,
) -> AgentDeployPlan:
    """Lo que `deploy_agent()` haría con estos argumentos, sin llamar a AWS:
    detecta el proyecto (lee ficheros locales, no ejecuta su código), y calcula
    el escalado, el origen de cada valor y el coste de lista.

    Coste y activación
    -------------------
    Activa: llamar a `plan_deploy()` (o `rayito agent deploy --dry-run`).
    Recursos y llamadas AWS: ninguno; no crea clientes de AWS ni carga
        ningún paquete opcional.
    Coste aproximado: ninguno.
    IAM: ninguna.
    Cómo apagarla: no la llames.
    Ejemplo:
        plan = plan_deploy("./mi-agente")
        plan.cost.running_usd_per_hour
    """
    chosen = options or AgentDeployOptions()
    detected, report = project_for(path, project, overrides, region)
    return build_plan(
        detected,
        chosen,
        region=resolved_region(region, detected, session, os.environ),
        detection=report,
    )


def deploy_agent(
    path: str | os.PathLike[str] = ".",
    *,
    project: AgentProject | None = None,
    options: AgentDeployOptions | None = None,
    overrides: DetectOverrides | None = None,
    region: str | None = None,
    session: Any = None,
) -> AgentFleet:
    """Despliega el agente de `path` y devuelve su `AgentFleet`.

    `path` es un directorio con el proyecto (con o sin `rayito-agent.toml`):
    el lenguaje, el framework, el punto de entrada y la instalación se
    detectan de sus ficheros sin ejecutar su código, y todo lo detectado se
    puede fijar con `overrides` o con el fichero. Un `project` explícito se
    usa tal cual. `options` (`AgentDeployOptions`) dice dónde y cómo; `region` y
    `session` son los de boto3.

    El orden de cada valor es argumento explícito, fichero de proyecto,
    variable de entorno y descubrimiento; ninguno crea nada en silencio:
    Rayito no crea buckets, un secreto sólo se crea con
    `create_secret_from_env=True` y una imagen base ausente sólo se publica si
    pasas `base_publisher`. La imagen del agente se construye sólo si cambió su
    contenido. El access token de la flota es `options.access_token`, o
    `RAYITO_ACCESS_TOKEN`, o uno nuevo en `AgentFleet.access_token`.

    Coste y activación
    -------------------
    Activa: llamar a `deploy_agent()` (o `rayito agent deploy`); nada más lo
        crea. `plan_deploy()` (`--dry-run`) calcula el coste sin llamar a AWS.
    Recursos y llamadas AWS: `min_vms` MicroVMs (`run-microvm`) y
        `warm_vms` plazas aparcadas; una versión de imagen nueva si cambió el
        contenido (`create`/`update-microvm-image`, un objeto en S3);
        `GetCallerIdentity`, `HeadBucket`, `GetMicrovmImage` y
        `DescribeSecret` de lectura; con `create_secret_from_env=True`,
        `CreateSecret`; con `base_publisher`, una imagen base; con
        `session_index`, DynamoDB.
    Coste aproximado: por VM, el del MicroVM mientras vive (2 GB: ≈ $0,13 por
        hora en us-east-1, consultado 2026-10-06 en
        https://aws.amazon.com/lambda/pricing/); la imagen nueva, ≈ 3,0 GB de
        snapshot (≈ $0,011 de escritura y ≈ $0,056 por semana mínima de
        almacenamiento); un secreto, $0,40 al mes
        (https://aws.amazon.com/secrets-manager/pricing/, consultado
        2026-09-30); el modelo lo cobra su proveedor.
    IAM: los de `Sandbox.create()`, `Sandbox.list()` y `AgentTemplate.build()`
        (`RayitoSandboxRunner`, `RayitoTemplateBuilder`),
        `secretsmanager:DescribeSecret` y, con `create_secret_from_env`,
        `secretsmanager:CreateSecret`, `sts:GetCallerIdentity` y
        `s3:ListBucket` sobre el bucket del artefacto.
    Cómo apagarla: `AgentFleet.stop()` termina los VMs; los valores por
        defecto de `AgentDeployOptions` no crean ningún secreto, imagen base ni
        índice; sin llamar a `deploy_agent()` nada se crea.
    Ejemplo:
        options = AgentDeployOptions(bucket="amzn-s3-demo-bucket")
        fleet = deploy_agent("./mi-agente", options=options)
        fleet.invoke("Resume el README.").text
        fleet.stop()
    """
    return deploy_with(
        DeployPorts(),
        path,
        project=project,
        options=options,
        overrides=overrides,
        region=region,
        session=session,
    )


async def deploy_agent_async(
    path: str | os.PathLike[str] = ".",
    *,
    project: AgentProject | None = None,
    options: AgentDeployOptions | None = None,
    overrides: DetectOverrides | None = None,
    region: str | None = None,
    session: Any = None,
) -> AsyncAgentFleet:
    """Como `deploy_agent()`, con `AsyncAgentFleet`: mismas opciones, mismo
    orden de resolución y mismo coste. Las lecturas de AWS previas a crear nada
    corren en un hilo para no bloquear el bucle de eventos.

    Coste y activación
    -------------------
    Activa: `await deploy_agent_async()`; nada más lo crea.
    Recursos y llamadas AWS: los de `deploy_agent()`.
    Coste aproximado: el de `deploy_agent()`, por VM (2 GB: ≈ $0,13 por hora en
        us-east-1, consultado 2026-10-06 en https://aws.amazon.com/lambda/pricing/).
    IAM: los de `deploy_agent()`.
    Cómo apagarla: `await fleet.stop()`; sin llamar a esta función nada se
        crea.
    Ejemplo:
        fleet = await deploy_agent_async("./mi-agente")
        (await fleet.invoke("Resume el README.")).text
        await fleet.stop()
    """
    return await deploy_with_async(
        DeployPorts(),
        path,
        project=project,
        options=options,
        overrides=overrides,
        region=region,
        session=session,
    )


def deploy_with(
    ports: DeployPorts,
    path: str | os.PathLike[str] = ".",
    *,
    project: AgentProject | None = None,
    options: AgentDeployOptions | None = None,
    overrides: DetectOverrides | None = None,
    region: str | None = None,
    session: Any = None,
) -> AgentFleet:
    """`deploy_agent()` con las dependencias de `ports`."""
    chosen = options or AgentDeployOptions()
    detected, _ = project_for(path, project, overrides, region)
    flight = preflight(detected, chosen, ports, region=region, session=session)
    _emit(chosen, flight.steps)
    service = ProjectService(flight.project, **dict(ports.service_kwargs))
    if flight.bucket is not None:
        info = service.build(
            bucket=flight.bucket,
            timeout=chosen.build_timeout_seconds,
            on_build_logs=chosen.on_build_logs,
            region=flight.region,
            session=session,
            base_version=base_version_of(chosen, flight.project),
        )
        _emit(chosen, (AgentDeployStep(STEP_IMAGE, _image_detail(info)),))
    fleet = AgentFleet(
        flight.project,
        access_token=flight.token,
        scaling=chosen.scaling,
        session_index=chosen.session_index,
        pool_backend=chosen.pool_backend,
        keep_on_failure=chosen.keep_on_failure,
        region=flight.region,
        session=session,
        **{"service": service, **dict(ports.fleet_kwargs)},
    )
    fleet.start()
    _emit(chosen, (AgentDeployStep(STEP_FLEET, str(len(fleet.sandbox_ids))),))
    _wait_warm(fleet, chosen)
    return fleet


async def deploy_with_async(
    ports: DeployPorts,
    path: str | os.PathLike[str] = ".",
    *,
    project: AgentProject | None = None,
    options: AgentDeployOptions | None = None,
    overrides: DetectOverrides | None = None,
    region: str | None = None,
    session: Any = None,
) -> AsyncAgentFleet:
    """`deploy_agent_async()` con las dependencias de `ports`."""
    chosen = options or AgentDeployOptions()
    detected, _ = await asyncio.to_thread(project_for, path, project, overrides)
    flight = await asyncio.to_thread(
        preflight, detected, chosen, ports, region=region, session=session
    )
    _emit(chosen, flight.steps)
    service = AsyncProjectService(flight.project, **dict(ports.service_kwargs))
    if flight.bucket is not None:
        info = await service.build(
            bucket=flight.bucket,
            timeout=chosen.build_timeout_seconds,
            on_build_logs=chosen.on_build_logs,
            region=flight.region,
            session=session,
            base_version=base_version_of(chosen, flight.project),
        )
        _emit(chosen, (AgentDeployStep(STEP_IMAGE, _image_detail(info)),))
    fleet = AsyncAgentFleet(
        flight.project,
        access_token=flight.token,
        scaling=chosen.scaling,
        session_index=chosen.session_index,
        pool_backend=chosen.pool_backend,
        keep_on_failure=chosen.keep_on_failure,
        region=flight.region,
        session=session,
        **{"service": service, **dict(ports.fleet_kwargs)},
    )
    await fleet.start()
    _emit(chosen, (AgentDeployStep(STEP_FLEET, str(len(fleet.sandbox_ids))),))
    await _wait_warm_async(fleet, chosen)
    return fleet


def preflight(
    project: AgentProject,
    options: AgentDeployOptions,
    ports: DeployPorts,
    *,
    region: str | None,
    session: Any,
) -> Preflight:
    """Lo que se resuelve antes de crear nada: la imagen base, el secreto del
    modelo, el bucket y el token. Todo lo que lee AWS ocurre aquí, y lo único
    que puede crear es un secreto (con `create_secret_from_env`) o la imagen
    base (con `base_publisher`)."""
    environ = os.environ if ports.environ is None else ports.environ
    effective_region = resolved_region(region, project, session, environ)
    aws = ports.aws or AwsResolver(region=effective_region, session=session)
    steps: list[AgentDeployStep] = []
    building = options.build_image is not False
    if building and ensure_base_image(
        aws, project.base, options.base_publisher, ensure=options.ensure_base_image
    ):
        steps.append(AgentDeployStep(STEP_BASE_IMAGE, "published"))
    store = ports.store or SecretStore(region=effective_region, session=session)
    secrets = ensure_model_secrets(
        project,
        secret_names=options.secret_names,
        create_from_env=options.create_secret_from_env,
        store=store,
        environ=environ,
    )
    steps.extend(AgentDeployStep(STEP_SECRET_CREATED, gateway) for gateway in secrets.created)
    resolved = with_default_excludes(dataclasses.replace(project, gateways=secrets.gateways))
    bucket = _bucket_for(resolved, options, aws, environ, steps)
    token = resolve_token(options.access_token, environ)
    return Preflight(resolved, bucket, token, effective_region, tuple(steps))


def _bucket_for(
    project: AgentProject,
    options: AgentDeployOptions,
    aws: DeployAws,
    environ: Mapping[str, str],
    steps: list[AgentDeployStep],
) -> str | None:
    if options.build_image is False:
        return None
    try:
        choice = resolve_bucket(
            options.bucket,
            project_bucket(project),
            environ,
            lambda: discover_bucket(aws, aws.resolved_region),
        )
    except InvalidArgumentException:
        if options.build_image is None and aws.image_exists(template_name(project)):
            logger.warning(
                "sin bucket para el artefacto: se reutiliza la imagen existente sin "
                "comprobar si el proyecto cambió"
            )
            return None
        raise
    steps.append(AgentDeployStep(STEP_BUCKET, f"{choice.source}:{choice.bucket}"))
    return choice.bucket


def _image_detail(info: Any) -> str:
    """La versión de la imagen de `BuildInfo.build_id` (`<versión>/<build>`)."""
    return str(info.build_id).split("/", maxsplit=1)[0]


def _emit(options: AgentDeployOptions, steps: tuple[AgentDeployStep, ...]) -> None:
    if options.on_step is not None:
        for step in steps:
            options.on_step(step)


def _wait_warm(fleet: AgentFleet, options: AgentDeployOptions) -> None:
    if fleet.scaling.warm_vms == 0:
        return
    ready = fleet.wait_warm(options.warm_timeout_seconds, AGENT_FLEET_WARM_POLL_SECONDS)
    _emit(options, (AgentDeployStep(STEP_WARM, "ready" if ready else "timeout"),))


async def _wait_warm_async(fleet: AsyncAgentFleet, options: AgentDeployOptions) -> None:
    if fleet.scaling.warm_vms == 0:
        return
    ready = await fleet.wait_warm(options.warm_timeout_seconds, AGENT_FLEET_WARM_POLL_SECONDS)
    _emit(options, (AgentDeployStep(STEP_WARM, "ready" if ready else "timeout"),))

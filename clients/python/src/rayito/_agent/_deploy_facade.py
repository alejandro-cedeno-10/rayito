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
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, Literal, overload

from rayito._agent._aws_resolve import (
    GATEWAY_SECRET_PREFIX,
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
    deploy_scaling,
    project_bucket,
    project_for,
    resolved_region,
    with_default_excludes,
)
from rayito._agent._fleet import AgentFleet
from rayito._agent._fleet_async import AsyncAgentFleet
from rayito._agent._fleet_base import AGENT_FLEET_WARM_POLL_SECONDS
from rayito._agent._fleet_recipe_aws import RECIPE_SAVED
from rayito._agent._gateway_keys import renewable_gateways
from rayito._agent._project_base import image_version_of, template_name
from rayito._agent._project_domain import AgentProject
from rayito._agent._project_service import ProjectService
from rayito._agent._project_service_async import AsyncProjectService
from rayito._agent._secret_env_deploy import (
    STEP_SECRET_ENV_CREATED,
    EnvFileSecret,
    EnvSecretStore,
    env_missing_steps,
    env_secret_store,
    prepare_secret_env,
    publish_env_file_secret,
)
from rayito._agent._secret_env_domain import uses_secret_env
from rayito._agent._secret_env_reader import async_secret_reader_for, secret_reader_for
from rayito._agentcore._targets import (
    TARGET_AGENTCORE,
    check_agentcore_options_target,
    require_target,
)
from rayito._secrets import SecretStore
from rayito.exceptions import AgentCoreException, InvalidArgumentException

if TYPE_CHECKING:
    from rayito._agent._detect_report import DetectOverrides
    from rayito._agentcore._fleet import AgentCoreFleet
    from rayito._agentcore._fleet_async import AsyncAgentCoreFleet
    from rayito._agentcore._models import AgentCoreDeployPlan, AgentCoreOptions

logger = logging.getLogger("rayito.agent")

STEP_BASE_IMAGE: Final = "base_image"
STEP_SECRET_CREATED: Final = "secret_created"
STEP_BUCKET: Final = "bucket"
STEP_IMAGE: Final = "image"
STEP_FLEET: Final = "fleet"
STEP_WARM: Final = "warm"
#: El paso de la receta de la flota que no se guardó: su detalle es el código
#: del aviso (`recipe_access_denied`, `recipe_not_saved`, `recipe_too_large`).
STEP_RECIPE: Final = "recipe"
#: Las opciones de la receta y de la renovación de claves que sólo tienen
#: sentido en MicroVMs: con `target="agentcore"`, `option_not_supported`
#: (nombre, bandera de la CLI, motivo y si la llamada la trae).
MICROVM_ONLY_OPTIONS: Final[
    tuple[tuple[str, str, str, Callable[[AgentDeployOptions], bool]], ...]
] = (
    (
        "scale_in_after_seconds",
        "--scale-in-after",
        "usa idle_session_timeout_seconds / --idle-session-timeout",
        lambda options: options.scale_in_after_seconds is not None,
    ),
    (
        "save_recipe",
        "--no-save-recipe",
        "AgentCore guarda su configuración en el runtime: no hay receta",
        lambda options: not options.save_recipe,
    ),
    (
        "renew_gateway_keys",
        "--renew-gateway-keys",
        "no hay pasarela de secretos",
        lambda options: options.renew_gateway_keys,
    ),
)


def check_microvm_only_options(options: AgentDeployOptions | None, target: str) -> None:
    """Con `target="agentcore"`, `AgentCoreException` `option_not_supported`
    con la primera opción de la receta o de la renovación de claves que la
    llamada trae, antes de cualquier llamada a AWS."""
    if target != TARGET_AGENTCORE or options is None:
        return
    for name, _flag, reason, passed in MICROVM_ONLY_OPTIONS:
        if passed(options):
            raise AgentCoreException("option_not_supported", option=name, reason=reason)


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
    secret_env_store: EnvSecretStore | None = None


@dataclass(frozen=True)
class Preflight:
    """Lo que se decidió antes de crear nada: el proyecto con los secretos
    resueltos, el bucket (o `None` si no se construye), el token y la región."""

    project: AgentProject
    bucket: str | None
    token: str
    region: str | None
    steps: tuple[AgentDeployStep, ...]


@overload
def plan_deploy(
    path: str | os.PathLike[str] = ...,
    *,
    project: AgentProject | None = ...,
    options: AgentDeployOptions | None = ...,
    overrides: DetectOverrides | None = ...,
    region: str | None = ...,
    session: Any = ...,
    target: Literal["microvm"] = ...,
    agentcore: None = ...,
) -> AgentDeployPlan: ...


@overload
def plan_deploy(
    path: str | os.PathLike[str] = ...,
    *,
    project: AgentProject | None = ...,
    options: AgentDeployOptions | None = ...,
    overrides: DetectOverrides | None = ...,
    region: str | None = ...,
    session: Any = ...,
    target: Literal["agentcore"],
    agentcore: AgentCoreOptions | None = ...,
) -> AgentCoreDeployPlan: ...


def plan_deploy(
    path: str | os.PathLike[str] = ".",
    *,
    project: AgentProject | None = None,
    options: AgentDeployOptions | None = None,
    overrides: DetectOverrides | None = None,
    region: str | None = None,
    session: Any = None,
    target: Literal["microvm", "agentcore"] = "microvm",
    agentcore: AgentCoreOptions | None = None,
) -> AgentDeployPlan | AgentCoreDeployPlan:
    """Lo que `deploy_agent()` haría con estos argumentos, sin llamar a AWS:
    detecta el proyecto (lee ficheros locales, no ejecuta su código), y calcula
    el escalado, el origen de cada valor y el coste de lista. Con
    `target="agentcore"` devuelve un `AgentCoreDeployPlan`: valida las opciones,
    deriva los nombres de los recursos y dice qué rol tendrá el runtime y qué
    cuesta; `agentcore=` sólo vale con ese destino.

    Coste y activación
    -------------------
    Activa: llamar a `plan_deploy()` (o `rayito agent deploy --dry-run`).
    Recursos y llamadas AWS: ninguno; no crea clientes de AWS ni carga
        ningún paquete opcional.
    Coste aproximado: ninguno.
    IAM: ninguna.
    Cómo apagarla: no la llamo.
    Ejemplo:
        plan = plan_deploy("./mi-agente")
        plan.cost.running_usd_per_hour
    """
    require_target(target)
    check_agentcore_options_target(agentcore, target)
    check_microvm_only_options(options, target)
    if target == TARGET_AGENTCORE:
        from rayito._agentcore._deploy import plan_agentcore

        return plan_agentcore(
            path,
            project=project,
            options=options,
            overrides=overrides,
            region=region,
            session=session,
            agentcore=agentcore,
        ).plan
    chosen = options or AgentDeployOptions()
    detected, report = project_for(path, project, overrides, region)
    detected = prepare_secret_env(
        detected, chosen.create_secrets_from, check_env=chosen.check_env
    ).project
    return build_plan(
        detected,
        chosen,
        region=resolved_region(region, detected, session, os.environ),
        detection=report,
    )


@overload
def deploy_agent(
    path: str | os.PathLike[str] = ...,
    *,
    project: AgentProject | None = ...,
    options: AgentDeployOptions | None = ...,
    overrides: DetectOverrides | None = ...,
    region: str | None = ...,
    session: Any = ...,
    target: Literal["microvm"] = ...,
    agentcore: None = ...,
) -> AgentFleet: ...


@overload
def deploy_agent(
    path: str | os.PathLike[str] = ...,
    *,
    project: AgentProject | None = ...,
    options: AgentDeployOptions | None = ...,
    overrides: DetectOverrides | None = ...,
    region: str | None = ...,
    session: Any = ...,
    target: Literal["agentcore"],
    agentcore: AgentCoreOptions | None = ...,
) -> AgentCoreFleet: ...


def deploy_agent(
    path: str | os.PathLike[str] = ".",
    *,
    project: AgentProject | None = None,
    options: AgentDeployOptions | None = None,
    overrides: DetectOverrides | None = None,
    region: str | None = None,
    session: Any = None,
    target: Literal["microvm", "agentcore"] = "microvm",
    agentcore: AgentCoreOptions | None = None,
) -> AgentFleet | AgentCoreFleet:
    """Despliega el agente de `path` y devuelve su `AgentFleet`; con
    `target="agentcore"`, su `AgentCoreFleet`.

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

    Con `target="agentcore"` (y `agentcore=AgentCoreOptions(...)` para los
    modelos y secretos del rol, los plazos de sesión y las etiquetas) el agente
    se despliega en Amazon Bedrock AgentCore Runtime sin Docker local: una
    pila de CloudFormation por agente (`rayito-agentcore-<nombre>`), la imagen
    arm64 construida en CodeBuild sólo si cambió el contenido y un runtime
    creado o actualizado por nombre, con MMDSv2 exigido. No hay pasarela de
    secretos: el agente lee las credenciales del rol del runtime, que sólo
    puede invocar los modelos y leer los secretos que le des. Las opciones de
    MicroVMs que AgentCore no admite (`scaling`, `secret_names`,
    `access_token`...) son `AgentCoreException` con código
    `option_not_supported`.

    Coste y activación
    -------------------
    Activa: llamar a `deploy_agent()` (o `rayito agent deploy`); nada más lo
        crea. `plan_deploy()` (`--dry-run`) calcula el coste sin llamar a AWS.
        Con `target="agentcore"`, sólo ese argumento crea nada de AgentCore,
        CodeBuild o ECR; sin él no se crea ni se carga ningún cliente de esos
        servicios.
    Recursos y llamadas AWS: `min_vms` MicroVMs (`run-microvm`) y
        `warm_vms` plazas aparcadas; una versión de imagen nueva si cambió el
        contenido (`create`/`update-microvm-image`, un objeto en S3);
        `GetCallerIdentity`, `HeadBucket`, `GetMicrovmImage` y
        `DescribeSecret` de lectura; con `create_secret_from_env=True`,
        `CreateSecret`; con `base_publisher`, una imagen base; con
        `session_index`, DynamoDB; tras arrancar la flota, la receta en
        Parameter Store (`PutParameter` de un `SecureString` estándar, salvo
        con `save_recipe=False`); con `renew_gateway_keys`, `DescribeSecret`
        y `PutSecretValue` sobre el secreto de cada pasarela `bedrock`.
        Con `target="agentcore"`: la pila
        `rayito-agentcore-<nombre>` (repositorio de ECR, proyecto de CodeBuild
        con su grupo de logs y dos roles), un zip del contexto de build en el
        bucket de artefactos mientras dura el build, un build de CodeBuild si
        cambió el contenido, y un runtime de AgentCore (`CreateAgentRuntime`
        y `UpdateAgentRuntime`); de lectura, `GetCallerIdentity`, `HeadBucket`,
        `DescribeStacks`, `DescribeImages`, `ListAgentRuntimes` y
        `GetAgentRuntime`.
    Coste aproximado: por VM, el del MicroVM mientras vive (2 GB: ≈ $0,13 por
        hora en us-east-1, consultado 2026-10-06 en
        https://aws.amazon.com/lambda/pricing/); la imagen nueva, ≈ 3,0 GB de
        snapshot (≈ $0,011 de escritura y ≈ $0,056 por semana mínima de
        almacenamiento); un secreto, $0,40 al mes
        (https://aws.amazon.com/secrets-manager/pricing/, consultado
        2026-09-30); el modelo lo cobra su proveedor. Con
        `target="agentcore"` (us-east-1, consultado 2026-10-19): CodeBuild
        arm1.small $0,0034 por minuto de build (100 minutos gratis al mes,
        https://aws.amazon.com/codebuild/pricing/), imágenes en ECR $0,10 por
        GB-mes, logs $0,50 por GB ingerido
        (https://aws.amazon.com/cloudwatch/pricing/) y el runtime, V1,
        $0,0895 por vCPU-hora y $0,00945 por GB-hora de uso real
        (https://aws.amazon.com/bedrock/agentcore/pricing/); sin sesiones no
        hay cómputo.
    IAM: los de `Sandbox.create()`, `Sandbox.list()` y `AgentTemplate.build()`
        (`RayitoSandboxRunner`, `RayitoTemplateBuilder`),
        `secretsmanager:DescribeSecret` y, con `create_secret_from_env`,
        `secretsmanager:CreateSecret`, `sts:GetCallerIdentity` y
        `s3:ListBucket` sobre el bucket del artefacto; `ssm:PutParameter`
        sobre `parameter/rayito/agents/*` para la receta (sin él, un aviso
        `recipe_access_denied` y el despliegue sigue); con
        `renew_gateway_keys`, `secretsmanager:PutSecretValue` y
        `bedrock:CallWithBearerToken`. Con
        `target="agentcore"`: CloudFormation sobre `rayito-agentcore-*` con
        `CAPABILITY_IAM`, ECR, CodeBuild, S3 sobre
        `rayito/images/agentcore/*`, AgentCore (`CreateAgentRuntime`,
        `UpdateAgentRuntime`, `GetAgentRuntime`, `ListAgentRuntimes`) e
        `iam:PassRole` del rol del runtime.
    Cómo apagarla: `AgentFleet.stop()` termina los VMs (y borra la
        receta); los valores por defecto de `AgentDeployOptions` no crean
        ningún secreto, imagen base ni índice; `save_recipe=False` no guarda
        la receta; sin llamar a `deploy_agent()` nada se crea. Un agente
        de AgentCore se borra con `AgentCoreFleet.delete()` (`rayito agent
        delete --target agentcore`).
    Ejemplo:
        options = AgentDeployOptions(bucket="amzn-s3-demo-bucket")
        fleet = deploy_agent("./mi-agente", options=options)
        fleet.invoke("Resume el README.").text
        fleet.stop()
    """
    require_target(target)
    check_agentcore_options_target(agentcore, target)
    check_microvm_only_options(options, target)
    if target == TARGET_AGENTCORE:
        from rayito._agentcore._deploy import deploy_agentcore

        return deploy_agentcore(
            None,
            path,
            project=project,
            options=options,
            overrides=overrides,
            region=region,
            session=session,
            agentcore=agentcore,
        )
    return deploy_with(
        DeployPorts(),
        path,
        project=project,
        options=options,
        overrides=overrides,
        region=region,
        session=session,
    )


@overload
async def deploy_agent_async(
    path: str | os.PathLike[str] = ...,
    *,
    project: AgentProject | None = ...,
    options: AgentDeployOptions | None = ...,
    overrides: DetectOverrides | None = ...,
    region: str | None = ...,
    session: Any = ...,
    target: Literal["microvm"] = ...,
    agentcore: None = ...,
) -> AsyncAgentFleet: ...


@overload
async def deploy_agent_async(
    path: str | os.PathLike[str] = ...,
    *,
    project: AgentProject | None = ...,
    options: AgentDeployOptions | None = ...,
    overrides: DetectOverrides | None = ...,
    region: str | None = ...,
    session: Any = ...,
    target: Literal["agentcore"],
    agentcore: AgentCoreOptions | None = ...,
) -> AsyncAgentCoreFleet: ...


async def deploy_agent_async(
    path: str | os.PathLike[str] = ".",
    *,
    project: AgentProject | None = None,
    options: AgentDeployOptions | None = None,
    overrides: DetectOverrides | None = None,
    region: str | None = None,
    session: Any = None,
    target: Literal["microvm", "agentcore"] = "microvm",
    agentcore: AgentCoreOptions | None = None,
) -> AsyncAgentFleet | AsyncAgentCoreFleet:
    """Como `deploy_agent()`, con `AsyncAgentFleet` (o `AsyncAgentCoreFleet`
    con `target="agentcore"`): mismas opciones, mismo orden de resolución y
    mismo coste. Las lecturas de AWS previas a crear nada corren en un hilo
    para no bloquear el bucle de eventos.

    Coste y activación
    -------------------
    Activa: `await deploy_agent_async()`; nada más lo crea.
    Recursos y llamadas AWS: los de `deploy_agent()`.
    Coste aproximado: el de `deploy_agent()`, por VM (2 GB: ≈ $0,13 por hora en
        us-east-1, consultado 2026-10-06 en https://aws.amazon.com/lambda/pricing/);
        con `target="agentcore"`, el de AgentCore (V1: $0,0895 por vCPU-hora y
        $0,00945 por GB-hora de uso real, us-east-1, consultado 2026-10-19 en
        https://aws.amazon.com/bedrock/agentcore/pricing/) más CodeBuild, ECR y
        logs como se detalla en `deploy_agent()`.
    IAM: los de `deploy_agent()`.
    Cómo apagarla: `await fleet.stop()` (en AgentCore, `await fleet.delete()`);
        sin llamar a esta función nada se crea.
    Ejemplo:
        fleet = await deploy_agent_async("./mi-agente")
        (await fleet.invoke("Resume el README.")).text
        await fleet.stop()
    """
    require_target(target)
    check_agentcore_options_target(agentcore, target)
    check_microvm_only_options(options, target)
    if target == TARGET_AGENTCORE:
        from rayito._agentcore._deploy import deploy_agentcore_async

        return await deploy_agentcore_async(
            None,
            path,
            project=project,
            options=options,
            overrides=overrides,
            region=region,
            session=session,
            agentcore=agentcore,
        )
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
    prepared = prepare_secret_env(detected, chosen.create_secrets_from, check_env=chosen.check_env)
    _emit(chosen, _warning_steps(prepared.warnings))
    flight = preflight(prepared.project, chosen, ports, region=region, session=session)
    _emit(chosen, flight.steps)
    _publish_env_secret(prepared.secret, ports, flight, chosen, session)
    service = ProjectService(
        flight.project,
        **_secret_reader_kwargs(flight, secret_reader_for, session),
        **dict(ports.service_kwargs),
    )
    if flight.bucket is not None:
        info = service.build(
            bucket=flight.bucket,
            timeout=chosen.build_timeout_seconds,
            on_build_logs=chosen.on_build_logs,
            region=flight.region,
            session=session,
            base_version=base_version_of(chosen, flight.project),
        )
        _emit(chosen, (AgentDeployStep(STEP_IMAGE, image_version_of(info)),))
    fleet = AgentFleet(
        flight.project,
        access_token=flight.token,
        scaling=deploy_scaling(flight.project, chosen),
        session_index=chosen.session_index,
        pool_backend=chosen.pool_backend,
        keep_on_failure=chosen.keep_on_failure,
        region=flight.region,
        session=session,
        renew_gateway_keys=chosen.renew_gateway_keys,
        **{"service": service, **dict(ports.fleet_kwargs)},
    )
    fleet._renew_keys(force=True)
    fleet.start()
    _emit(chosen, (AgentDeployStep(STEP_FLEET, str(len(fleet.sandbox_ids))),))
    _emit(chosen, _recipe_steps(fleet._save_recipe() if chosen.save_recipe else None))
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
    prepared = await asyncio.to_thread(
        prepare_secret_env, detected, chosen.create_secrets_from, check_env=chosen.check_env
    )
    _emit(chosen, _warning_steps(prepared.warnings))
    flight = await asyncio.to_thread(
        preflight, prepared.project, chosen, ports, region=region, session=session
    )
    _emit(chosen, flight.steps)
    await asyncio.to_thread(_publish_env_secret, prepared.secret, ports, flight, chosen, session)
    service = AsyncProjectService(
        flight.project,
        **_secret_reader_kwargs(flight, async_secret_reader_for, session),
        **dict(ports.service_kwargs),
    )
    if flight.bucket is not None:
        info = await service.build(
            bucket=flight.bucket,
            timeout=chosen.build_timeout_seconds,
            on_build_logs=chosen.on_build_logs,
            region=flight.region,
            session=session,
            base_version=base_version_of(chosen, flight.project),
        )
        _emit(chosen, (AgentDeployStep(STEP_IMAGE, image_version_of(info)),))
    fleet = AsyncAgentFleet(
        flight.project,
        access_token=flight.token,
        scaling=deploy_scaling(flight.project, chosen),
        session_index=chosen.session_index,
        pool_backend=chosen.pool_backend,
        keep_on_failure=chosen.keep_on_failure,
        region=flight.region,
        session=session,
        renew_gateway_keys=chosen.renew_gateway_keys,
        **{"service": service, **dict(ports.fleet_kwargs)},
    )
    await fleet._renew_keys(force=True)
    await fleet.start()
    _emit(chosen, (AgentDeployStep(STEP_FLEET, str(len(fleet.sandbox_ids))),))
    _emit(chosen, _recipe_steps(await fleet._save_recipe() if chosen.save_recipe else None))
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
    """Lo que se resuelve antes de crear nada: el bucket, la imagen base, el
    secreto del modelo y el token. Todo lo que lee AWS ocurre aquí, y lo único
    que puede crear es un secreto (con `create_secret_from_env`) o la imagen
    base (con `base_publisher`). La imagen base sólo se exige si el despliegue
    va a construir (hay bucket): reutilizar la imagen del proyecto
    (`build_image=False`, o sin bucket y con la imagen ya creada) no la lee."""
    if options.renew_gateway_keys:
        renewable_gateways(project)
    environ = os.environ if ports.environ is None else ports.environ
    effective_region = resolved_region(region, project, session, environ)
    aws = ports.aws or AwsResolver(region=effective_region, session=session)
    steps: list[AgentDeployStep] = []
    bucket_steps: list[AgentDeployStep] = []
    bucket = _bucket_for(project, options, aws, environ, bucket_steps)
    if bucket is not None and ensure_base_image(
        aws,
        project.base,
        options.base_publisher,
        ensure=options.ensure_base_image,
        template=template_name(project),
    ):
        steps.append(AgentDeployStep(STEP_BASE_IMAGE, "published"))
    store = ports.store or SecretStore(
        region=effective_region, session=session, prefix=GATEWAY_SECRET_PREFIX
    )
    secrets = ensure_model_secrets(
        project,
        secret_names=options.secret_names,
        create_from_env=options.create_secret_from_env,
        store=store,
        environ=environ,
    )
    steps.extend(AgentDeployStep(STEP_SECRET_CREATED, gateway) for gateway in secrets.created)
    resolved = with_default_excludes(dataclasses.replace(project, gateways=secrets.gateways))
    steps.extend(bucket_steps)
    token = resolve_token(options.access_token, environ)
    return Preflight(resolved, bucket, token, effective_region, tuple(steps))


def _recipe_steps(outcome: str | None) -> tuple[AgentDeployStep, ...]:
    """El paso `recipe` sólo si la receta no se guardó: su detalle es el
    código del aviso. Guardada (o sin pedirla), ningún paso: un despliegue
    sin incidencias informa de los mismos pasos que en 0.16."""
    if outcome is None or outcome == RECIPE_SAVED:
        return ()
    return (AgentDeployStep(STEP_RECIPE, outcome),)


def _publish_env_secret(
    secret: EnvFileSecret | None,
    ports: DeployPorts,
    flight: Preflight,
    options: AgentDeployOptions,
    session: Any,
) -> None:
    """Crea o actualiza el secreto de `create_secrets_from` (sólo si se pidió)
    y lo dice en `on_step`, sin nombrarlo."""
    if secret is None:
        return
    store = ports.secret_env_store or env_secret_store(flight.region, session)
    outcome = publish_env_file_secret(store, secret)
    _emit(options, (AgentDeployStep(STEP_SECRET_ENV_CREATED, outcome),))


def _warning_steps(warnings: tuple[str, ...]) -> tuple[AgentDeployStep, ...]:
    return tuple(AgentDeployStep(kind, detail) for kind, detail in env_missing_steps(warnings))


def _secret_reader_kwargs(
    flight: Preflight, reader_for: Callable[[str | None, Any], Any], session: Any
) -> dict[str, Any]:
    """El lector de `secret_env` con la región y la sesión del despliegue;
    nada (ni cliente) si el proyecto no lo usa."""
    project = flight.project
    if not uses_secret_env(project.secret_env, project.secret_env_from):
        return {}
    return {"secret_reader": reader_for(flight.region, session)}


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

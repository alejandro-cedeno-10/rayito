"""El despliegue de un agente en AgentCore desde un proyecto (ADR-031): lo que
`deploy_agent(target="agentcore")`, `plan_deploy(target="agentcore")` y
`rayito agent deploy --target agentcore` comparten. Detecta el proyecto, calcula
el plan sin llamar a AWS (`plan_agentcore`) y lo aplica sobre los puertos de AWS
(`apply_agentcore`); `deploy_agentcore` y `deploy_agentcore_async` son las dos
cosas juntas y devuelven la flota.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from rayito._agent._deploy_plan import (
    AgentDeployOptions,
    AgentDeployStep,
    project_for,
    resolved_region,
    with_default_excludes,
)
from rayito._agent._export import RawAppNotExportable, render_export
from rayito._agent._project_domain import AgentProject
from rayito._agent._secret_env_deploy import (
    STEP_SECRET_ENV_CREATED,
    EnvFileSecret,
    EnvSecretStore,
    env_missing_steps,
    env_secret_store,
    prepare_secret_env,
    publish_env_file_secret,
)
from rayito._agentcore._domain import build_plan
from rayito._agentcore._models import (
    AgentCoreApplyRequest,
    AgentCoreDeployment,
    AgentCoreDeployPlan,
    AgentCoreOptions,
)
from rayito._agentcore._ports import AgentCoreGateways, RuntimeInvoker
from rayito._agentcore._service import AgentCoreDeployer
from rayito._aws_region import aws_session
from rayito.exceptions import AgentCoreException

if TYPE_CHECKING:
    from rayito._agent._detect_report import DetectOverrides
    from rayito._agentcore._fleet import AgentCoreFleet
    from rayito._agentcore._fleet_async import AsyncAgentCoreFleet


@dataclass(frozen=True)
class AgentCorePorts:
    """Las dependencias de `deploy_agent(target="agentcore")` que los tests
    sustituyen: los puertos de AWS (`gateways`), el invocador y el entorno. En
    producción es el valor por defecto y no se pasa."""

    gateways: AgentCoreGateways | None = None
    invoker: RuntimeInvoker | None = None
    environ: Mapping[str, str] | None = None
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    secret_env_store: EnvSecretStore | None = None


@dataclass(frozen=True)
class PlannedDeploy:
    """Un despliegue planificado: el proyecto, las opciones de la llamada, el
    plan, el árbol de ficheros del contexto de build y, con
    `create_secrets_from`, el secreto que se creará al aplicar."""

    project: AgentProject
    options: AgentDeployOptions
    plan: AgentCoreDeployPlan
    tree: Mapping[str, bytes]
    env_file_secret: EnvFileSecret | None = None
    env_warnings: tuple[str, ...] = ()


def render_agentcore_tree(project: AgentProject) -> Mapping[str, bytes]:
    """El contexto de build del agente para AgentCore; una app `raw` es
    `raw_protocol`."""
    try:
        return render_export(project, target="agentcore")
    except RawAppNotExportable:
        raise AgentCoreException("raw_protocol") from None


def plan_agentcore(
    path: str | os.PathLike[str],
    *,
    project: AgentProject | None = None,
    options: AgentDeployOptions | None = None,
    overrides: DetectOverrides | None = None,
    region: str | None = None,
    session: Any = None,
    agentcore: AgentCoreOptions | None = None,
    environ: Mapping[str, str] | None = None,
    cli_flags: Collection[str] = (),
    adjust: Callable[[AgentProject], AgentProject] | None = None,
) -> PlannedDeploy:
    """El plan de desplegar el proyecto de `path` en AgentCore, sin llamar a
    AWS: lee ficheros locales y la configuración local de boto3, no ejecuta el
    código del agente ni crea ningún cliente. `adjust` cambia el proyecto
    detectado antes de planificar (lo que la CLI hace con sus banderas) y
    `cli_flags` son las banderas de MicroVMs que se pasaron, para
    `option_not_supported`."""
    chosen = options or AgentDeployOptions()
    detected, report = project_for(path, project, overrides, region)
    if adjust is not None:
        detected = adjust(detected)
    prepared = prepare_secret_env(detected, chosen.create_secrets_from, check_env=chosen.check_env)
    detected = with_default_excludes(prepared.project)
    variables = os.environ if environ is None else environ
    effective = resolved_region(region, detected, session, variables)
    trees: list[Mapping[str, bytes]] = []

    def tree_for() -> Mapping[str, bytes]:
        trees.append(render_agentcore_tree(detected))
        return trees[0]

    plan = build_plan(
        detected,
        chosen,
        agentcore or AgentCoreOptions(),
        region=effective or aws_session(session, None).region_name,
        environ=variables,
        tree_for=tree_for,
        detection=report,
        cli_flags=cli_flags,
    )
    return PlannedDeploy(detected, chosen, plan, trees[0], prepared.secret, prepared.warnings)


def publish_planned_secret(
    planned: PlannedDeploy, ports: AgentCorePorts | None, session: Any
) -> None:
    """Crea o actualiza el secreto de `create_secrets_from` antes de aplicar
    (sólo si se pidió), en la región del despliegue, y cuenta los avisos y la
    creación en `on_step`."""
    on_step = planned.options.on_step
    if on_step is not None:
        for kind, detail in env_missing_steps(planned.env_warnings):
            on_step(AgentDeployStep(kind, detail))
    secret = planned.env_file_secret
    if secret is None:
        return
    store = ports.secret_env_store if ports is not None else None
    chosen = store or env_secret_store(planned.plan.region, session)
    outcome = publish_env_file_secret(chosen, secret)
    if on_step is not None:
        on_step(AgentDeployStep(STEP_SECRET_ENV_CREATED, outcome))


def agentcore_gateways(
    planned: PlannedDeploy, ports: AgentCorePorts | None, session: Any
) -> AgentCoreGateways:
    """Los puertos del agente: los de `ports` o los adaptadores de AWS (que no
    llaman a nada al construirse)."""
    if ports is not None and ports.gateways is not None:
        return ports.gateways
    from rayito._agentcore._aws import gateways_for

    plan = planned.plan
    return gateways_for(plan.name, plan.region, session=session, tags=plan.tags)


def agentcore_invoker(
    planned: PlannedDeploy, ports: AgentCorePorts | None, session: Any
) -> RuntimeInvoker:
    if ports is not None and ports.invoker is not None:
        return ports.invoker
    from rayito._agentcore._aws import AgentCoreClients, AgentCoreInvoker

    return AgentCoreInvoker(AgentCoreClients(planned.plan.region, session))


def apply_request(
    planned: PlannedDeploy, on_progress: Callable[[str, str], None] | None
) -> AgentCoreApplyRequest:
    options = planned.options
    model_region = planned.project.spec.model.region or planned.plan.region
    return AgentCoreApplyRequest(
        tree=planned.tree,
        model_region=model_region,
        build_image=options.build_image,
        build_timeout_seconds=options.build_timeout_seconds,
        on_progress=on_progress,
        on_build_logs=options.on_build_logs,
    )


def step_reporter(options: AgentDeployOptions) -> Callable[[str, str], None] | None:
    """Cada línea de progreso como un `AgentDeployStep` para `on_step`: `kind`
    es la clave de `PROGRESS_MESSAGES` y `detail` la línea formateada."""
    on_step = options.on_step
    if on_step is None:
        return None

    def report(key: str, message: str) -> None:
        on_step(AgentDeployStep(key, message))

    return report


def apply_agentcore(
    planned: PlannedDeploy,
    gateways: AgentCoreGateways,
    on_progress: Callable[[str, str], None] | None = None,
    ports: AgentCorePorts | None = None,
) -> AgentCoreDeployment:
    """Aplica un plan: la pila, la imagen y el runtime. `ports` trae el reloj
    y el sueño de las esperas, que los tests sustituyen."""
    chosen = ports or AgentCorePorts()
    deployer = AgentCoreDeployer(gateways, clock=chosen.clock, sleep=chosen.sleep)
    return deployer.apply(planned.plan, apply_request(planned, on_progress))


def deploy_agentcore(
    ports: AgentCorePorts | None,
    path: str | os.PathLike[str],
    *,
    project: AgentProject | None = None,
    options: AgentDeployOptions | None = None,
    overrides: DetectOverrides | None = None,
    region: str | None = None,
    session: Any = None,
    agentcore: AgentCoreOptions | None = None,
) -> AgentCoreFleet:
    """Planifica y aplica el despliegue y devuelve la flota, sin preguntar
    nada."""
    from rayito._agentcore._fleet import AgentCoreFleet

    planned = plan_agentcore(
        path,
        project=project,
        options=options,
        overrides=overrides,
        region=region,
        session=session,
        agentcore=agentcore,
        environ=None if ports is None else ports.environ,
    )
    gateways = agentcore_gateways(planned, ports, session)
    publish_planned_secret(planned, ports, session)
    deployment = apply_agentcore(planned, gateways, step_reporter(planned.options), ports)
    return AgentCoreFleet.from_deployment(
        deployment, gateways=gateways, invoker=agentcore_invoker(planned, ports, session)
    )


async def deploy_agentcore_async(
    ports: AgentCorePorts | None,
    path: str | os.PathLike[str],
    *,
    project: AgentProject | None = None,
    options: AgentDeployOptions | None = None,
    overrides: DetectOverrides | None = None,
    region: str | None = None,
    session: Any = None,
    agentcore: AgentCoreOptions | None = None,
) -> AsyncAgentCoreFleet:
    """Como `deploy_agentcore`, con `AsyncAgentCoreFleet`: lo bloqueante corre
    en un hilo."""
    from rayito._agentcore._fleet_async import AsyncAgentCoreFleet
    from rayito._agentcore._service_async import AsyncAgentCoreDeployer

    planned = await asyncio.to_thread(
        plan_agentcore,
        path,
        project=project,
        options=options,
        overrides=overrides,
        region=region,
        session=session,
        agentcore=agentcore,
        environ=None if ports is None else ports.environ,
    )
    gateways = agentcore_gateways(planned, ports, session)
    await asyncio.to_thread(publish_planned_secret, planned, ports, session)
    request = apply_request(planned, step_reporter(planned.options))
    chosen = ports or AgentCorePorts()
    deployer = AsyncAgentCoreDeployer(gateways, clock=chosen.clock, sleep=chosen.sleep)
    deployment = await deployer.apply(planned.plan, request)
    return AsyncAgentCoreFleet.from_deployment(
        deployment, gateways=gateways, invoker=agentcore_invoker(planned, ports, session)
    )

"""Servicio de aplicación de `--target agentcore` (ADR-031): `AgentCoreDeployer`
observa la cuenta con lecturas y decide con el dominio puro, en el orden de
`design.md` («Apply sequence»): identidad y bucket, la pila, la imagen y el
runtime; `AgentCoreDeleter` borra sólo lo que Rayito creó para un nombre. Sólo
habla con puertos (`_ports.py`).

Cada espera sondea con el reloj y el sueño inyectados, así que los tests no
duermen.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from rayito._agentcore._domain import (
    BUILD_IN_PROGRESS,
    BUILD_SUCCEEDED,
    RUNTIME_IN_PROGRESS,
    RUNTIME_READY,
    STACK_PARAMETER_AGENT_NAME,
    STACK_PARAMETER_BUCKET,
    STACK_PARAMETER_BUILD_TIMEOUT,
    STACK_PARAMETER_LOG_DAYS,
    STACK_PARAMETER_MODEL_ARNS,
    STACK_PARAMETER_RETENTION,
    STACK_PARAMETER_RUNTIME_NAME,
    STACK_PARAMETER_SECRET_ARNS,
    StackOutputs,
    agent_name_of_runtime,
    base_image_reference,
    bedrock_resource_arns,
    check_image_size,
    check_runtime_usable,
    decide_image,
    decide_runtime,
    decide_stack,
    image_uri_for,
    is_owned,
    runtime_description_for,
    runtime_log_group_for,
    secret_resource_arns,
    source_key_for,
    source_zip,
    stack_outputs_of,
    validate_agent_name,
)
from rayito._agentcore._messages import DELETE_PROGRESS_MESSAGES, PROGRESS_MESSAGES
from rayito._agentcore._models import (
    AgentCoreApplyRequest,
    AgentCoreDeletePlan,
    AgentCoreDeleteResult,
    AgentCoreDeployment,
    AgentCoreDeployPlan,
    AgentCoreListItem,
    AgentCoreStatus,
    AgentCoreStep,
    RuntimeListing,
    RuntimeSpec,
    StepAction,
)
from rayito._agentcore._ports import (
    AgentCoreGateways,
    RuntimeAlreadyExists,
    RuntimeControl,
    RuntimeDescription,
    RuntimeNotFound,
    StackResourceMissing,
)
from rayito._agentcore._stack import AGENTCORE_STACK_COMPONENT
from rayito._limits import (
    AGENT_EXPORT_BASE_IMAGE_DIGEST,
    AGENTCORE_BUILD_LOG_RETENTION_DAYS,
    AGENTCORE_BUILD_POLL_SECONDS,
    AGENTCORE_BUILD_TIMEOUT_MINUTES,
    AGENTCORE_DELETE_TIMEOUT_SECONDS,
    AGENTCORE_IMAGE_RETENTION_COUNT,
    AGENTCORE_READY_POLL_SECONDS,
    AGENTCORE_READY_TIMEOUT_SECONDS,
)
from rayito._stacks._condition import StackObservation
from rayito._stacks._model import stack_tags
from rayito.exceptions import AgentCoreException

Clock = Callable[[], float]
Sleeper = Callable[[float], None]
BYTES_PER_MEGABYTE: Final = 1_000_000
ARN_LIST_SEPARATOR: Final = ","
RUNTIME_DELETING: Final = "DELETING"


@dataclass
class _Reporter:
    """Entrega al llamante cada línea de progreso, con su clave."""

    sink: Callable[[str, str], None] | None

    def __call__(self, key: str, **fields: object) -> None:
        if self.sink is not None:
            self.sink(key, PROGRESS_MESSAGES[key].format(**fields))


def find_runtime(runtimes: RuntimeControl, runtime_name: str) -> RuntimeListing | None:
    """El runtime que se llama `runtime_name` (`ListAgentRuntimes`), sea o no
    de Rayito."""
    return next((item for item in runtimes.list_runtimes() if item.name == runtime_name), None)


def list_agents(runtimes: RuntimeControl) -> tuple[AgentCoreListItem, ...]:
    """Los agentes de Rayito de la región: los runtimes con el prefijo y la
    marca de Rayito, ordenados por nombre."""
    items = [
        AgentCoreListItem(
            name=agent_name_of_runtime(item),
            runtime_name=item.name,
            runtime_id=item.runtime_id,
            runtime_version=item.version,
            status=item.status,
        )
        for item in runtimes.list_runtimes()
        if is_owned(item)
    ]
    return tuple(sorted(items, key=lambda item: item.name))


def describe_agent(
    runtimes: RuntimeControl, name: str, region: str, runtime_name: str
) -> tuple[RuntimeDescription, AgentCoreStatus]:
    """El runtime de Rayito llamado `runtime_name` y su estado; si no existe
    o no es de Rayito, `runtime_not_found`."""
    listing = find_runtime(runtimes, runtime_name)
    if listing is None or not is_owned(listing):
        raise AgentCoreException("runtime_not_found", name=name, region=region)
    try:
        described = runtimes.get_runtime(listing.runtime_id)
    except RuntimeNotFound:
        raise AgentCoreException("runtime_not_found", name=name, region=region) from None
    return described, status_of(name, region, described)


def status_of(name: str, region: str, described: RuntimeDescription) -> AgentCoreStatus:
    listing = described.listing
    return AgentCoreStatus(
        name=name,
        region=region,
        runtime_name=listing.name,
        runtime_id=listing.runtime_id,
        runtime_arn=listing.arn,
        runtime_version=listing.version,
        status=listing.status,
        image_uri=described.details.container_uri,
        log_group=runtime_log_group_for(listing.runtime_id),
    )


class AgentCoreDeployer:
    """Despliega un agente en AgentCore: `apply(plan, request)` crea o actualiza
    la pila, construye la imagen con CodeBuild si su contenido cambió, y crea o
    actualiza el runtime con MMDSv2 exigido, sin duplicar nada al repetirse.

    Coste y activación
    -------------------
    Activa: sólo `deploy_agent(target="agentcore")` y `rayito agent deploy
        --target agentcore` (ningún `create()`, listado ni getter del SDK lo
        llama). Construirlo no llama a AWS ni carga ningún cliente.
    Recursos y llamadas AWS: la pila `rayito-agentcore-<nombre>` (repositorio
        de ECR, proyecto de CodeBuild y dos roles), un zip del contexto de
        build en el bucket de artefactos mientras dura el build, un build de
        CodeBuild si el contenido cambió y un runtime de AgentCore creado o
        actualizado; de lectura, `GetCallerIdentity`, `HeadBucket`,
        `DescribeStacks`, `DescribeImages`, `ListAgentRuntimes` y
        `GetAgentRuntime`.
    Coste aproximado: ver `deploy_agent()`.
    IAM: los de la lista de permisos de la guía de AgentCore (CloudFormation
        con `CAPABILITY_IAM`, ECR, CodeBuild, S3 del prefijo del contexto,
        AgentCore y `iam:PassRole` del rol del runtime).
    Cómo apagarla: no desplegar; `rayito agent delete --target agentcore`
        borra lo creado.
    Ejemplo:
        deployment = AgentCoreDeployer(gateways).apply(plan, request)
    """

    def __init__(
        self,
        gateways: AgentCoreGateways,
        *,
        clock: Clock = time.monotonic,
        sleep: Sleeper = time.sleep,
    ) -> None:
        self._gateways = gateways
        self._clock = clock
        self._sleep = sleep

    def apply(
        self, plan: AgentCoreDeployPlan, request: AgentCoreApplyRequest
    ) -> AgentCoreDeployment:
        report = _Reporter(request.on_progress)
        account = self._gateways.identity.account_id()
        bucket = self._resolve_bucket(plan)
        check_runtime_usable(
            find_runtime(self._gateways.runtimes, plan.runtime_name), plan.runtime_name, plan.name
        )
        stack_step, outputs, stack_status = self._apply_stack(
            plan, request, account, bucket, report
        )
        try:
            image_step = self._apply_image(plan, request, bucket, outputs, report)
        except StackResourceMissing:
            raise AgentCoreException(
                "stack_failed", stackName=plan.stack_name, status=stack_status
            ) from None
        try:
            runtime_step, described = self._apply_runtime(plan, outputs, report)
        except RuntimeNotFound:
            raise AgentCoreException(
                "runtime_not_found", name=plan.name, region=plan.region
            ) from None
        listing = described.listing
        return AgentCoreDeployment(
            name=plan.name,
            region=plan.region,
            runtime_name=listing.name,
            runtime_id=listing.runtime_id,
            runtime_arn=listing.arn,
            runtime_version=listing.version,
            status=listing.status,
            image_uri=described.details.container_uri,
            stack_name=plan.stack_name,
            log_group=runtime_log_group_for(listing.runtime_id),
            steps=(stack_step, image_step, runtime_step),
        )

    def _resolve_bucket(self, plan: AgentCoreDeployPlan) -> str:
        """El bucket del contexto de build: el del plan o el que se descubre,
        siempre en la región del despliegue (CodeBuild sólo lee el código de un
        bucket de su región)."""
        buckets = self._gateways.bucket
        bucket = plan.bucket or buckets.discover()
        if not bucket:
            raise AgentCoreException("no_bucket")
        bucket_region = buckets.region_of(bucket)
        if bucket_region is None:
            raise AgentCoreException("no_bucket")
        if bucket_region != plan.region:
            raise AgentCoreException(
                "bucket_region", bucket=bucket, bucketRegion=bucket_region, region=plan.region
            )
        return bucket

    # ----------------------------------------------------------------- pila

    def _stack_parameters(
        self, plan: AgentCoreDeployPlan, request: AgentCoreApplyRequest, account: str, bucket: str
    ) -> dict[str, str]:
        models = bedrock_resource_arns(plan.bedrock_models, request.model_region, account)
        secrets = secret_resource_arns(plan.readable_secrets, plan.region, account)
        return {
            STACK_PARAMETER_AGENT_NAME: plan.name,
            STACK_PARAMETER_RUNTIME_NAME: plan.runtime_name,
            STACK_PARAMETER_BUCKET: bucket,
            STACK_PARAMETER_MODEL_ARNS: ARN_LIST_SEPARATOR.join(models),
            STACK_PARAMETER_SECRET_ARNS: ARN_LIST_SEPARATOR.join(secrets),
            STACK_PARAMETER_RETENTION: str(AGENTCORE_IMAGE_RETENTION_COUNT),
            STACK_PARAMETER_LOG_DAYS: str(AGENTCORE_BUILD_LOG_RETENTION_DAYS),
            STACK_PARAMETER_BUILD_TIMEOUT: str(AGENTCORE_BUILD_TIMEOUT_MINUTES),
        }

    def _apply_stack(
        self,
        plan: AgentCoreDeployPlan,
        request: AgentCoreApplyRequest,
        account: str,
        bucket: str,
        report: _Reporter,
    ) -> tuple[AgentCoreStep, StackOutputs, str]:
        stack = self._gateways.stack
        observed = stack.observe()
        first = decide_stack(observed, plan.name)
        if first == "create" and request.build_image is False:
            raise AgentCoreException(
                "image_missing", imageTag=plan.image_tag, repositoryName=plan.repository_name
            )
        if first == "wait":
            report("stackWait", stackName=plan.stack_name, status=observed.status)
            observed = stack.wait()
            decide_stack(observed, plan.name)
        report("stackDeploy", stackName=plan.stack_name)
        deployment = stack.deploy(self._stack_parameters(plan, request, account, bucket))
        action = _stack_action(first, observed, deployment.changed)
        outputs = stack_outputs_of(deployment.observation)
        report("stackReady" if deployment.changed else "stackNoChanges", stackName=plan.stack_name)
        status = observed.status if action != "create" else None
        final_status = str(deployment.observation.status)
        return AgentCoreStep("stack", plan.stack_name, action, status), outputs, final_status

    # --------------------------------------------------------------- imagen

    def _apply_image(
        self,
        plan: AgentCoreDeployPlan,
        request: AgentCoreApplyRequest,
        bucket: str,
        outputs: StackOutputs,
        report: _Reporter,
    ) -> AgentCoreStep:
        registry = self._gateways.registry
        found = registry.describe(outputs.repository_name, plan.image_tag)
        action = decide_image(found, request.build_image, plan.image_tag, outputs.repository_name)
        if action == "build":
            self._build(plan, request, bucket, outputs, report)
            found = registry.describe(outputs.repository_name, plan.image_tag)
            report(
                "imageReady",
                imageTag=plan.image_tag,
                megabytes=found.size_bytes // BYTES_PER_MEGABYTE,
            )
        else:
            report("imageReused", imageTag=plan.image_tag)
        check_image_size(found.size_bytes)
        return AgentCoreStep("image", plan.repository_name, action)

    def _build(
        self,
        plan: AgentCoreDeployPlan,
        request: AgentCoreApplyRequest,
        bucket: str,
        outputs: StackOutputs,
        report: _Reporter,
    ) -> None:
        data = source_zip(request.tree)
        key = source_key_for(plan.name, plan.image_tag)
        report("sourceUpload", bytes=len(data))
        self._gateways.bucket.put(bucket, key, data)
        try:
            started = self._gateways.builder.start(
                outputs.build_project_name,
                bucket,
                key,
                plan.image_tag,
                base_image_reference(AGENT_EXPORT_BASE_IMAGE_DIGEST),
            )
            report("buildStart", imageTag=plan.image_tag, buildId=started.build_id)
            self._await_build(started.build_id, outputs, request)
        finally:
            self._gateways.bucket.delete(bucket, key)
            report("sourceDeleted")

    def _await_build(
        self, build_id: str, outputs: StackOutputs, request: AgentCoreApplyRequest
    ) -> None:
        deadline = self._clock() + request.build_timeout_seconds
        phase = ""
        while True:
            observed = self._gateways.builder.get(build_id)
            if observed.phase != phase and request.on_build_logs is not None and observed.phase:
                request.on_build_logs(observed.phase)
            phase = observed.phase
            log_group = observed.log_group or outputs.build_log_group
            if observed.status != BUILD_IN_PROGRESS:
                break
            if self._clock() >= deadline:
                raise AgentCoreException(
                    "image_build_timeout",
                    buildId=build_id,
                    seconds=int(request.build_timeout_seconds),
                    logGroup=log_group,
                )
            self._sleep(AGENTCORE_BUILD_POLL_SECONDS)
        if observed.status != BUILD_SUCCEEDED:
            raise AgentCoreException(
                "image_build_failed", buildId=build_id, status=observed.status, logGroup=log_group
            )

    # -------------------------------------------------------------- runtime

    def _spec(self, plan: AgentCoreDeployPlan, outputs: StackOutputs) -> RuntimeSpec:
        return RuntimeSpec(
            name=plan.runtime_name,
            description=runtime_description_for(plan.name),
            container_uri=image_uri_for(outputs.repository_uri, plan.image_tag),
            role_arn=outputs.runtime_role_arn,
            environment_variables=plan.environment,
            idle_runtime_session_timeout=plan.lifecycle.idle_session_timeout_seconds,
            max_lifetime=plan.lifecycle.max_session_lifetime_seconds,
            tags=stack_tags(AGENTCORE_STACK_COMPONENT, dict(plan.tags)),
        )

    def _observe_runtime(
        self, plan: AgentCoreDeployPlan, spec: RuntimeSpec
    ) -> tuple[RuntimeListing | None, RuntimeDescription | None]:
        runtimes = self._gateways.runtimes
        listing = find_runtime(runtimes, plan.runtime_name)
        if listing is None or not is_owned(listing) or listing.status != RUNTIME_READY:
            return listing, None
        return listing, runtimes.get_runtime(listing.runtime_id)

    def _apply_runtime(
        self, plan: AgentCoreDeployPlan, outputs: StackOutputs, report: _Reporter
    ) -> tuple[AgentCoreStep, RuntimeDescription]:
        spec = self._spec(plan, outputs)
        listing, described = self._observe_runtime(plan, spec)
        details = None if described is None else described.details
        first = decide_runtime(listing, details, spec, plan.name)
        action: StepAction = first
        if first == "wait" and listing is not None:
            report("runtimeWait", runtimeName=spec.name, status=listing.status)
            described = self._await_ready(listing.runtime_id, spec.name)
            action = decide_runtime(described.listing, described.details, spec, plan.name)
        if action == "create":
            described, action = self._create(plan, spec, report)
        elif action == "update" and listing is not None:
            described = self._update(listing.runtime_id, spec, report)
        elif described is not None:
            report("runtimeReuse", runtimeName=spec.name, version=described.listing.version)
        if described is None:
            raise AgentCoreException("runtime_not_found", name=plan.name, region=plan.region)
        shown: StepAction = first if first == "wait" else action
        status = None if listing is None else listing.status
        return AgentCoreStep("runtime", plan.runtime_name, shown, status), described

    def _create(
        self, plan: AgentCoreDeployPlan, spec: RuntimeSpec, report: _Reporter
    ) -> tuple[RuntimeDescription, StepAction]:
        runtimes = self._gateways.runtimes
        report("runtimeCreate", runtimeName=spec.name)
        try:
            created = runtimes.create_runtime(spec)
        except RuntimeAlreadyExists:
            winner = find_runtime(runtimes, plan.runtime_name)
            if winner is None or not is_owned(winner):
                raise AgentCoreException(
                    "name_conflict", runtimeName=spec.name, name=plan.name
                ) from None
            self._await_ready(winner.runtime_id, spec.name)
            return self._update(winner.runtime_id, spec, report), "update"
        self._await_ready(created.runtime_id, spec.name)
        report("runtimeRequireMmdsV2", runtimeName=spec.name)
        return self._update(created.runtime_id, spec, report, announce=False), "create"

    def _update(
        self, runtime_id: str, spec: RuntimeSpec, report: _Reporter, *, announce: bool = True
    ) -> RuntimeDescription:
        if announce:
            report("runtimeUpdate", runtimeName=spec.name)
        base_version = self._gateways.runtimes.get_runtime(runtime_id).listing.version
        self._gateways.runtimes.update_runtime(runtime_id, spec, base_version)
        described = self._await_ready(runtime_id, spec.name)
        report("runtimeReady", runtimeName=spec.name, version=described.listing.version)
        return described

    def _await_ready(self, runtime_id: str, runtime_name: str) -> RuntimeDescription:
        """Sondea `GetAgentRuntime` hasta `READY`; cualquier estado que no sea
        `CREATING` ni `UPDATING` es `runtime_failed` y vencer el plazo,
        `runtime_timeout`."""
        deadline = self._clock() + AGENTCORE_READY_TIMEOUT_SECONDS
        while True:
            described = self._gateways.runtimes.get_runtime(runtime_id)
            status = described.listing.status
            if status == RUNTIME_READY:
                return described
            if status not in RUNTIME_IN_PROGRESS:
                raise AgentCoreException(
                    "runtime_failed",
                    runtimeName=runtime_name,
                    status=status,
                    logGroup=runtime_log_group_for(runtime_id),
                )
            if self._clock() >= deadline:
                raise AgentCoreException(
                    "runtime_timeout",
                    runtimeName=runtime_name,
                    status=status,
                    seconds=int(AGENTCORE_READY_TIMEOUT_SECONDS),
                )
            self._sleep(AGENTCORE_READY_POLL_SECONDS)


def _stack_action(first: str, observed: StackObservation, changed: bool) -> StepAction:
    """Lo que se hizo con la pila: `wait` si había una operación en curso;
    si no, crearla, actualizarla si CloudFormation cambió algo o reutilizarla
    si respondió «No updates are to be performed»."""
    if first == "wait":
        return "wait"
    if first == "create":
        return "create"
    return "update" if changed else "reuse"


class AgentCoreDeleter:
    """Borra un agente de AgentCore: el runtime (y espera a que desaparezca),
    la pila `rayito-agentcore-<nombre>` y el grupo de logs del runtime. Sólo
    toca lo que Rayito creó con ese nombre: un runtime sin su marca es
    `name_conflict` y no se borra nada.

    Coste y activación
    -------------------
    Activa: sólo `rayito agent delete --target agentcore` y
        `AgentCoreFleet.delete()`. Construirlo no llama a AWS.
    Recursos y llamadas AWS: `ListAgentRuntimes`, `DeleteAgentRuntime` y
        `GetAgentRuntime` hasta que desaparece, `DescribeStacks` y
        `DeleteStack` (CloudFormation vacía el repositorio y borra los roles)
        y `DeleteLogGroup` del grupo del runtime.
    Coste aproximado: ninguno; borrar es gratis y deja de costar el
        almacenamiento de las imágenes.
    IAM: `bedrock-agentcore:ListAgentRuntimes`, `GetAgentRuntime` y
        `DeleteAgentRuntime`, CloudFormation sobre la pila del agente (y lo
        que sus recursos exigen) y `logs:DeleteLogGroup` sobre el grupo del
        runtime.
    Cómo apagarla: no llamar a `delete`.
    Ejemplo:
        deleter = AgentCoreDeleter(gateways)
        plan = deleter.plan("support-bot", "rayito_support_bot", "rayito-agentcore-support-bot")
        result = deleter.delete(plan)
    """

    def __init__(
        self,
        gateways: AgentCoreGateways,
        *,
        clock: Clock = time.monotonic,
        sleep: Sleeper = time.sleep,
        on_progress: Callable[[str], None] | None = None,
    ) -> None:
        self._gateways = gateways
        self._clock = clock
        self._sleep = sleep
        self._on_progress = on_progress

    def plan(self, name: str, runtime_name: str, stack_name: str) -> AgentCoreDeletePlan:
        """Lo que hay de `name` en la cuenta (sólo lecturas): el runtime de
        Rayito, la pila y el grupo de logs que se borrarían."""
        validate_agent_name(name)
        listing = find_runtime(self._gateways.runtimes, runtime_name)
        if listing is not None and not is_owned(listing):
            raise AgentCoreException("name_conflict", runtimeName=runtime_name, name=name)
        stack_exists = self._gateways.stack.observe().status is not None
        return AgentCoreDeletePlan(
            name=name,
            region=self._gateways.region,
            stack_name=stack_name,
            runtime=listing,
            stack_exists=stack_exists,
            log_group=None if listing is None else runtime_log_group_for(listing.runtime_id),
        )

    def delete(self, plan: AgentCoreDeletePlan) -> AgentCoreDeleteResult:
        runtime_deleted = self._delete_runtime(plan)
        stack_deleted = self._delete_stack(plan)
        log_group_deleted = self._delete_log_group(plan)
        return AgentCoreDeleteResult(
            name=plan.name,
            region=plan.region,
            runtime_deleted=runtime_deleted,
            stack_deleted=stack_deleted,
            log_group_deleted=log_group_deleted,
        )

    def _say(self, key: str, **fields: object) -> None:
        if self._on_progress is not None:
            self._on_progress(DELETE_PROGRESS_MESSAGES[key].format(**fields))

    def _delete_runtime(self, plan: AgentCoreDeletePlan) -> bool:
        listing = plan.runtime
        if listing is None:
            return False
        runtimes = self._gateways.runtimes
        self._say("runtime", runtimeName=listing.name)
        if listing.status != RUNTIME_DELETING:
            runtimes.delete_runtime(listing.runtime_id)
        deadline = self._clock() + AGENTCORE_DELETE_TIMEOUT_SECONDS
        while runtimes.runtime_exists(listing.runtime_id):
            if self._clock() >= deadline:
                raise AgentCoreException(
                    "delete_failed",
                    resource="runtime",
                    resourceName=listing.name,
                    status=RUNTIME_DELETING,
                )
            self._sleep(AGENTCORE_READY_POLL_SECONDS)
        return True

    def _delete_stack(self, plan: AgentCoreDeletePlan) -> bool:
        if not plan.stack_exists:
            return False
        self._say("stack", stackName=plan.stack_name)
        self._gateways.stack.destroy()
        return True

    def _delete_log_group(self, plan: AgentCoreDeletePlan) -> bool:
        if plan.log_group is None:
            return False
        self._say("logGroup", logGroup=plan.log_group)
        return self._gateways.logs.delete_log_group(plan.log_group)


__all__ = [
    "AgentCoreDeleter",
    "AgentCoreDeployer",
    "describe_agent",
    "find_runtime",
    "list_agents",
    "status_of",
]

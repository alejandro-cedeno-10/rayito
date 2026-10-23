"""Servicio de aplicación de `rayito setup` (ADR-030): `plan` observa la
cuenta con lecturas y decide con el dominio puro; `apply` ejecuta ese plan
en el orden del diseño D6. Sólo habla con puertos."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from rayito._account_setup._domain import (
    ARTIFACT_STACK_NAME,
    ARTIFACT_TEMPLATE_PATH,
    BASE_IMAGE_NAME,
    BUCKET_NAME_OUTPUT_KEY,
    CAPS_IMAGE_NAME,
    CAPS_IMAGE_OS_CAPABILITIES,
    IAM_STACK_NAME,
    IAM_TEMPLATE_PATH,
    IMAGE_ASSET_NAME,
    AccountSetupException,
    PublishedImage,
    SetupPlan,
    SetupRequest,
    SetupResult,
    SetupStep,
    bucket_candidates,
    creation_target,
    decide_bucket,
    needs_default_probe,
    plan_bucket_creation,
    plan_setup,
    plan_stack,
    progress_message,
    require_supported_region,
    stack_output,
    verify_checksum,
)
from rayito._account_setup._ports import AccountSetupGateways, Progress
from rayito._images import BUILD_ROLE_OUTPUT_KEY
from rayito._stacks._condition import StackObservation


def uploaded_by_either(base: bool | None, caps: bool | None) -> bool | None:
    """Si la publicación subió el artefacto, para la base o para la imagen con
    capacidades (en una cuenta de 0.16 sólo se publica la segunda); `None`
    si ninguna de las dos publicó."""
    if base is None and caps is None:
        return None
    return bool(base) or bool(caps)


class AccountSetup:
    """Prepara una cuenta y región para Rayito: observa, planifica y aplica.

    Coste y activación
    -------------------
    Activa: sólo `rayito setup` (privado: ningún `create()`, listado ni
        getter del SDK lo llama). Construirlo y construir sus adaptadores no
        llama a AWS ni carga ningún cliente opcional.
    Recursos y llamadas AWS: `plan` sólo lee (`GetCallerIdentity`,
        `DescribeStacks`, un `HeadBucket`, `GetMicrovmImage` y
        `ListMicrovmImageVersions`, de `rayito-base` y, sólo con
        `caps_image=True`, de `rayito-base-caps`); sólo en una cuenta sin
        bucket reutilizable
        lee además `DescribeStacks` de `rayito-m0-artifacts` y hasta tres
        `HeadBucket`. `apply` puede crear el bucket con la pila
        `rayito-m0-artifacts` (CloudFormation crea el bucket; el SDK no llama
        a `CreateBucket` ni a ninguna API de configuración de S3), crear o
        actualizar la pila `rayito-m0-iam`, subir el zip de la imagen y
        publicar `rayito-base` y, con `caps_image`, `rayito-base-caps` (ésta con
        `additionalOsCapabilities=["ALL"]`) desde ese mismo zip, descargado
        una sola vez.
    Coste aproximado: la pila de IAM es gratis; cada versión publicada de
        cada imagen ≈ $0,04 por semana de almacenamiento ($0,08 por GB-mes,
        mínimo una semana, `cost.md`); el zip en el bucket cuesta céntimos; el bucket nuevo, sin
        coste en reposo más allá de esos zips, que caducan a los 7 días.
    IAM: las acciones de `infra/iam.yaml` (CloudFormation con
        `CAPABILITY_NAMED_IAM`, S3 del artefacto y `CreateMicrovmImage`/
        `UpdateMicrovmImage`) en las credenciales del llamante; para crear el
        bucket, además CloudFormation sobre `rayito-m0-artifacts` y los
        permisos de S3 que la pila ejerce con esas credenciales
        (`s3:CreateBucket`, `s3:PutBucketPolicy` y las de configuración),
        propios de un rol de bootstrap y no del rol mínimo del SDK.
    Cómo apagarla: no ejecutes `rayito setup`; con `--dry-run` sólo lee;
        sin `--caps-image` (`SetupRequest.caps_image`, por defecto `False`) no
        publica `rayito-base-caps`.
    Ejemplo:
        setup = AccountSetup(gateways)
        plan = setup.plan(request)
        result = setup.apply(plan, workdir=Path("tmp"), on_progress=print)
    """

    def __init__(self, gateways: AccountSetupGateways) -> None:
        self._gateways = gateways

    def plan(self, request: SetupRequest) -> SetupPlan:
        require_supported_region(request.region)
        account = self._gateways.buckets.account_id()
        stack = self._gateways.iam_stack.observe()
        bucket_step = self._resolve_bucket(request, account, stack)
        image = self._gateways.images.observe(BASE_IMAGE_NAME)
        caps = self._gateways.images.observe(CAPS_IMAGE_NAME) if request.caps_image else None
        return plan_setup(request, account, stack, bucket_step, image, caps)

    def apply(self, plan: SetupPlan, *, workdir: Path, on_progress: Progress) -> SetupResult:
        bucket = self._apply_bucket(plan, on_progress)
        observed = self._apply_stack(plan, bucket, on_progress)
        publishing = _Publishing(self._gateways, plan, bucket, observed, workdir, on_progress)
        base = publishing.image(plan.image_step, plan.image_arn, None)
        caps = publishing.image(plan.caps_step, plan.caps_image_arn, CAPS_IMAGE_OS_CAPABILITIES)
        return SetupResult(
            bucket=bucket,
            image_arn=base.image_arn or plan.image_arn,
            image_version=str(base.image_version),
            artifact_uploaded=uploaded_by_either(base.artifact_uploaded, caps.artifact_uploaded),
            caps_image_arn=caps.image_arn,
            caps_image_version=caps.image_version,
        )

    def _resolve_bucket(
        self, request: SetupRequest, account: str, stack: StackObservation
    ) -> SetupStep:
        """Prueba cada candidato reutilizable hasta que uno decide; si ninguno,
        la fase de creación mira la pila del bucket y, sólo con el nombre por
        defecto y sin pila, sonda ese nombre."""
        buckets = self._gateways.buckets
        for candidate in bucket_candidates(request, stack, account):
            step = decide_bucket(candidate, buckets.probe(candidate.name), request.region)
            if step is not None:
                return step
        target = creation_target(request, account)
        artifact_stack = self._gateways.artifact_stack.observe()
        probe = buckets.probe(target.name) if needs_default_probe(target, artifact_stack) else None
        return plan_bucket_creation(target, artifact_stack, probe, request.region)

    def _apply_bucket(self, plan: SetupPlan, on_progress: Progress) -> str:
        """Crea o espera la pila del bucket y devuelve el nombre de su salida;
        con `reuse` no toca nada."""
        step = plan.steps[0]
        gateway = self._gateways.artifact_stack
        if step.action == "create":
            on_progress(
                progress_message("bucketCreate", bucket=step.name, name=ARTIFACT_STACK_NAME)
            )
            observation = gateway.create(step.name)
        elif step.action == "wait":
            on_progress(
                progress_message("bucketWait", name=ARTIFACT_STACK_NAME, status=str(step.status))
            )
            observation = gateway.wait()
        else:
            return plan.bucket
        bucket = stack_output(observation, BUCKET_NAME_OUTPUT_KEY, ARTIFACT_TEMPLATE_PATH)
        on_progress(progress_message("bucketReady", bucket=bucket, name=ARTIFACT_STACK_NAME))
        return bucket

    def _apply_stack(
        self, plan: SetupPlan, bucket: str, on_progress: Progress
    ) -> StackObservation | None:
        """Crea, actualiza o espera la pila; `None` si ya estaba lista."""
        step = plan.stack_step
        gateway = self._gateways.iam_stack
        if step.action == "create":
            on_progress(progress_message("stackCreate", name=IAM_STACK_NAME))
            observation = gateway.create(bucket)
        elif step.action == "update":
            on_progress(progress_message("stackUpdate", name=IAM_STACK_NAME))
            observation = gateway.update_bucket(bucket)
        elif step.action == "wait":
            on_progress(progress_message("stackWait", name=IAM_STACK_NAME, status=str(step.status)))
            observation = gateway.wait()
            if plan_stack(observation, bucket).action == "update":
                # la pila que se esperaba trabajaba con otro bucket: se actualiza
                on_progress(progress_message("stackUpdate", name=IAM_STACK_NAME))
                observation = gateway.update_bucket(bucket)
        else:
            return None
        stack_output(observation, BUILD_ROLE_OUTPUT_KEY, IAM_TEMPLATE_PATH)
        on_progress(progress_message("stackReady", name=IAM_STACK_NAME))
        return observation


@dataclass(frozen=True)
class _ImageOutcome:
    image_arn: str | None
    image_version: str | None
    artifact_uploaded: bool | None


class _Publishing:
    """Los dos pasos de imagen de un `apply`: espera, reutiliza o publica
    cada uno. El zip (el de `--artifact` o el de la release, descargado y
    verificado una sola vez) y la pila de IAM se piden la primera vez que una
    publicación los necesita y se reutilizan en la segunda."""

    def __init__(
        self,
        gateways: AccountSetupGateways,
        plan: SetupPlan,
        bucket: str,
        observed: StackObservation | None,
        workdir: Path,
        on_progress: Progress,
    ) -> None:
        self._gateways = gateways
        self._plan = plan
        self._bucket = bucket
        self._stack = observed
        self._workdir = workdir
        self._on_progress = on_progress
        self._artifact: Path | None = plan.artifact

    def image(
        self, step: SetupStep, image_arn: str | None, os_capabilities: str | None
    ) -> _ImageOutcome:
        if step.action == "skip" or image_arn is None:
            return _ImageOutcome(None, None, None)
        if step.action == "wait" and step.image_version is not None:
            self._wait(image_arn, step.name, step.image_version)
        elif step.action == "publish":
            published = self._publish(step.name, os_capabilities)
            self._report_ready(step.name, published.image_version)
            return _ImageOutcome(
                published.image_arn, published.image_version, published.artifact_uploaded
            )
        return _ImageOutcome(image_arn, step.image_version, None)

    def _wait(self, image_arn: str, name: str, image_version: str) -> None:
        self._on_progress(progress_message("imageWait", name=name, imageVersion=image_version))
        gate = self._gateways.images.wait(image_arn, image_version, self._on_progress)
        if not gate.launchable:
            raise AccountSetupException(
                "image_build_failed",
                name=name,
                imageVersion=image_version,
                state=gate.version_state,
            )
        self._report_ready(name, image_version)

    def _publish(self, name: str, os_capabilities: str | None) -> PublishedImage:
        if self._stack is None:
            self._stack = self._gateways.iam_stack.observe()
        build_role_arn = stack_output(self._stack, BUILD_ROLE_OUTPUT_KEY, IAM_TEMPLATE_PATH)
        if self._artifact is None:
            self._artifact = self._download_release()
        return self._gateways.publisher.publish(
            self._artifact,
            name=name,
            os_capabilities=os_capabilities,
            bucket=self._bucket,
            build_role_arn=build_role_arn,
            on_progress=self._on_progress,
        )

    def _download_release(self) -> Path:
        tag = next((step.release for step in self._plan.steps if step.release is not None), None)
        if tag is None:
            raise ValueError("un paso de publicación sin artefacto lleva la release")
        release = self._gateways.releases.fetch(tag, self._workdir, self._on_progress)
        verify_checksum(release, tag)
        self._on_progress(
            progress_message("verified", asset=IMAGE_ASSET_NAME, sha256=release.artifact_sha256)
        )
        return release.artifact

    def _report_ready(self, name: str, image_version: str) -> None:
        self._on_progress(progress_message("imageReady", name=name, imageVersion=image_version))

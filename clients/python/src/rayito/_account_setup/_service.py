"""Servicio de aplicación de `rayito setup` (ADR-030): `plan` observa la
cuenta con lecturas y decide con el dominio puro; `apply` ejecuta ese plan
en el orden del diseño D6. Sólo habla con puertos."""

from __future__ import annotations

from pathlib import Path

from rayito._account_setup._domain import (
    ARTIFACT_STACK_NAME,
    ARTIFACT_TEMPLATE_PATH,
    BASE_IMAGE_NAME,
    BUCKET_NAME_OUTPUT_KEY,
    IAM_STACK_NAME,
    IAM_TEMPLATE_PATH,
    IMAGE_ASSET_NAME,
    AccountSetupException,
    PublishedImage,
    SetupPlan,
    SetupRequest,
    SetupResult,
    SetupStep,
    StackObservation,
    bucket_candidates,
    creation_target,
    decide_bucket,
    needs_default_probe,
    plan_bucket_creation,
    plan_setup,
    progress_message,
    require_supported_region,
    stack_output,
    verify_checksum,
)
from rayito._account_setup._ports import AccountSetupGateways, Progress
from rayito._images import BUILD_ROLE_OUTPUT_KEY


class AccountSetup:
    """Prepara una cuenta y región para Rayito: observa, planifica y aplica.

    Coste y activación
    -------------------
    Activa: sólo `rayito setup` (privado: ningún `create()`, listado ni
        getter del SDK lo llama). Construirlo y construir sus adaptadores no
        llama a AWS ni carga ningún cliente opcional.
    Recursos y llamadas AWS: `plan` sólo lee (`GetCallerIdentity`,
        `DescribeStacks`, un `HeadBucket`, `GetMicrovmImage` y
        `ListMicrovmImageVersions`); sólo en una cuenta sin bucket reutilizable
        lee además `DescribeStacks` de `rayito-m0-artifacts` y hasta tres
        `HeadBucket`. `apply` puede crear el bucket con la pila
        `rayito-m0-artifacts` (CloudFormation crea el bucket; el SDK no llama
        a `CreateBucket` ni a ninguna API de configuración de S3), crear o
        actualizar la pila `rayito-m0-iam`, subir el zip de la imagen y
        publicar `rayito-base`.
    Coste aproximado: la pila de IAM es gratis; cada versión publicada de
        la imagen ≈ $0,04 por semana de almacenamiento (mínimo una semana,
        `cost.md`); el zip en el bucket cuesta céntimos; el bucket nuevo, sin
        coste en reposo más allá de esos zips, que caducan a los 7 días.
    IAM: las acciones de `infra/iam.yaml` (CloudFormation con
        `CAPABILITY_NAMED_IAM`, S3 del artefacto y `CreateMicrovmImage`/
        `UpdateMicrovmImage`) en las credenciales del llamante; para crear el
        bucket, además CloudFormation sobre `rayito-m0-artifacts` y los
        permisos de S3 que la pila ejerce con esas credenciales
        (`s3:CreateBucket`, `s3:PutBucketPolicy` y las de configuración),
        propios de un rol de bootstrap y no del rol mínimo del SDK.
    Cómo apagarla: no ejecutes `rayito setup`; con `--dry-run` sólo lee.
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
        return plan_setup(request, account, stack, bucket_step, image)

    def apply(self, plan: SetupPlan, *, workdir: Path, on_progress: Progress) -> SetupResult:
        bucket = self._apply_bucket(plan, on_progress)
        observed = self._apply_stack(plan, bucket, on_progress)
        step = plan.image_step
        image_arn = plan.image_arn
        image_version = step.image_version
        artifact_uploaded: bool | None = None
        if step.action == "wait" and image_version is not None:
            self._wait_for_image(plan, image_version, on_progress)
        elif step.action == "publish":
            stack = observed or self._gateways.iam_stack.observe()
            published = self._publish_image(
                plan,
                bucket,
                stack_output(stack, BUILD_ROLE_OUTPUT_KEY, IAM_TEMPLATE_PATH),
                workdir=workdir,
                on_progress=on_progress,
            )
            image_arn = published.image_arn
            image_version = published.image_version
            artifact_uploaded = published.artifact_uploaded
            self._report_ready(image_version, on_progress)
        return SetupResult(
            bucket=bucket,
            image_arn=image_arn,
            image_version=str(image_version),
            artifact_uploaded=artifact_uploaded,
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
        else:
            return None
        stack_output(observation, BUILD_ROLE_OUTPUT_KEY, IAM_TEMPLATE_PATH)
        on_progress(progress_message("stackReady", name=IAM_STACK_NAME))
        return observation

    def _wait_for_image(self, plan: SetupPlan, image_version: str, on_progress: Progress) -> None:
        on_progress(progress_message("imageWait", name=BASE_IMAGE_NAME, imageVersion=image_version))
        gate = self._gateways.images.wait(plan.image_arn, image_version, on_progress)
        if not gate.launchable:
            raise AccountSetupException(
                "image_build_failed",
                name=BASE_IMAGE_NAME,
                imageVersion=image_version,
                state=gate.version_state,
            )
        self._report_ready(image_version, on_progress)

    def _publish_image(
        self,
        plan: SetupPlan,
        bucket: str,
        build_role_arn: str,
        *,
        workdir: Path,
        on_progress: Progress,
    ) -> PublishedImage:
        artifact = plan.artifact
        if artifact is None:
            artifact = self._download_release(plan, workdir, on_progress)
        return self._gateways.publisher.publish(
            artifact, bucket=bucket, build_role_arn=build_role_arn, on_progress=on_progress
        )

    def _download_release(self, plan: SetupPlan, workdir: Path, on_progress: Progress) -> Path:
        tag = plan.image_step.release
        if tag is None:
            raise ValueError("un paso de publicación sin artefacto lleva la release")
        release = self._gateways.releases.fetch(tag, workdir, on_progress)
        verify_checksum(release, tag)
        on_progress(
            progress_message("verified", asset=IMAGE_ASSET_NAME, sha256=release.artifact_sha256)
        )
        return release.artifact

    @staticmethod
    def _report_ready(image_version: str, on_progress: Progress) -> None:
        on_progress(
            progress_message("imageReady", name=BASE_IMAGE_NAME, imageVersion=image_version)
        )

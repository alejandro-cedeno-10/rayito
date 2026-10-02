"""Templates declarativos (m15-templates, ADR-022): DSL (`Template`,
`AsyncTemplate`) -> Dockerfile compuesto sobre una imagen `rayito-base` ya
publicada -> zip determinista en S3 -> `create`/`update-microvm-image`.
Apagado por defecto (ADR-014): nada de este paquete se importa ni crea un
cliente `boto3` hasta que el código llama a `Template.build()` o a uno de
sus `classmethod` hermanos.

Coste y activación
-------------------
Activa: `Template.build()`/`build_in_background()` (o sus variantes
    `AsyncTemplate`). `Template()` (el builder) y `to_dockerfile()`/
    `to_json()` son puros, no llaman a AWS.
Recursos y llamadas AWS: `lambda-microvms:GetMicrovmImageVersion` (para leer
    el `codeArtifact` de la imagen base y, al terminar, para sondear el
    build), `CreateMicrovmImage`/`UpdateMicrovmImage`, `GetMicrovmImage`,
    `ListMicrovmImageVersions`; `s3:GetObject` (zip base), `HeadObject`/
    `PutObject` (subir el artefacto nuevo, sólo si no existe ya por hash);
    `logs:DescribeLogStreams`/`GetLogEvents` sólo si el build termina en un
    estado que no es `SUCCESSFUL`+`ACTIVE` (para explicar el fallo). Ver
    `AWS_API_NOTES.md` §27.
Coste aproximado: cada versión de imagen nueva cuesta almacenamiento de
    snapshot (~$0,04/semana por versión, investigación §3.4,
    `docs/research/2026-10-e2b-out-of-scope.md`); el build en sí no se
    factura aparte (sin CodeBuild en 0.6, opción A de la investigación).
IAM: `RayitoTemplateBuilder` (`infra/templates.yaml`) sobre quien llama a
    `Template.build()`: `lambda:CreateMicrovmImage`/`UpdateMicrovmImage`/
    `GetMicrovmImage*`/`ListMicrovmImageVersions`, `iam:PassRole` sobre el
    rol de build (el mismo que usa `rayito image publish`), `s3:PutObject`/
    `GetObject` en el bucket de artefactos (`HeadObject` lo autoriza
    `GetObject`), y lectura del grupo de logs de la imagen; nunca crear o
    actualizar las imágenes base publicadas.
Cómo apagarla: no llames a `Template.build()`. Las versiones de imagen ya
    construidas se borran con `rayito image` (no las borra `Template`:
    `rayito-base` y sus templates comparten el mismo espacio de imágenes).
Ejemplo:
    from rayito import Template, wait_for_port
    t = (
        Template()
        .from_base_image("rayito-base")
        .pip_install(["pandas==2.2.3"])
        .set_start_cmd("python -m http.server 8000", wait_for_port(8000))
    )
    info = Template.build(t, "mi-template", bucket="<bucket-de-artefactos>")
    sbx = Sandbox.create(info.template_id)

Divergencias con E2B (ADR-022, `docs/site/docs/funciones-opcionales/templates.md`):
sin caché de capas entre builds (`skip_cache()` equivale a `force=True`);
sólo ARM64; `from_image`/`from_template`/`from_dockerfile` fuera de
alcance en 0.6 (sólo `from_base_image()` compone de verdad: inyectar
`rayd` y sus hooks en una imagen externa no tiene aún un camino soportado);
sin streaming en vivo de los pasos (los logs se leen al terminar el build,
investigación TPL-1); `apt_install`/`from_gcp_registry` no tienen análogo
en Amazon Linux 2023 / AWS y lanzan `UnimplementedError`.
"""

from __future__ import annotations

from rayito._templates._dsl import AsyncTemplate, Template
from rayito._templates._instructions import (
    BaseImageRef,
    CopyStep,
    EnvStep,
    ReadyPoll,
    RunStep,
    StartSpec,
    TemplateSpec,
    UserStep,
    WorkdirStep,
)
from rayito._templates._models import BuildHandle, BuildInfo, BuildStatus
from rayito._templates._ready_cmds import (
    ReadyCommand,
    wait_for_file,
    wait_for_port,
    wait_for_process,
    wait_for_url,
)

__all__ = [
    "AsyncTemplate",
    "BaseImageRef",
    "BuildHandle",
    "BuildInfo",
    "BuildStatus",
    "CopyStep",
    "EnvStep",
    "ReadyCommand",
    "ReadyPoll",
    "RunStep",
    "StartSpec",
    "Template",
    "TemplateSpec",
    "UserStep",
    "WorkdirStep",
    "wait_for_file",
    "wait_for_port",
    "wait_for_process",
    "wait_for_url",
]

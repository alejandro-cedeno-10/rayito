"""Constructos de AWS CDK de Rayito (`agentcore-cdk`, ADR-032), detrás del
extra `rayito[cdk]` (`aws-cdk-lib>=2.261.0,<3`, `constructs>=10.5.0,<11`;
sintetizar en Python necesita Node, por jsii).

- `RayitoAgent`: un agente de Rayito en Amazon Bedrock AgentCore Runtime,
  con los mismos recursos e IAM que `rayito agent deploy --target agentcore`.
  La imagen la construye CodeBuild (`AgentImageBuild.code_build()`, por
  defecto) o el Docker local (`AgentImageBuild.local_docker()`).
- `RayitoMicrovmImage`: una imagen de Lambda MicroVMs con `rayd`, con la
  misma configuración que `rayito image publish`, a partir de un
  `MicrovmImageArtifact` (`from_file` o `download_release`).

Importar `rayito` (o la CLI, o el servidor MCP) nunca importa este módulo ni
`aws_cdk`. Sin el extra, importarlo lanza `InvalidArgumentException` con la
instrucción `pip install rayito[cdk]`.

Coste y activación
-------------------
Activa: sólo `cdk deploy` de un stack con alguno de los constructos;
    importar el módulo y sintetizar no llaman a AWS ni cuestan nada.
Recursos y llamadas AWS: los de `RayitoAgent` y `RayitoMicrovmImage`.
Coste aproximado: el de cada constructo (CodeBuild, ECR, logs, Lambda y Step
    Functions por despliegue, AgentCore por uso, builds y snapshots de las
    imágenes de MicroVM).
IAM: la de `cdk deploy` (el bootstrap de CDK).
Cómo apagarla: no instalar el extra o `cdk destroy`.
Ejemplo:
    from rayito.cdk import RayitoAgent
    RayitoAgent(stack, "Agent", project="./agent")
"""

from __future__ import annotations

from rayito._optional import require_module

require_module("aws_cdk", extra="cdk", feature="rayito.cdk")

from rayito.cdk._agent import AgentImageBuild, RayitoAgent  # noqa: E402
from rayito.cdk._errors import RayitoCdkException  # noqa: E402
from rayito.cdk._microvm_image import MicrovmImageArtifact, RayitoMicrovmImage  # noqa: E402

__all__ = [
    "AgentImageBuild",
    "MicrovmImageArtifact",
    "RayitoAgent",
    "RayitoCdkException",
    "RayitoMicrovmImage",
]

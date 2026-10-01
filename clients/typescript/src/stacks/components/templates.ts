/**
 * Componente `templates` (m15-templates): la política `RayitoTemplateBuilder`
 * de `infra/templates.yaml`, para quien llama a `Template.build()`. No crea
 * ningún bucket, imagen ni función Lambda ($0 en reposo: sólo IAM).
 */

import type { StackComponent } from "../model.js";

export const COMPONENT: StackComponent = {
  name: "templates",
  description:
    "Política RayitoTemplateBuilder (CreateMicrovmImage/UpdateMicrovmImage/GetMicrovmImage*/" +
    "ListMicrovmImageVersions, PassRole sobre el rol de build, S3 del artefacto y lectura de " +
    "logs de build), para Template.build().",
  parameters: [
    {
      name: "ArtifactBucketArn",
      description:
        "ARN del bucket S3 al que Template.build() sube el zip de contexto (bucket de " +
        "Template.build()); se crea y se gestiona fuera de esta pila.",
      required: true,
    },
    {
      name: "BuildRoleArn",
      description:
        "ARN del rol de IAM que Template.build() pasa como buildRoleArn (la salida " +
        "BuildRoleArn de infra/iam.yaml).",
      required: true,
    },
    {
      name: "ImageLogGroupPrefix",
      description:
        "Prefijo de los grupos de logs de imagen que Template.build() relee si un build falla " +
        "(/rayito/<nombre>).",
      default: "/rayito",
    },
    {
      name: "BaseImageBucketArn",
      description:
        "ARN del bucket que ya tiene el zip de codeArtifact de rayito-base (fromBaseImage()); " +
        "vacío concede s3:GetObject sobre cualquier bucket, porque la imagen base puede " +
        "haberse publicado desde un bucket que esta pila no conoce.",
    },
  ],
  capabilities: ["CAPABILITY_IAM"],
  cost: {
    creates: ["AWS::IAM::ManagedPolicy"],
    idleMonthly: "$0 (sólo IAM)",
    perUse: ["almacenamiento de snapshot por versión de imagen nueva, no de esta pila"],
    removal: "destroy() borra la política; no borra ninguna versión de imagen ni objeto S3",
    source: "AWS_API_NOTES.md §27",
  },
};

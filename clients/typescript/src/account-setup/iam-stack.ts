/**
 * El componente `iam` (ADR-030, D3): `infra/iam.yaml` empaquetado en el SDK.
 * Lo despliega `rayito setup` a través de `OptionalStacks` con el nombre
 * histórico `rayito-m0-iam`; no está en el catálogo de `rayito stack`, que
 * lista sólo funciones opcionales con coste. Los parámetros son exactamente los
 * de la plantilla: sólo `ArtifactBucket` es obligatorio y los demás no llevan
 * valor aquí, así que la plantilla conserva el suyo al crear y la pila el
 * actual al actualizar.
 */

import type { StackComponent } from "../stacks/model.js";
import {
  ARTIFACT_BUCKET_PARAMETER,
  IAM_STACK_CAPABILITIES,
  IAM_STACK_COMPONENT_NAME,
  LEGACY_CALLER_POLICY_PARAMETER,
  TEMPLATE_FINGERPRINT_PARAMETER,
} from "./domain.js";

export const IAM_STACK_COMPONENT: StackComponent = {
  name: IAM_STACK_COMPONENT_NAME,
  description:
    "Roles y políticas gestionadas de Rayito (build, ejecución y las políticas del " +
    "llamante) que necesita publicar imágenes y lanzar sandboxes.",
  parameters: [
    {
      name: ARTIFACT_BUCKET_PARAMETER,
      description: "Bucket S3 con los zips de las imágenes (en la región de las imágenes).",
      required: true,
    },
    { name: "LogGroupPrefix", description: "Prefijo explícito del grupo de logs de CloudWatch." },
    {
      name: "NetworkConnectorArns",
      description: "ARNs de conectores de red propios que el llamante puede pasar como egreso.",
    },
    {
      name: "PersistenceBucket",
      description: "Bucket donde rayd guarda /home/user (persist=); vacío, sin acceso a S3.",
    },
    {
      name: "PersistencePrefix",
      description: "Prefijo de clave bajo PersistenceBucket que el rol puede leer y escribir.",
    },
    {
      name: "TransferBucket",
      description: "Bucket de upload_url/download_url y ficheros grandes (transfer=S3Staging).",
    },
    { name: "TransferPrefix", description: "Prefijo de clave bajo TransferBucket." },
    {
      name: TEMPLATE_FINGERPRINT_PARAMETER,
      description: "sha256 de la plantilla que desplegó la pila (lo pone rayito setup).",
    },
    {
      name: LEGACY_CALLER_POLICY_PARAMETER,
      description:
        "true si la pila se creó con la plantilla de 0.6.1 o anterior (lo pone rayito setup).",
    },
  ],
  cost: {
    creates: ["AWS::IAM::Role (build y ejecución)", "AWS::IAM::ManagedPolicy (tres políticas)"],
    idleMonthly: "$0 (IAM no se factura)",
    removal: "destroy() borra la pila; no borra el bucket ni las imágenes",
    source: "configurar-aws.md, paso 3",
  },
  capabilities: IAM_STACK_CAPABILITIES,
};

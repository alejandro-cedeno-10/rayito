/**
 * El componente `artifact-bucket` (ADR-030, D4 y D10): `infra/artifact-bucket.yaml`
 * empaquetado en el SDK. Lo despliega `rayito setup` a través de `OptionalStacks`
 * como `rayito-m0-artifacts` sólo cuando no hay un bucket que reutilizar; no está
 * en el catálogo de `rayito stack`, por las mismas razones que el componente
 * `iam`. Los parámetros son exactamente los de la plantilla: `BucketName` es
 * obligatorio y `ArtifactExpirationDays` conserva el valor por defecto de la
 * plantilla. No pide `Capabilities`: la plantilla no crea nada de IAM.
 */

import type { StackComponent } from "../stacks/model.js";
import {
  ARTIFACT_COMPONENT_NAME,
  ARTIFACT_EXPIRATION_DAYS,
  BUCKET_NAME_PARAMETER,
} from "./domain.js";

export const ARTIFACT_STACK_COMPONENT: StackComponent = {
  name: ARTIFACT_COMPONENT_NAME,
  description:
    "Bucket S3 privado y cifrado para los zips de las imágenes de Rayito, con " +
    "caducidad de los objetos y sólo TLS.",
  parameters: [
    {
      name: BUCKET_NAME_PARAMETER,
      description: "Nombre del bucket, en la región de la pila.",
      required: true,
    },
    {
      name: "ArtifactExpirationDays",
      description: "Días tras los que se borra un objeto bajo rayito/images/.",
    },
  ],
  cost: {
    creates: ["AWS::S3::Bucket", "AWS::S3::BucketPolicy"],
    idleMonthly: `céntimos: los zips de la imagen, que caducan a los ${ARTIFACT_EXPIRATION_DAYS} días`,
    removal: "borrar la pila elimina la política y conserva el bucket (Retain)",
    source: "AWS_API_NOTES.md, Preparación de la cuenta",
  },
  capabilities: [],
};

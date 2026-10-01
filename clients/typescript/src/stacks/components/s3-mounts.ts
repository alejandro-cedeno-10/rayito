/**
 * Componente `s3-mounts` (`m15-s3-mounts`, ADR-017): la política IAM
 * `RayitoS3MountAccess` de `infra/s3-mounts.yaml`. Ver ese fichero para el
 * contrato exacto de parámetros y statements.
 */

import type { StackComponent } from "../model.js";

export const COMPONENT: StackComponent = {
  name: "s3-mounts",
  description:
    "Política IAM RayitoS3MountAccess (lectura, y escritura con ReadOnly=false) sobre " +
    "un bucket, para el execution role de un sandbox con mounts.",
  parameters: [
    {
      name: "BucketName",
      description: "El único bucket al que da acceso esta política (una pila por bucket).",
      required: true,
    },
    {
      name: "Prefixes",
      description: "Prefijos (coma-separados) permitidos en ListBucket; '*' por defecto.",
      default: "*",
    },
    {
      name: "ReadOnly",
      description: "'true' (por defecto) sólo lectura; 'false' añade PutObject/DeleteObject.",
      default: "true",
    },
  ],
  cost: {
    creates: ["AWS::IAM::ManagedPolicy"],
    idleMonthly: "$0 (sólo IAM)",
    removal: "destroy() borra la política; no borra ningún objeto ni el bucket",
    source: "AWS_API_NOTES.md §23",
  },
};

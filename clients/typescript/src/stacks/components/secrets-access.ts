/**
 * Componente `secrets-access` (M13a, adoptado por el convenio
 * `OptionalStack` en M15 foundations). Ver `infra/README.md`; este módulo
 * sólo describe la pila para `OptionalStacks`.
 */

import type { StackComponent } from "../model.js";

export const COMPONENT: StackComponent = {
  name: "secrets-access",
  description:
    "Políticas IAM RayitoSecretsReader/RayitoSecretsAdmin sobre un prefijo de Secrets " +
    "Manager, para secrets/SecretStore/SecretCache.",
  parameters: [
    {
      name: "SecretPrefix",
      description: "Prefijo de nombre en Secrets Manager; nunca vacío.",
      default: "rayito/",
    },
    {
      name: "KmsKeyArn",
      description: "ARN de una CMK propia; vacío usa la clave gestionada aws/secretsmanager.",
      default: "",
    },
  ],
  capabilities: ["CAPABILITY_IAM"],
  cost: {
    creates: ["AWS::IAM::ManagedPolicy (x2)"],
    idleMonthly: "$0 (sólo IAM; los secretos que crees facturan aparte)",
    removal:
      "destroy() borra las dos políticas; no borra ningún secreto " +
      "(usa SecretStore().destroy(...) antes)",
    source: "AWS_API_NOTES.md §19",
  },
};

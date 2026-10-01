/**
 * Componente `sizes-guard` (m15-sizes-catalog): la política IAM
 * `RayitoRunAllowedSizes` de `infra/sizes-guard.yaml`, que limita
 * `lambda:RunMicrovm` a los ARN de imagen que el operador liste
 * explícitamente (Q90). Sólo IAM: ningún recurso facturable.
 */

import type { StackComponent } from "../model.js";

export const COMPONENT: StackComponent = {
  name: "sizes-guard",
  description:
    "Política RayitoRunAllowedSizes: lambda:RunMicrovm sólo sobre los ARN de imagen " +
    "listados (el baseline más cada sufijo de --sizes que se quiera permitir).",
  parameters: [
    {
      name: "ImageArns",
      description: "ARN completos (CommaDelimitedList) de cada versión de imagen permitida.",
      required: true,
    },
  ],
  capabilities: ["CAPABILITY_IAM"],
  cost: {
    creates: ["AWS::IAM::ManagedPolicy"],
    idleMonthly: "$0 (sólo IAM)",
    removal: "destroy() borra la política; ninguna imagen se toca",
    source: "AWS_API_NOTES.md §24 (Q90)",
  },
};

/**
 * Componente `sizes-guard` (m15-sizes-catalog): la política IAM
 * `RayitoRunAllowedSizes` de `infra/sizes-guard.yaml`, que limita
 * `lambda:RunMicrovm` a los ARN de imagen que el operador liste
 * explícitamente (Q90) con un Deny sobre `NotResource: ImageArns` (no sólo
 * un Allow: el Allow por sí solo no restringe nada una vez que la
 * identidad ya tiene el `microvm-image:*` de la `CallerPolicy` estándar de
 * `infra/iam.yaml`). Sólo IAM: ningún recurso facturable.
 */

import type { StackComponent } from "../model.js";

export const COMPONENT: StackComponent = {
  name: "sizes-guard",
  description:
    "Política RayitoRunAllowedSizes: Deny de lambda:RunMicrovm fuera de los ARN de " +
    "imagen listados (el baseline más cada sufijo de --sizes que se quiera permitir), " +
    "efectivo sea cual sea el resto de permisos de la identidad.",
  parameters: [
    {
      name: "ImageArns",
      description:
        "ARN de imagen (CommaDelimitedList, sin versión) de cada imagen permitida, " +
        "exactamente como `rayito image list` imprime `imageArn` (Q90: RunMicrovm " +
        "autoriza contra el ARN sin versión; uno con versión haría que el Deny " +
        "denegara todos los lanzamientos de la identidad).",
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

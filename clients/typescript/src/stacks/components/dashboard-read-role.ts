/**
 * Componente `dashboard-read-role` (ADR-026, fase 3): espejo exacto de
 * `rayito._stacks.components.dashboard_read_role` (Python). El rol de sólo
 * lectura que una cuenta destino crea para la vista multicuenta de
 * `rayito dashboard --account`. Se despliega en la cuenta que quieres ver.
 */

import { DASHBOARD_ASSUME_ROLE_SESSION_SECONDS } from "../../limits.js";
import type { StackComponent } from "../model.js";

export const COMPONENT: StackComponent = {
  name: "dashboard-read-role",
  description:
    "Rol de sólo lectura de una cuenta destino para la vista multicuenta de " +
    "rayito dashboard --account. Se despliega en la cuenta que quieres ver.",
  parameters: [
    {
      name: "TrustedPrincipalArn",
      description:
        "ARN del rol o usuario IAM, en la cuenta que ejecuta el panel, que puede asumir " +
        "este rol (nunca la raíz de la cuenta).",
      required: true,
    },
    {
      name: "ExternalId",
      description:
        "Opcional: si lo pones, el rol sólo se asume pasando este mismo valor " +
        "(rayito dashboard --external-id).",
      default: "",
      noEcho: true,
    },
    {
      name: "MaxSessionSeconds",
      description:
        "Sesión más larga que se puede pedir (IAM admite de 1 a 12 horas); el panel pide " +
        `${DASHBOARD_ASSUME_ROLE_SESSION_SECONDS} s.`,
      default: String(DASHBOARD_ASSUME_ROLE_SESSION_SECONDS),
    },
  ],
  capabilities: ["CAPABILITY_IAM"],
  cost: {
    creates: ["AWS::IAM::Role (1, con una política en línea de sólo lectura)"],
    idleMonthly: "$0: IAM no factura",
    perUse: [
      "Cada sesión del panel es un sts:AssumeRole (sin coste) y las lecturas son las " +
        "mismas de rayito dashboard: ListMicrovms, ListMicrovmImages, " +
        "Get/ListMicrovmImageVersion y ListServiceQuotas",
    ],
    removal: "destroy() borra el rol; los accesos desde el panel dejan de funcionar al instante",
    source: "IAM no tiene precio propio (https://aws.amazon.com/iam/pricing/, 2026-10-13)",
  },
};

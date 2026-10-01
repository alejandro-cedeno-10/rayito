/**
 * Componente `metadata-index` (M14, adoptado por el convenio
 * `OptionalStack` en M15 foundations). Ver `infra/README.md`; este módulo
 * sólo describe la pila para `OptionalStacks`.
 */

import type { StackComponent } from "../model.js";

export const COMPONENT: StackComponent = {
  name: "metadata-index",
  description:
    "Tabla DynamoDB (PAY_PER_REQUEST, TTL en expires_at) más las políticas " +
    "RayitoIndexWriter/RayitoIndexReader, para index: new DynamoDbIndex({...}).",
  parameters: [
    {
      name: "TableName",
      description: "Nombre de la tabla del índice (rayito-sandboxes por defecto).",
      default: "rayito-sandboxes",
    },
    {
      name: "PointInTimeRecovery",
      description: "Backups continuos (se facturan por GB-mes); 'false' por defecto.",
      default: "false",
    },
    {
      name: "DeletionProtection",
      description: "Bloquea DeleteTable (y destroy()) mientras sea 'true'; 'false' por defecto.",
      default: "false",
    },
  ],
  capabilities: ["CAPABILITY_IAM"],
  cost: {
    creates: ["AWS::DynamoDB::Table", "AWS::IAM::ManagedPolicy (x2)"],
    idleMonthly: "$0 (on-demand, tabla vacía; IAM es gratis)",
    perUse: [
      "~1 WRU por sandbox creado con index (US$0,625 por millón de WRU)",
      "~0,5 RRU por sandbox candidato al listar (US$0,125 por millón de RRU)",
      "US$0,25/GB-mes de almacenamiento tras los primeros 25 GB",
    ],
    removal:
      "destroy() borra la tabla (desactiva antes DeletionProtection si está en true) y las dos políticas",
    source: "AWS_API_NOTES.md §20; precios de DynamoDB us-east-1 consultados 2026-09-30",
  },
};

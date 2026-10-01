/**
 * Componente `events-webhooks` (M15, m15-events-webhooks): espejo exacto de
 * `rayito._stacks.components.events_webhooks` (Python). Ver ese módulo y
 * `infra/README.md` para el detalle completo.
 */

import type { StackComponent } from "../model.js";

export const COMPONENT: StackComponent = {
  name: "events-webhooks",
  description:
    "Secreto HMAC del stack, tabla de eventos/webhooks, forwarder/deliverer/" +
    "reconciliador y el scheduler, para events/LifecycleEvents.",
  parameters: [
    {
      name: "LogGroupName",
      description: 'Log group de la imagen (el mismo que logging: "cloudwatch" ya usa).',
      required: true,
    },
    {
      name: "ArtifactBucket",
      description: "Bucket S3 donde deploy({ artifactBucket }) subió el código.",
      required: true,
    },
    {
      name: "ReconcilerIntervalMinutes",
      description: "Frecuencia del reconciliador (rate), en minutos; 5 por defecto.",
      default: "5",
    },
    {
      name: "ReconcilerImageArn",
      description: "ARN de imagen a anotar en un evento killed sintetizado; informativo.",
      default: "",
    },
    {
      name: "ReconcilerImageVersion",
      description: "Versión de imagen a anotar en un evento killed sintetizado; informativo.",
      default: "",
    },
  ],
  artifacts: [{ name: "events-webhooks", parameterKey: "ArtifactS3Key" }],
  capabilities: ["CAPABILITY_IAM"],
  cost: {
    creates: [
      "AWS::SecretsManager::Secret",
      "AWS::DynamoDB::Table (streams habilitados)",
      "AWS::Lambda::Function (x3)",
      "AWS::Logs::SubscriptionFilter",
      "AWS::Scheduler::Schedule",
      "AWS::IAM::Role (x4) + AWS::IAM::ManagedPolicy",
    ],
    idleMonthly: "~$0,40/mes (el secreto; DynamoDB y Lambda son on-demand/por invocación)",
    perUse: [
      "Forwarder: 1 invocación Lambda por lote de líneas del log",
      "Deliverer: 1 invocación por lote del stream de DynamoDB",
      "Reconciliador: 1 invocación cada ReconcilerIntervalMinutes (~$0,0000002 c/u)",
      "~$1,25 por millón de eventos escritos (WRU) + lecturas de getEvents (RRU)",
    ],
    removal:
      "destroy() borra el secreto (force-delete: cualquier webhook ya registrado deja de " +
      "poder verificarse), la tabla, las tres Lambdas, la suscripción y el scheduler",
    source: "AWS_API_NOTES.md §25; precios de Lambda/DynamoDB/Scheduler us-east-1, 2026-09-30",
  },
};

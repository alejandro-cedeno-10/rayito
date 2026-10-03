/**
 * Componente `events-webhooks` (M15, m15-events-webhooks): espejo exacto de
 * `rayito._stacks.components.events_webhooks` (Python). Ver ese módulo y
 * `infra/README.md` para el detalle completo.
 */

import {
  DEFAULT_RECONCILER_INTERVAL_MINUTES,
  MIN_RECONCILER_INTERVAL_MINUTES,
} from "../../lifecycle-events/domain.js";
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
      description:
        "Frecuencia del reconciliador (rate), en minutos; " +
        `${DEFAULT_RECONCILER_INTERVAL_MINUTES} por defecto, mínimo ${MIN_RECONCILER_INTERVAL_MINUTES}.`,
      default: String(DEFAULT_RECONCILER_INTERVAL_MINUTES),
    },
  ],
  artifacts: [
    {
      name: "events-webhooks",
      parameterKey: "ArtifactS3Key",
      bucketParameterKey: "ArtifactBucket",
    },
  ],
  capabilities: ["CAPABILITY_IAM"],
  cost: {
    creates: [
      "AWS::SecretsManager::Secret",
      "AWS::DynamoDB::Table (streams habilitados)",
      "AWS::Lambda::Function (x3)",
      "AWS::Logs::SubscriptionFilter",
      "AWS::Scheduler::Schedule",
      "AWS::SQS::Queue (destino OnFailure del deliverer; vacía en condiciones normales)",
      "AWS::IAM::Role (x4) + AWS::IAM::ManagedPolicy",
    ],
    idleMonthly: "~$0,40/mes (el secreto; DynamoDB, Lambda y SQS son on-demand/por uso)",
    perUse: [
      "Forwarder: 1 invocación Lambda por lote de líneas del log",
      "Deliverer: 1 invocación por lote del stream de DynamoDB",
      "Reconciliador: 1 invocación cada ReconcilerIntervalMinutes (~$0,0000002 c/u)",
      "~$1,25 por millón de eventos escritos (WRU) + lecturas de getEvents (RRU)",
      "SQS: sólo factura cuando un lote entero del deliverer falla " +
        "(BisectBatchOnFunctionError ya aísla el registro problemático); " +
        "dentro de la capa gratuita al volumen de este stack",
    ],
    removal:
      "destroy() borra el secreto (force-delete: cualquier webhook ya registrado deja de " +
      "poder verificarse), la tabla, las tres Lambdas, la suscripción, la cola de fallos y " +
      "el scheduler",
    source: "AWS_API_NOTES.md §25; precios de Lambda/DynamoDB/Scheduler/SQS us-east-1, 2026-09-30",
  },
};

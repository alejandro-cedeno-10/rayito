/**
 * `rayito dashboard cloudwatch`: imprime el JSON de un dashboard de
 * CloudWatch (Logs Insights sobre `/rayito/*` y, si se piden, métricas OTLP y
 * el uso de la cuota). No llama a AWS ni necesita credenciales: sólo genera
 * texto, y generarlo es gratis; desplegarlo en CloudWatch cuesta (el
 * dashboard pasados los gratuitos y cada widget de Logs Insights por GB
 * escaneado). Para crearlo, `aws cloudwatch put-dashboard` con el fichero que
 * escribas o `rayito stack deploy dashboard-cloudwatch`, que despliega el
 * mismo dashboard (ADR-026, fase 3).
 *
 * Códigos de salida: 0; 2 por uso (sin región, prefijo o grupo inválido,
 * fichero de salida ilegible).
 */

import { writeFileSync } from "node:fs";
import { basename } from "node:path";
import {
  buildBoard,
  DEFAULT_LOG_GROUP_PREFIX,
  renderBoard,
} from "../../dashboard/cloudwatch-board.js";
import { InvalidArgumentError } from "../../errors.js";
import {
  DASHBOARD_IDLE_COST,
  LOGS_INSIGHTS_COST,
} from "../../stacks/components/dashboard-cloudwatch.js";
import { CUSTOM_METRIC_MONTHLY_COST } from "../../stacks/components/quota-alerts.js";
import { defineCommand, flag, option } from "../command.js";
import { UsageError } from "../failures.js";

const REGION_ENV_VAR = "AWS_REGION";
const NO_REGION_MESSAGE = `sin región: pasa --region o exporta ${REGION_ENV_VAR}`;
const COST_NOTE =
  "Generar el JSON es gratis y no llama a AWS. Desplegarlo en CloudWatch (put-dashboard o la " +
  `pila dashboard-cloudwatch) cuesta: ${DASHBOARD_IDLE_COST}. ${LOGS_INSIGHTS_COST} ` +
  "(precios de CloudWatch, us-east-1, consultados 2026-10-13).";
const QUOTA_COST_NOTE = `requiere la pila quota-alerts, que publica esa métrica (~${CUSTOM_METRIC_MONTHLY_COST} USD al mes)`;

export const cloudwatchCommand = defineCommand({
  path: ["dashboard", "cloudwatch"],
  help: `Imprime el JSON de un dashboard de CloudWatch con los logs de tus sandboxes.\n\n${COST_NOTE}`,
  parameters: [
    option(["--log-group"], {
      multiple: true,
      help:
        "Grupo de logs a consultar, repetible. Sin él, todos los que empiezan por " +
        "--log-group-prefix.",
    }),
    option(["--log-group-prefix"], {
      default: DEFAULT_LOG_GROUP_PREFIX,
      help: "Prefijo de los grupos de logs (con la barra final para no abarcar otros).",
    }),
    flag(["--otlp"], {
      help:
        "Añade CPU, memoria y disco de las métricas OTLP de rayd (telemetry=), con PromQL " +
        "(las consultas desde la consola no se facturan). Consultas PromQL verificadas en AWS real.",
    }),
    flag(["--quota"], {
      help:
        "Añade el uso de la cuota de memoria: grafica la métrica que publica la pila " +
        `quota-alerts, así que está vacía hasta que se despliegue; ${QUOTA_COST_NOTE}.`,
    }),
    option(["--output", "-o"], {
      type: "path",
      help: "Escribe el JSON en este fichero en vez de imprimirlo.",
    }),
  ],
  run: async (context) => {
    const { values } = context;
    const region = context.globals.region ?? (context.env[REGION_ENV_VAR] || undefined);
    if (region === undefined) {
      throw new UsageError(NO_REGION_MESSAGE);
    }
    let text: string;
    try {
      text = renderBoard(
        buildBoard({
          region,
          logGroupPrefix: values.string("log_group_prefix") ?? DEFAULT_LOG_GROUP_PREFIX,
          logGroups: values.strings("log_group"),
          includeOtlp: values.flag("otlp"),
          includeQuota: values.flag("quota"),
        }),
        true,
      );
    } catch (error) {
      if (error instanceof InvalidArgumentError) {
        throw new UsageError(error.message);
      }
      throw error;
    }
    const output = values.string("output");
    if (output === undefined) {
      context.console.echo(text);
      return undefined;
    }
    try {
      writeFileSync(output, `${text}\n`, "utf8");
    } catch {
      throw new UsageError("--output: no se pudo escribir el fichero");
    }
    context.console.echo(`Dashboard escrito en ${basename(output)}.`);
    return undefined;
  },
});

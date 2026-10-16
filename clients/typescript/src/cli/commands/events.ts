/**
 * `rayito events deploy|status|destroy|webhook add|webhook list|webhook
 * remove|list` (M15, m15-events-webhooks): la CLI de `LifecycleEvents`.
 * `rayito stack deploy events-webhooks` (genérico) también funciona; estas
 * órdenes son el atajo con los nombres de parámetro propios de la función.
 */

import {
  DEFAULT_GET_EVENTS_LIMIT,
  DEFAULT_RECONCILER_INTERVAL_MINUTES,
  DEFAULT_STACK_NAME,
  eventRecordType,
} from "../../lifecycle-events/domain.js";
import { COMPONENT } from "../../stacks/components/events-webhooks.js";
import type { StackStatus } from "../../stacks/model.js";
import {
  argument,
  type CommandContext,
  defineCommand,
  defineGroup,
  flag,
  option,
} from "../command.js";
import { EXIT_FAILURE } from "../exit-codes.js";
import { pairsOrUsage } from "./sandbox.js";
import { confirmDeploy, confirmDestroy } from "./stack.js";

const COMPONENT_LABEL = COMPONENT.name;
const DEFAULT_ORDER = "desc";

export const eventsGroup = defineGroup({
  path: ["events"],
  help: "Eventos de ciclo de vida y webhooks (events=).",
});

export const webhookGroup = defineGroup({
  path: ["events", "webhook"],
  help: "Webhooks registrados.",
});

const stackNameOption = option(["--stack-name"], {
  default: DEFAULT_STACK_NAME,
  help: "Nombre de la pila.",
});
const yesFlag = flag(["--yes"], { help: "No pedir confirmación." });

function stackNameOf(context: CommandContext): string {
  return context.values.string("stack_name") ?? DEFAULT_STACK_NAME;
}

async function eventsOf(context: CommandContext) {
  return (await context.services()).lifecycleEvents(stackNameOf(context));
}

function statusDocument(status: StackStatus): Record<string, unknown> {
  return { name: status.name, state: status.state, outputs: status.outputs };
}

export const deployCommand = defineCommand({
  path: ["events", "deploy"],
  help: "Despliega (o actualiza) la pila events-webhooks; imprime su coste.",
  parameters: [
    option(["--artifact-bucket"], { required: true, help: "Bucket del código de las Lambdas." }),
    option(["--log-group-name"], { required: true, help: "Grupo de logs de la imagen." }),
    option(["--reconciler-interval-minutes"], {
      type: "int",
      default: DEFAULT_RECONCILER_INTERVAL_MINUTES,
      help: "Cada cuántos minutos corre el reconciliador.",
    }),
    stackNameOption,
    option(["--tag"], {
      multiple: true,
      default: [],
      help:
        "Etiqueta K=V de la pila y de sus recursos; repetible (cuentas cuya política de " +
        "organización exige etiquetas al crear SQS/Lambda).",
    }),
    yesFlag,
  ],
  run: async (context) => {
    const { values } = context;
    const events = await eventsOf(context);
    const tags = pairsOrUsage(values.strings("tag"), "--tag");
    if (!(await confirmDeploy(context, COMPONENT, undefined))) {
      return EXIT_FAILURE;
    }
    const status = await events.deploy({
      artifactBucket: values.string("artifact_bucket") ?? "",
      logGroupName: values.string("log_group_name") ?? "",
      reconcilerIntervalMinutes: values.number("reconciler_interval_minutes"),
      tags,
    });
    if (context.globals.json) {
      context.console.emitJson(statusDocument(status));
      return undefined;
    }
    context.console.echo(`${COMPONENT_LABEL}: ${status.state}`);
    return undefined;
  },
});

export const statusCommand = defineCommand({
  path: ["events", "status"],
  help: "Estado de la pila events-webhooks (sólo lectura).",
  parameters: [stackNameOption],
  run: async (context) => {
    const status = await (await eventsOf(context)).status();
    if (context.globals.json) {
      context.console.emitJson(status === undefined ? null : statusDocument(status));
      return undefined;
    }
    context.console.echo(
      status === undefined
        ? `${COMPONENT_LABEL}: no desplegado`
        : `${COMPONENT_LABEL}: ${status.state}`,
    );
    return undefined;
  },
});

export const destroyCommand = defineCommand({
  path: ["events", "destroy"],
  help: "Borra la pila events-webhooks y sus recursos.",
  parameters: [stackNameOption, yesFlag],
  run: async (context) => {
    if (!(await confirmDestroy(context, COMPONENT))) {
      return EXIT_FAILURE;
    }
    await (await eventsOf(context)).destroy();
    if (context.globals.json) {
      context.console.emitJson({ name: COMPONENT_LABEL, state: "destroyed" });
      return undefined;
    }
    context.console.echo(`${COMPONENT_LABEL}: borrado`);
    return undefined;
  },
});

export const listCommand = defineCommand({
  path: ["events", "list"],
  help: "Eventos de ciclo de vida guardados (por defecto, los más recientes primero).",
  parameters: [
    option(["--sandbox-id"], { help: "Sólo los eventos de este sandbox." }),
    option(["--type"], {
      name: "event_type",
      multiple: true,
      default: [],
      help: "Tipo de evento; repetible.",
    }),
    option(["--limit"], {
      type: "int",
      default: DEFAULT_GET_EVENTS_LIMIT,
      help: "Máximo de eventos.",
    }),
    option(["--order"], { default: DEFAULT_ORDER, help: "asc o desc." }),
    stackNameOption,
  ],
  run: async (context) => {
    const { values } = context;
    const types = values.strings("event_type");
    const sandboxId = values.string("sandbox_id");
    const events = await (await eventsOf(context)).getEvents({
      ...(sandboxId === undefined ? {} : { sandboxId }),
      ...(types.length === 0 ? {} : { types }),
      limit: values.number("limit"),
      order: (values.string("order") ?? DEFAULT_ORDER) as "asc" | "desc",
    });
    if (context.globals.json) {
      context.console.emitJson(
        events.map((event) => ({
          eventId: event.eventId,
          sandboxId: event.sandboxId,
          type: eventRecordType(event),
          killReason: event.killReason ?? null,
          generation: event.generation,
          occurredAtMs: event.occurredAtMs,
        })),
      );
      return undefined;
    }
    context.console.table(
      ["event_id", "sandbox_id", "type", "occurred_at_ms"],
      events.map((event) => [
        event.eventId,
        event.sandboxId,
        eventRecordType(event),
        event.occurredAtMs,
      ]),
    );
    return undefined;
  },
});

export const webhookAddCommand = defineCommand({
  path: ["events", "webhook", "add"],
  help: "Registra un webhook firmado con el secreto HMAC indicado.",
  parameters: [
    argument("url", { required: true, help: "URL https que recibe los eventos." }),
    option(["--secret-name"], { required: true, help: "Secreto HMAC con el que se firma." }),
    option(["--type"], {
      name: "event_type",
      required: true,
      multiple: true,
      help: "Tipo de evento; repetible.",
    }),
    stackNameOption,
  ],
  run: async (context) => {
    const { values } = context;
    const webhook = await (await eventsOf(context)).registerWebhook(values.string("url") ?? "", {
      secretName: values.string("secret_name") ?? "",
      types: values.strings("event_type"),
    });
    if (context.globals.json) {
      context.console.emitJson({
        webhookId: webhook.webhookId,
        url: webhook.url,
        types: [...webhook.types],
      });
      return undefined;
    }
    context.console.echo(
      `webhook ${webhook.webhookId}: ${webhook.url} (${webhook.types.join(", ")})`,
    );
    return undefined;
  },
});

export const webhookListCommand = defineCommand({
  path: ["events", "webhook", "list"],
  help: "Webhooks registrados en la pila.",
  parameters: [stackNameOption],
  run: async (context) => {
    const webhooks = await (await eventsOf(context)).listWebhooks();
    if (context.globals.json) {
      context.console.emitJson(
        webhooks.map((webhook) => ({
          webhookId: webhook.webhookId,
          url: webhook.url,
          types: [...webhook.types],
        })),
      );
      return undefined;
    }
    context.console.table(
      ["webhook_id", "url", "types"],
      webhooks.map((webhook) => [webhook.webhookId, webhook.url, webhook.types.join(",")]),
    );
    return undefined;
  },
});

export const webhookRemoveCommand = defineCommand({
  path: ["events", "webhook", "remove"],
  help: "Da de baja un webhook por su id.",
  parameters: [
    argument("webhook_id", { required: true, help: "Id del webhook." }),
    stackNameOption,
  ],
  run: async (context) => {
    const webhookId = context.values.string("webhook_id") ?? "";
    await (await eventsOf(context)).deleteWebhook(webhookId);
    if (context.globals.json) {
      context.console.emitJson({ webhookId, state: "deleted" });
      return undefined;
    }
    context.console.echo(`webhook ${webhookId}: borrado`);
    return undefined;
  },
});

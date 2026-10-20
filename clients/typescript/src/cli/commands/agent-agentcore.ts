/**
 * `rayito agent list` y `rayito agent delete` (ADR-031): las dos órdenes que
 * sólo existen para `--target agentcore`. Una flota de MicroVMs sigue con
 * `rayito agent stop`. Los textos de ayuda y las opciones son los de
 * `testdata/agentcore-deploy/contract.json`; la lógica vive en `../agentcore.ts`.
 */

import { TARGET_AGENTCORE } from "../../agentcore/models.js";
import { AgentCoreError } from "../../errors.js";
import { guardedAgentCore, runAgentCoreDelete, runAgentCoreList, targetOf } from "../agentcore.js";
import {
  DELETE_HELP,
  DELETE_NAME_HELP,
  DELETE_YES_HELP,
  deleteTargetOption,
  LIST_HELP,
  listTargetOption,
} from "../agentcore-options.js";
import { argument, type CommandContext, defineCommand, flag } from "../command.js";
import { EXIT_USAGE } from "../exit-codes.js";

const TARGET_CHOICES_TEXT = "admitidas: microvm, agentcore";

/** `--target` de `list` y `delete`: sólo `agentcore`; `microvm` es `target_unsupported` y otro valor, un error de uso. */
function requireAgentCoreTarget(context: CommandContext, command: string): number | undefined {
  const target = targetOf(context);
  if (target === undefined) {
    context.console.echo(`rayito: --target: ${TARGET_CHOICES_TEXT}`, { err: true });
    return EXIT_USAGE;
  }
  if (target !== TARGET_AGENTCORE) {
    throw new AgentCoreError("target_unsupported", { command });
  }
  return undefined;
}

export const agentListCommand = defineCommand({
  path: ["agent", "list"],
  help: LIST_HELP,
  parameters: [listTargetOption],
  run: (context) =>
    guardedAgentCore(context, async () => {
      return requireAgentCoreTarget(context, "list") ?? (await runAgentCoreList(context));
    }),
});

export const agentDeleteCommand = defineCommand({
  path: ["agent", "delete"],
  help: DELETE_HELP,
  parameters: [
    argument("name", { required: true, help: DELETE_NAME_HELP }),
    deleteTargetOption,
    flag(["--yes"], { help: DELETE_YES_HELP }),
  ],
  run: (context) =>
    guardedAgentCore(context, async () => {
      return requireAgentCoreTarget(context, "delete") ?? (await runAgentCoreDelete(context));
    }),
});

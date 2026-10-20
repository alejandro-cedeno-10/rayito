/**
 * Todos los comandos y grupos de la CLI. Añadir un comando de una fase nueva
 * es importarlo aquí; el test de superficie y `check_sdk_parity.py --cli`
 * lo recogen solos.
 */

import type { CommandDefinition, GroupDefinition } from "./command.js";
import {
  agentBuildCommand,
  agentDeployCommand,
  agentDetectCommand,
  agentExportCommand,
  agentGroup,
  agentInitCommand,
  agentInvokeCommand,
  agentRunCommand,
  agentStatusCommand,
  agentStopCommand,
  agentTemplateBuildCommand,
  agentTemplateGroup,
} from "./commands/agent.js";
import { agentDeleteCommand, agentListCommand } from "./commands/agent-agentcore.js";
import { dashboardGroup } from "./commands/dashboard.js";
import { cloudwatchCommand } from "./commands/dashboard-cloudwatch.js";
import { doctorCommand } from "./commands/doctor.js";
import {
  deployCommand as domainDeploy,
  destroyCommand as domainDestroy,
  domainGroup,
  statusCommand as domainStatus,
} from "./commands/domain.js";
import {
  deployCommand as eventsDeploy,
  destroyCommand as eventsDestroy,
  eventsGroup,
  listCommand as eventsList,
  statusCommand as eventsStatus,
  webhookAddCommand,
  webhookGroup,
  webhookListCommand,
  webhookRemoveCommand,
} from "./commands/events.js";
import { imageGroup, listCommand as imageList, sizesCommand } from "./commands/image.js";
import { pruneCommand, publishCommand, zipCommand } from "./commands/image-tools.js";
import {
  createCommand,
  execCommand,
  infoCommand,
  killCommand,
  logsCommand,
  metricsCommand,
  sandboxGroup,
  listCommand as sandboxList,
} from "./commands/sandbox.js";
import { connectCommand, proxyCommand } from "./commands/sandbox-interactive.js";
import { setupCommand } from "./commands/setup.js";
import {
  deployCommand,
  destroyCommand,
  stackGroup,
  listCommand as stackList,
  statusCommand,
} from "./commands/stack.js";
import {
  buildCommand as templateBuild,
  templateGroup,
  logsCommand as templateLogs,
  statusCommand as templateStatus,
} from "./commands/template.js";
import type { CommandTree } from "./parse.js";

export const GROUPS: readonly GroupDefinition[] = [
  sandboxGroup,
  stackGroup,
  imageGroup,
  templateGroup,
  agentGroup,
  agentTemplateGroup,
  eventsGroup,
  webhookGroup,
  domainGroup,
  dashboardGroup,
];

export const COMMANDS: readonly CommandDefinition[] = [
  sandboxList,
  infoCommand,
  killCommand,
  logsCommand,
  metricsCommand,
  createCommand,
  execCommand,
  connectCommand,
  proxyCommand,
  stackList,
  statusCommand,
  deployCommand,
  destroyCommand,
  imageList,
  sizesCommand,
  publishCommand,
  pruneCommand,
  zipCommand,
  doctorCommand,
  setupCommand,
  templateBuild,
  templateStatus,
  templateLogs,
  agentTemplateBuildCommand,
  agentInitCommand,
  agentDetectCommand,
  agentBuildCommand,
  agentRunCommand,
  agentDeployCommand,
  agentExportCommand,
  agentInvokeCommand,
  agentStatusCommand,
  agentStopCommand,
  agentListCommand,
  agentDeleteCommand,
  eventsDeploy,
  eventsStatus,
  eventsDestroy,
  eventsList,
  webhookAddCommand,
  webhookListCommand,
  webhookRemoveCommand,
  domainDeploy,
  domainStatus,
  domainDestroy,
  cloudwatchCommand,
];

export const COMMAND_TREE: CommandTree = { groups: GROUPS, commands: COMMANDS };

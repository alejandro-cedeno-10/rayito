/**
 * Cada constante, texto y nombre de `--target agentcore` contra
 * `testdata/agentcore-deploy/contract.json`, que Python comparte: si el código
 * y el contrato discrepan, gana el contrato.
 */

import { describe, expect, it } from "vitest";
import {
  AGENT_BASE_IMAGE_ARG,
  AWS_CREDENTIALS_REQUIREMENTS_NAME,
  AWS_CREDENTIALS_REQUIREMENTS_PATH,
  AWS_CREDENTIALS_VENV_DIR,
  AWS_ENV_CREDENTIALS_SCRIPT_NAME,
  AWS_ENV_CREDENTIALS_SCRIPT_PATH,
  HOST_AWS_ENV_CREDENTIALS_KEY,
} from "../../src/agent/export.js";
import {
  ANY_REGION_FOUNDATION_MODEL_ARN_TEMPLATE,
  AWS_PARTITION,
  GLOBAL_FOUNDATION_MODEL_ARN_TEMPLATE,
  INFERENCE_PROFILE_ARN_TEMPLATE,
  REGIONAL_FOUNDATION_MODEL_ARN_TEMPLATE,
  SECRET_ARN_TEMPLATE,
} from "../../src/agentcore/arns.js";
import {
  BUILD_BASE_IMAGE_VARIABLE,
  BUILD_IMAGE_TAG_VARIABLE,
} from "../../src/agentcore/aws-build.js";
import {
  BUILD_SUCCEEDED_STATE,
  RUNTIME_BLOCKED_STATES,
  RUNTIME_IN_PROGRESS_STATES,
  RUNTIME_READY_STATE,
  RUNTIME_REPAIRABLE_STATE,
} from "../../src/agentcore/decisions.js";
import { RUNTIME_REGION_VARIABLES } from "../../src/agentcore/domain.js";
import { SESSION_ID_PATTERN } from "../../src/agentcore/identifiers.js";
import {
  AGENTCORE_ERROR_CODES,
  COST_MESSAGES,
  DELETE_PROGRESS_MESSAGES,
  ERROR_EXIT_CODES,
  ERROR_MESSAGES,
  PROGRESS_MESSAGES,
  SECURITY_MESSAGES,
  WARNING_MESSAGES,
} from "../../src/agentcore/messages.js";
import {
  AGENT_NAME_MAX_LENGTH,
  AGENT_NAME_PATTERN,
  buildProjectNameFor,
  repositoryNameFor,
  runtimeDescriptionFor,
  runtimeLogGroupFor,
  runtimeNameFor,
  sourceKeyFor,
  stackNameFor,
} from "../../src/agentcore/names.js";
import {
  AGENTCORE_STACK_CAPABILITIES,
  AGENTCORE_STACK_COMPONENT,
  STACK_OUTPUTS,
  STACK_PARAMETERS,
} from "../../src/agentcore/stack.js";
import * as cliMessages from "../../src/cli/agentcore-messages.js";
import { AgentCoreError } from "../../src/errors.js";
import {
  AGENT_AWS_CREDENTIALS_REQUIREMENTS_SHA256,
  AGENTCORE_BASE_IMAGE_MIRROR,
  AGENTCORE_ENV_CREDENTIAL_ADAPTERS,
} from "../../src/limits.js";
import { TEMPLATE_BODY } from "../../src/stacks/templates/agentcore-runtime.gen.js";
import { contract } from "./agentcore-vectors.js";

const { constants, messages } = contract;
const AGENT = "support-bot";

function record(value: unknown): Record<string, unknown> {
  return value as Record<string, unknown>;
}

describe("errores", () => {
  it("el conjunto de códigos, sus salidas y sus textos son los del contrato", () => {
    expect([...AGENTCORE_ERROR_CODES]).toEqual(Object.keys(contract.errorExitCodes));
    expect(ERROR_EXIT_CODES).toEqual(contract.errorExitCodes);
    expect(ERROR_MESSAGES).toEqual(messages.errors);
  });

  it("AgentCoreError lleva el código, la salida y el mensaje en español", () => {
    const error = new AgentCoreError("lifecycle_order", { idle: 3600, max: 1800 });
    expect(error.code).toBe("lifecycle_order");
    expect(error.exitCode).toBe(contract.errorExitCodes.lifecycle_order);
    expect(error.message).toBe(
      "idle_session_timeout (3600 s) no puede superar max_session_lifetime (1800 s)",
    );
    expect(error).toBeInstanceOf(Error);
  });

  it("ningún texto de error lleva una URL ni un ARN", () => {
    for (const template of Object.values(ERROR_MESSAGES)) {
      expect(template).not.toMatch(/https?:|arn:/);
    }
  });
});

describe("textos del SDK", () => {
  it("seguridad, avisos, coste y progreso son los del contrato", () => {
    expect(SECURITY_MESSAGES).toEqual(messages.security);
    expect(WARNING_MESSAGES).toEqual(messages.warnings);
    expect(COST_MESSAGES).toEqual(messages.cost);
    expect(PROGRESS_MESSAGES).toEqual(messages.progress);
    expect(DELETE_PROGRESS_MESSAGES).toEqual(messages.deleteProgress);
  });

  it("la salida de la CLI usa los textos del contrato", () => {
    expect(cliMessages.HEADER_TEMPLATE).toBe(messages.header);
    expect(cliMessages.STEP_TEMPLATE).toBe(messages.step);
    expect(cliMessages.STEP_LABELS).toEqual(messages.labels);
    expect(cliMessages.STACK_CONTENTS_LINE).toBe(messages.stackContents);
    expect(cliMessages.ACTION_TEMPLATES).toEqual(messages.actions);
    expect(cliMessages.SECURITY_HEADER).toBe(messages.securityHeader);
    expect(cliMessages.SECURITY_LINE_TEMPLATE).toBe(messages.securityLine);
    expect(cliMessages.WARNINGS_HEADER).toBe(messages.warningsHeader);
    expect(cliMessages.WARNING_LINE_TEMPLATE).toBe(messages.warningLine);
    expect(cliMessages.COST_HEADER).toBe(messages.costHeader);
    expect(cliMessages.COST_LINE_TEMPLATE).toBe(messages.costLine);
    expect(cliMessages.DRY_RUN_FOOTER).toBe(messages.dryRunFooter);
    expect(cliMessages.CONFIRM_QUESTION).toBe(messages.confirm);
    expect([...cliMessages.DEPLOYED_LINES]).toEqual(messages.deployed);
    expect([...cliMessages.STATUS_LINES]).toEqual(messages.status);
    expect(cliMessages.LIST_LINE_TEMPLATE).toBe(messages.listLine);
    expect(cliMessages.LIST_EMPTY_TEMPLATE).toBe(messages.listEmpty);
    expect(cliMessages.DELETE_HEADER_TEMPLATE).toBe(messages.deleteHeader);
    expect(cliMessages.DELETE_RUNTIME_LINE).toBe(messages.deleteRuntime);
    expect(cliMessages.DELETE_STACK_LINE).toBe(messages.deleteStack);
    expect(cliMessages.DELETE_LOG_GROUPS_LINE).toBe(messages.deleteLogGroups);
    expect(cliMessages.DELETE_CONFIRM_QUESTION).toBe(messages.deleteConfirm);
    expect(cliMessages.DELETE_NOTHING_TEMPLATE).toBe(messages.deleteNothing);
    expect(cliMessages.DELETED_TEMPLATE).toBe(messages.deleted);
    expect(cliMessages.STOPPED_TEMPLATE).toBe(messages.stopped);
    expect(cliMessages.ERROR_PREFIX).toBe(messages.errorPrefix);
  });
});

describe("nombres y constantes", () => {
  it("los nombres derivados del agente son los de las plantillas del contrato", () => {
    expect(runtimeNameFor(AGENT)).toBe("rayito_support_bot");
    expect(stackNameFor(AGENT)).toBe("rayito-agentcore-support-bot");
    expect(repositoryNameFor(AGENT)).toBe("rayito/agentcore/support-bot");
    expect(buildProjectNameFor(AGENT)).toBe("rayito-agentcore-support-bot");
    expect(sourceKeyFor(AGENT, "src-0123456789abcdef")).toBe(
      "rayito/images/agentcore/support-bot/src-0123456789abcdef.zip",
    );
    expect(runtimeDescriptionFor(AGENT)).toBe("Rayito agent support-bot (rayito.agentcore/1)");
    expect(runtimeLogGroupFor("rayito_support_bot-a1b2c3d4e5")).toBe(
      "/aws/bedrock-agentcore/runtimes/rayito_support_bot-a1b2c3d4e5-DEFAULT",
    );
    expect(constants.runtimeNameTemplate).toBe("{runtimeNamePrefix}{agentNameWithUnderscores}");
  });

  it("el nombre del agente: patrón y largo máximo del contrato", () => {
    expect(AGENT_NAME_PATTERN.source).toBe(constants.agentNamePattern);
    expect(AGENT_NAME_MAX_LENGTH).toBe(constants.agentNameMaxLength);
  });

  it("los parámetros, salidas y capacidades de la pila son los del contrato", () => {
    expect(STACK_PARAMETERS).toEqual(constants.stackParameters);
    expect(STACK_OUTPUTS).toEqual(constants.stackOutputs);
    expect([...AGENTCORE_STACK_CAPABILITIES]).toEqual(constants.stackCapabilities);
    expect(AGENTCORE_STACK_COMPONENT.name).toBe(constants.stackComponentName);
    expect(AGENTCORE_STACK_COMPONENT.parameters?.map((parameter) => parameter.name)).toEqual(
      Object.values(constants.stackParameters),
    );
  });

  it("la plantilla empaquetada declara cada parámetro y cada salida del componente", () => {
    for (const name of Object.values(STACK_PARAMETERS)) {
      expect(TEMPLATE_BODY).toMatch(new RegExp(`^  ${name}:$`, "m"));
    }
    for (const name of Object.values(STACK_OUTPUTS)) {
      expect(TEMPLATE_BODY).toMatch(new RegExp(`^  ${name}:$`, "m"));
    }
  });

  it("los estados del runtime y del build son los del contrato", () => {
    expect(RUNTIME_READY_STATE).toBe(constants.runtimeReadyState);
    expect([...RUNTIME_IN_PROGRESS_STATES]).toEqual(constants.runtimeInProgressStates);
    expect([...RUNTIME_BLOCKED_STATES]).toEqual(constants.runtimeBlockedStates);
    expect([RUNTIME_REPAIRABLE_STATE]).toEqual(constants.runtimeRepairableStates);
    expect(BUILD_SUCCEEDED_STATE).toBe(constants.buildSucceededState);
    expect([...RUNTIME_REGION_VARIABLES]).toEqual(constants.runtimeRegionEnvironmentVariables);
    expect(SESSION_ID_PATTERN.source).toBe(record(constants.sessionIdRules).pattern);
  });

  it("las variables de StartBuild y las plantillas de ARN son las del contrato", () => {
    expect(BUILD_IMAGE_TAG_VARIABLE).toBe(constants.buildEnvironmentVariables.imageTag);
    expect(BUILD_BASE_IMAGE_VARIABLE).toBe(constants.buildEnvironmentVariables.baseImage);
    expect(AWS_PARTITION).toBe(constants.awsPartition);
    expect(SECRET_ARN_TEMPLATE).toBe(constants.secretArnTemplate);
    expect(REGIONAL_FOUNDATION_MODEL_ARN_TEMPLATE).toBe(
      constants.regionalFoundationModelArnTemplate,
    );
    expect(ANY_REGION_FOUNDATION_MODEL_ARN_TEMPLATE).toBe(
      constants.anyRegionFoundationModelArnTemplate,
    );
    expect(GLOBAL_FOUNDATION_MODEL_ARN_TEMPLATE).toBe(constants.globalFoundationModelArnTemplate);
    expect(INFERENCE_PROFILE_ARN_TEMPLATE).toBe(constants.inferenceProfileArnTemplate);
  });

  it("el export de agentcore usa el argumento y el ayudante de credenciales del contrato", () => {
    const credentials = constants.export.awsCredentials as Record<string, string>;
    expect(AGENT_BASE_IMAGE_ARG).toBe(constants.dockerfileBaseImageArg);
    expect(AWS_CREDENTIALS_REQUIREMENTS_NAME).toBe(credentials.requirementsAsset);
    expect(AWS_CREDENTIALS_REQUIREMENTS_PATH).toBe(credentials.requirementsPath);
    expect(AWS_CREDENTIALS_VENV_DIR).toBe(credentials.venvDir);
    expect(AWS_ENV_CREDENTIALS_SCRIPT_NAME).toBe(credentials.scriptAsset);
    expect(AWS_ENV_CREDENTIALS_SCRIPT_PATH).toBe(credentials.scriptPath);
    expect(HOST_AWS_ENV_CREDENTIALS_KEY).toBe(credentials.hostKey);
    expect(AGENTCORE_BASE_IMAGE_MIRROR).toContain("public.ecr.aws/docker/library/python");
    expect([...AGENTCORE_ENV_CREDENTIAL_ADAPTERS]).toEqual(["vercel-ai"]);
    expect(AGENT_AWS_CREDENTIALS_REQUIREMENTS_SHA256).toMatch(/^[0-9a-f]{64}$/);
  });
});

describe("superficie de TypeScript", () => {
  it("AgentCoreFleet tiene los miembros del contrato", async () => {
    const { AgentCoreFleet } = await import("../../src/agentcore/fleet.js");
    const sdk = contract.sdk.typescript as { fleet: { methods: string[]; properties: string[] } };
    for (const signature of sdk.fleet.methods) {
      const name = signature.split("(")[0] ?? "";
      expect(
        typeof (AgentCoreFleet.prototype as unknown as Record<string, unknown>)[name],
        name,
      ).toBe("function");
    }
    expect(typeof AgentCoreFleet.connect).toBe("function");
    for (const property of sdk.fleet.properties) {
      const own = Object.getOwnPropertyNames(AgentCoreFleet.prototype).includes(property);
      const field = [
        "name",
        "region",
        "runtimeName",
        "runtimeId",
        "runtimeArn",
        "runtimeVersion",
        "imageUri",
      ].includes(property);
      expect(own || field, property).toBe(true);
    }
  });
});

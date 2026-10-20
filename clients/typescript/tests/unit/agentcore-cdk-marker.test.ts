/**
 * La marca `+cdk` (ADR-032 D6): un runtime que gestiona CloudFormation con
 * `RayitoAgent` es de Rayito para listar, consultar e invocar, pero
 * `deployAgent`, `rayito agent deploy` y `rayito agent delete` lo rechazan
 * con `name_conflict` y el mensaje del contrato antes de escribir nada.
 * Gemelo de los casos `+cdk` de `test_agentcore_name_collision.py`.
 */

import { describe, expect, it } from "vitest";
import { AgentCoreDeleter, findAgent, listAgents } from "../../src/agentcore/lifecycle.js";
import { CDK_MANAGED_MESSAGE } from "../../src/agentcore/messages.js";
import {
  CDK_DESCRIPTION_MARKER,
  isCdkManaged,
  isOwned,
  ownedAgentName,
  runtimeDescriptionFor,
} from "../../src/agentcore/names.js";
import { AgentCoreDeployer } from "../../src/agentcore/service.js";
import type { AgentCoreError } from "../../src/errors.js";
import { formatTemplate } from "../../src/template-text.js";
import {
  AGENT,
  clockOf,
  desiredDetails,
  newAccount,
  operations,
  REGION,
  RUNTIME_NAME,
  samplePlan,
} from "./agentcore-fakes.js";
import { loadContract } from "./cdk-contract-harness.js";

const CDK_DESCRIPTION = `Rayito agent ${AGENT} (rayito.agentcore/1+cdk)`;
const STACK = `rayito-agentcore-${AGENT}`;
const WRITES = [
  "CreateStack",
  "UpdateStack",
  "PutObject",
  "StartBuild",
  "CreateAgentRuntime",
  "UpdateAgentRuntime",
  "DeleteAgentRuntime",
  "DeleteStack",
  "DeleteLogGroup",
];

function cdkAccount(): ReturnType<typeof newAccount> {
  const runtime = desiredDetails({ description: CDK_DESCRIPTION });
  return newAccount({
    stack: { status: undefined },
    runtime: { listed: runtime, details: runtime },
  });
}

function refusal(): string {
  return formatTemplate(CDK_MANAGED_MESSAGE, {
    name: AGENT,
    stackHint: `del runtime ${RUNTIME_NAME}`,
  });
}

describe("marca +cdk", () => {
  it("la descripción de RayitoAgent lleva la marca del contrato", () => {
    expect(CDK_DESCRIPTION_MARKER).toBe(loadContract().constants.agentcoreDescriptionMarker);
    expect(runtimeDescriptionFor(AGENT, { managedByCdk: true })).toBe(CDK_DESCRIPTION);
    expect(runtimeDescriptionFor(AGENT)).toBe(`Rayito agent ${AGENT} (rayito.agentcore/1)`);
  });

  it("un runtime +cdk es de Rayito y se reconoce como de CDK", () => {
    const listed = desiredDetails({ description: CDK_DESCRIPTION });
    expect(isOwned(listed, AGENT)).toBe(true);
    expect(ownedAgentName(listed)).toBe(AGENT);
    expect(isCdkManaged(listed)).toBe(true);
    expect(isCdkManaged(desiredDetails())).toBe(false);
  });

  it("list y connect ven el agente de CDK", async () => {
    const account = cdkAccount();
    expect((await listAgents(account.ports)).map((item) => item.name)).toEqual([AGENT]);
    const found = await findAgent(account.ports, {
      name: AGENT,
      region: REGION,
      runtimeName: RUNTIME_NAME,
    });
    expect(found.description).toBe(CDK_DESCRIPTION);
  });

  it("deploy sobre un agente de CDK es name_conflict sin escribir nada", async () => {
    const account = cdkAccount();
    const failure = await new AgentCoreDeployer(account.ports, clockOf(account))
      .apply({
        plan: samplePlan(),
        tree: new Map([["Dockerfile", "FROM scratch\n"]]),
        modelRegion: REGION,
        buildImage: undefined,
        buildTimeoutMs: 1_800_000,
      })
      .catch((thrown: unknown) => thrown);
    expect((failure as AgentCoreError).code).toBe("name_conflict");
    expect((failure as AgentCoreError).message).toBe(refusal());
    expect((failure as AgentCoreError).exitCode).toBe(2);
    expect(operations(account).filter((name) => WRITES.includes(name))).toEqual([]);
  });

  it("delete de un agente de CDK es name_conflict sin borrar nada", async () => {
    const account = cdkAccount();
    const failure = await new AgentCoreDeleter(account.ports, clockOf(account))
      .delete({ name: AGENT, region: REGION, runtimeName: RUNTIME_NAME, stackName: STACK })
      .catch((thrown: unknown) => thrown);
    expect((failure as AgentCoreError).code).toBe("name_conflict");
    expect((failure as AgentCoreError).message).toBe(refusal());
    expect(operations(account).filter((name) => name.startsWith("Delete"))).toEqual([]);
  });
});

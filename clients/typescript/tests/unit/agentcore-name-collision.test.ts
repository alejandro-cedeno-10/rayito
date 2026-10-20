/**
 * `support-bot` y `support_bot` dan el mismo runtime y la misma pila: todo
 * nombre derivado sale de la forma canónica y el segundo agente falla con
 * `name_conflict` en vez de pisar al primero (gemelo de
 * `test_agentcore_name_collision.py`).
 */

import { describe, expect, it } from "vitest";
import { AgentCoreDeleter } from "../../src/agentcore/lifecycle.js";
import {
  canonicalAgentName,
  repositoryNameFor,
  runtimeNameFor,
  sourceKeyFor,
  stackNameFor,
} from "../../src/agentcore/names.js";
import { AgentCoreDeployer } from "../../src/agentcore/service.js";
import { STACK_PARAMETERS } from "../../src/agentcore/stack.js";
import type { AgentCoreError } from "../../src/errors.js";
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

const OTHER = "support_bot";
const STACK = `rayito-agentcore-${AGENT}`;

function applyAs(account: ReturnType<typeof newAccount>): Promise<unknown> {
  return new AgentCoreDeployer(account.ports, clockOf(account)).apply({
    plan: samplePlan({ name: OTHER }),
    tree: new Map([["Dockerfile", "FROM scratch\n"]]),
    modelRegion: REGION,
    buildImage: undefined,
    buildTimeoutMs: 1_800_000,
  });
}

describe("colisión de nombres entre a-b y a_b", () => {
  it("todo nombre derivado sale de la forma canónica", () => {
    expect(canonicalAgentName(OTHER)).toBe(AGENT);
    expect(stackNameFor(OTHER)).toBe(STACK);
    expect(runtimeNameFor(OTHER)).toBe(RUNTIME_NAME);
    expect(repositoryNameFor(OTHER)).toBe(`rayito/agentcore/${AGENT}`);
    expect(sourceKeyFor(OTHER, "t")).toBe(sourceKeyFor(AGENT, "t"));
  });

  it("los nombres sólo con guion conservan los que ya tenían", () => {
    expect(stackNameFor("a-b")).toBe("rayito-agentcore-a-b");
    expect(runtimeNameFor("a-b")).toBe("rayito_a_b");
    expect(repositoryNameFor("a-b")).toBe("rayito/agentcore/a-b");
  });

  it("un runtime de la otra grafía es name_conflict y no se escribe nada", async () => {
    const account = newAccount({
      stack: { status: "UPDATE_COMPLETE" },
      runtime: { listed: desiredDetails(), details: desiredDetails() },
    });
    const failure = await applyAs(account).catch((thrown: unknown) => thrown);
    expect((failure as AgentCoreError).code).toBe("name_conflict");
    expect(operations(account)).not.toContain("UpdateStack");
    expect(operations(account)).not.toContain("PutObject");
  });

  it("una pila de la otra grafía es name_conflict aunque no haya runtime", async () => {
    const account = newAccount({
      stack: {
        status: "UPDATE_COMPLETE",
        parameters: { [STACK_PARAMETERS.agentDisplayName]: AGENT },
      },
    });
    const failure = await applyAs(account).catch((thrown: unknown) => thrown);
    expect((failure as AgentCoreError).code).toBe("name_conflict");
    expect(operations(account)).not.toContain("UpdateStack");
  });

  it("la pila guarda el nombre tal como se escribió y nombra los recursos con el canónico", async () => {
    const account = newAccount();
    await applyAs(account);
    expect(account.stacks.state.parameters[STACK_PARAMETERS.agentName]).toBe(AGENT);
    expect(account.stacks.state.parameters[STACK_PARAMETERS.agentDisplayName]).toBe(OTHER);
  });

  it("borrar nunca toca los recursos de la otra grafía", async () => {
    const account = newAccount({
      stack: {
        status: "UPDATE_COMPLETE",
        parameters: { [STACK_PARAMETERS.agentDisplayName]: AGENT },
      },
    });
    const failure = await new AgentCoreDeleter(account.ports, clockOf(account))
      .delete({ name: OTHER, region: REGION, runtimeName: RUNTIME_NAME, stackName: STACK })
      .catch((thrown: unknown) => thrown);
    expect((failure as AgentCoreError).code).toBe("name_conflict");
    expect(operations(account).filter((name) => name.startsWith("Delete"))).toEqual([]);
  });
});

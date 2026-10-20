/**
 * Borrar y listar lo de Rayito en AgentCore (ADR-031, D8): sólo se borra lo que
 * creó Rayito para ese nombre, y nada si el runtime no lleva la marca.
 */

import { describe, expect, it } from "vitest";
import { AgentCoreDeleter, listAgents } from "../../src/agentcore/lifecycle.js";
import { AgentCoreError } from "../../src/errors.js";
import {
  AGENT,
  clockOf,
  desiredDetails,
  type FakeAccount,
  newAccount,
  operations,
  REGION,
  RUNTIME_ID,
  RUNTIME_NAME,
} from "./agentcore-fakes.js";

const REQUEST = {
  name: AGENT,
  region: REGION,
  runtimeName: RUNTIME_NAME,
  stackName: `rayito-agentcore-${AGENT}`,
};

function deployed(): FakeAccount {
  return newAccount({
    stack: { status: "UPDATE_COMPLETE" },
    runtime: { listed: desiredDetails(), details: desiredDetails() },
  });
}

function deleter(account: FakeAccount): AgentCoreDeleter {
  return new AgentCoreDeleter(account.ports, clockOf(account));
}

describe("borrar un agente", () => {
  it("borra el runtime, la pila y el grupo de logs del runtime, y nada más", async () => {
    const account = deployed();
    const steps: string[] = [];
    const result = await deleter(account).delete({
      ...REQUEST,
      onStep: (step) => steps.push(step.kind),
    });
    expect(result).toEqual({
      name: AGENT,
      region: REGION,
      runtimeDeleted: true,
      stackDeleted: true,
      logGroupDeleted: true,
    });
    const writes = operations(account).filter((name) => name.startsWith("Delete"));
    expect(writes).toEqual(["DeleteAgentRuntime", "DeleteStack", "DeleteLogGroup"]);
    expect(account.logs.deleted).toEqual([`/aws/bedrock-agentcore/runtimes/${RUNTIME_ID}-DEFAULT`]);
    expect(steps).toEqual(["runtime", "stack", "logGroup"]);
  });

  it("un runtime con el nombre y sin la marca de Rayito es name_conflict y no se borra nada", async () => {
    const account = newAccount({
      stack: { status: "UPDATE_COMPLETE" },
      runtime: {
        listed: desiredDetails({ description: "creado a mano" }),
        details: desiredDetails(),
      },
    });
    const failure = await deleter(account)
      .delete(REQUEST)
      .catch((thrown: unknown) => thrown);
    expect(failure).toBeInstanceOf(AgentCoreError);
    expect((failure as AgentCoreError).code).toBe("name_conflict");
    expect(operations(account).filter((name) => name.startsWith("Delete"))).toEqual([]);
  });

  it("sin nada que borrar no escribe ninguna llamada y devuelve todo en false", async () => {
    const account = newAccount();
    const result = await deleter(account).delete(REQUEST);
    expect(result).toMatchObject({
      runtimeDeleted: false,
      stackDeleted: false,
      logGroupDeleted: false,
    });
    expect(operations(account).filter((name) => name.startsWith("Delete"))).toEqual([]);
  });

  it("sólo la pila (el runtime ya no está) se borra sin tocar logs", async () => {
    const account = newAccount({ stack: { status: "CREATE_COMPLETE" } });
    const result = await deleter(account).delete(REQUEST);
    expect(result).toMatchObject({
      runtimeDeleted: false,
      stackDeleted: true,
      logGroupDeleted: false,
    });
    expect(account.logs.deleted).toEqual([]);
  });

  it("un grupo de logs que ya no existía se cuenta como no borrado", async () => {
    const account = deployed();
    account.logs.exists = false;
    const result = await deleter(account).delete(REQUEST);
    expect(result.logGroupDeleted).toBe(false);
    expect(result.runtimeDeleted).toBe(true);
  });

  it("una pila que no se borra es delete_failed", async () => {
    const account = deployed();
    account.stacks.failWait = true;
    const failure = await deleter(account)
      .delete(REQUEST)
      .catch((thrown: unknown) => thrown);
    expect((failure as AgentCoreError).code).toBe("delete_failed");
  });
});

describe("listar agentes", () => {
  it("lista sólo los runtimes de Rayito, con el nombre del agente", async () => {
    const account = deployed();
    expect(await listAgents(account.ports)).toEqual([
      {
        name: AGENT,
        runtimeName: RUNTIME_NAME,
        runtimeId: RUNTIME_ID,
        runtimeVersion: "2",
        status: "READY",
      },
    ]);
  });

  it("no lista un runtime sin la marca ni uno con otro nombre", async () => {
    const account = newAccount({
      runtime: {
        listed: desiredDetails({ description: "creado a mano" }),
        details: desiredDetails(),
      },
    });
    expect(await listAgents(account.ports)).toEqual([]);
    const renamed = newAccount({
      runtime: {
        listed: desiredDetails({ runtimeName: "otro_nombre" }),
        details: desiredDetails(),
      },
    });
    expect(await listAgents(renamed.ports)).toEqual([]);
  });
});

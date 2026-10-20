/**
 * Lo que `--target agentcore` hace con un agente ya desplegado (ADR-031, D8):
 * listar los de Rayito, resolver uno por nombre y borrar todo lo que Rayito
 * creó para él. Sólo toca lo que lleva la marca de Rayito: un runtime con el
 * nombre y sin la marca es `name_conflict` y no se borra nada. Nada aquí
 * importa un SDK de AWS.
 */

import { AgentCoreError, StackError } from "../errors.js";
import { AGENTCORE_DELETE_TIMEOUT_SECONDS, AGENTCORE_READY_POLL_SECONDS } from "../limits.js";
import { DEFAULT_WAIT_TIMEOUT_MS } from "../stacks/service.js";
import { MS_PER_SECOND } from "../units.js";
import { Clock, type ServiceSettings } from "./clock.js";
import { checkStackOwner, stackObservation } from "./decisions.js";
import type {
  AgentCoreDeleteResult,
  AgentCoreListItem,
  ListedRuntime,
  RuntimeDetails,
} from "./models.js";
import { isOwned, ownedAgentName, runtimeLogGroupFor } from "./names.js";
import type { AgentCorePorts } from "./ports.js";
import { deleteEmitter, type EmitDelete, type OnStep } from "./progress.js";

/** Los runtimes de Rayito de la región, con el nombre del agente que sale de su descripción. */
export async function listAgents(ports: AgentCorePorts): Promise<AgentCoreListItem[]> {
  const items: AgentCoreListItem[] = [];
  for (const listed of await ports.control.listRuntimes()) {
    const name = ownedAgentName(listed);
    if (name !== undefined) {
      items.push({
        name,
        runtimeName: listed.runtimeName,
        runtimeId: listed.runtimeId,
        runtimeVersion: listed.runtimeVersion,
        status: listed.status,
      });
    }
  }
  return items;
}

/** El runtime de Rayito del agente `name`, que `connect`, `status` e `invoke` resuelven por nombre; `runtime_not_found` si no hay. */
export async function findAgent(
  ports: AgentCorePorts,
  target: { readonly name: string; readonly region: string; readonly runtimeName: string },
): Promise<RuntimeDetails> {
  const listed = (await ports.control.listRuntimes()).find(
    (candidate) => candidate.runtimeName === target.runtimeName && isOwned(candidate, target.name),
  );
  const details =
    listed === undefined ? undefined : await ports.control.getRuntime(listed.runtimeId);
  if (details === undefined) {
    throw new AgentCoreError("runtime_not_found", { name: target.name, region: target.region });
  }
  return details;
}

export interface DeleteRequest {
  readonly name: string;
  readonly region: string;
  readonly runtimeName: string;
  readonly stackName: string;
  readonly onStep?: OnStep;
}

/** Lo que existe de un agente en la región: su runtime de Rayito (si hay) y si tiene pila. Un runtime con el nombre y sin la marca es `name_conflict`. */
export async function inspectDeletion(
  ports: AgentCorePorts,
  target: Pick<DeleteRequest, "name" | "runtimeName" | "stackName">,
): Promise<{ readonly listed: ListedRuntime | undefined; readonly stackExists: boolean }> {
  const listed = (await ports.control.listRuntimes()).find(
    (candidate) => candidate.runtimeName === target.runtimeName,
  );
  if (listed !== undefined && !isOwned(listed, target.name)) {
    throw new AgentCoreError("name_conflict", {
      runtimeName: target.runtimeName,
      name: target.name,
    });
  }
  const stack = await ports.stacks.describe(target.stackName);
  if (stack !== undefined) {
    checkStackOwner(stackObservation(target.stackName, stack), target.name);
  }
  return { listed, stackExists: stack !== undefined };
}

/**
 * Borra lo que Rayito creó para un agente y nada más: el runtime, la pila
 * `rayito-agentcore-<nombre>` con su repositorio, su proyecto de CodeBuild y sus
 * roles, y el grupo de logs del runtime. Sin nada que borrar devuelve todo en
 * `false`.
 *
 * Coste y activación
 * -------------------
 * Activa: `AgentCoreFleet.delete()` o `rayito agent delete <nombre> --target agentcore`.
 * Recursos y llamadas AWS: `DeleteAgentRuntime`, `DeleteStack` y `DeleteLogGroup`;
 *     lee con `ListAgentRuntimes`, `GetAgentRuntime` y `DescribeStacks`.
 * Coste aproximado: gratis; deja de costar el almacenamiento de las imágenes.
 * IAM: `bedrock-agentcore:DeleteAgentRuntime`, `cloudformation:DeleteStack`
 *     y `logs:DeleteLogGroup` sobre el grupo del runtime.
 * Cómo apagarla: no llamar a `delete()`.
 * Ejemplo: `await new AgentCoreDeleter(ports).delete(request)`.
 */
export class AgentCoreDeleter {
  readonly #ports: AgentCorePorts;
  readonly #clock: Clock;

  constructor(ports: AgentCorePorts, settings: ServiceSettings = {}) {
    this.#ports = ports;
    this.#clock = new Clock(settings);
  }

  async delete(request: DeleteRequest): Promise<AgentCoreDeleteResult> {
    const emit = deleteEmitter(request.onStep);
    const { listed, stackExists } = await inspectDeletion(this.#ports, request);
    const runtimeDeleted =
      listed !== undefined && (await this.#deleteRuntime(request, listed, emit));
    const stackDeleted = stackExists && (await this.#deleteStack(request, emit));
    const logGroupDeleted =
      listed !== undefined && (await this.#deleteLogGroup(listed.runtimeId, emit));
    return {
      name: request.name,
      region: request.region,
      runtimeDeleted,
      stackDeleted,
      logGroupDeleted,
    };
  }

  async #deleteRuntime(
    request: DeleteRequest,
    listed: ListedRuntime,
    emit: EmitDelete,
  ): Promise<boolean> {
    emit("runtime", { runtimeName: request.runtimeName });
    await this.#ports.control.deleteRuntime(listed.runtimeId);
    const started = this.#clock.now();
    for (;;) {
      const details = await this.#ports.control.getRuntime(listed.runtimeId);
      if (details === undefined) {
        return true;
      }
      if (this.#clock.now() - started >= AGENTCORE_DELETE_TIMEOUT_SECONDS * MS_PER_SECOND) {
        throw new AgentCoreError("delete_failed", {
          resource: "runtime",
          resourceName: request.runtimeName,
          status: details.status,
        });
      }
      await this.#clock.sleep(AGENTCORE_READY_POLL_SECONDS * MS_PER_SECOND);
    }
  }

  async #deleteStack(request: DeleteRequest, emit: EmitDelete): Promise<boolean> {
    emit("stack", { stackName: request.stackName });
    try {
      await this.#ports.stacks.delete(request.stackName);
      await this.#ports.stacks.wait(request.stackName, "deleted", DEFAULT_WAIT_TIMEOUT_MS);
    } catch (error) {
      if (!(error instanceof StackError)) {
        throw error;
      }
      const observed = await this.#ports.stacks.describe(request.stackName).catch(() => undefined);
      throw new AgentCoreError(
        "delete_failed",
        { resource: "stack", resourceName: request.stackName, status: observed?.state ?? "" },
        { cause: error },
      );
    }
    return true;
  }

  async #deleteLogGroup(runtimeId: string, emit: EmitDelete): Promise<boolean> {
    const logGroup = runtimeLogGroupFor(runtimeId);
    emit("logGroup", { logGroup });
    return this.#ports.logs.deleteLogGroup(logGroup);
  }
}

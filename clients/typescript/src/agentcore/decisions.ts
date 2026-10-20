/**
 * Las decisiones de `--target agentcore` al aplicar (ADR-031, D5 y D6): qué
 * hacer con la pila, la imagen y el runtime a partir de lo que se observó en la
 * cuenta en esa misma ejecución. Puras: el servicio lee AWS y ejecuta lo que
 * éstas deciden. Los vectores compartidos con Python están en
 * `testdata/agentcore-deploy/apply-vectors.json`.
 */

import { AgentCoreError } from "../errors.js";
import {
  AGENTCORE_MAX_IMAGE_BYTES,
  AGENTCORE_NETWORK_MODE,
  AGENTCORE_SERVER_PROTOCOL,
} from "../limits.js";
import { classifyStack, type StackObservation } from "../stacks/condition.js";
import type { StackProvisioner } from "../stacks/port.js";
import type {
  DesiredRuntime,
  ImageAction,
  ImageObservation,
  ListedRuntime,
  RuntimeAction,
  RuntimeDetails,
  StackAction,
} from "./models.js";
import { isOwned, runtimeNameFor } from "./names.js";
import { STACK_OUTPUTS, STACK_PARAMETERS } from "./stack.js";

/** Los megabytes de los mensajes son decimales (`bytes // 1000000`), como el límite de 2 GB de AgentCore. */
export const BYTES_PER_MEGABYTE = 1_000_000;
export const RUNTIME_READY_STATE = "READY";
export const RUNTIME_IN_PROGRESS_STATES: ReadonlySet<string> = new Set(["CREATING", "UPDATING"]);
export const RUNTIME_BLOCKED_STATES: ReadonlySet<string> = new Set([
  "CREATE_FAILED",
  "DELETING",
  "DELETE_FAILED",
]);
export const RUNTIME_REPAIRABLE_STATE = "UPDATE_FAILED";
export const RUNTIME_FAILED_STATES: ReadonlySet<string> = new Set([
  "CREATE_FAILED",
  "UPDATE_FAILED",
]);
export const BUILD_SUCCEEDED_STATE = "SUCCEEDED";
export const BUILD_IN_PROGRESS_STATE = "IN_PROGRESS";

/** Lo que `DescribeStacks` dijo de la pila `name`, en la forma de `classifyStack`. */
export function stackObservation(
  name: string,
  status: Awaited<ReturnType<StackProvisioner["describe"]>>,
): StackObservation {
  return {
    name,
    status: status?.state,
    outputs: status?.outputs ?? {},
    parameters: status?.parameters ?? {},
  };
}

/**
 * `name_conflict` si la pila es de otro agente cuyo nombre da la misma pila
 * (`a-b` y `a_b`): la plantilla guarda el nombre sin normalizar en
 * `AgentDisplayName`. Una pila sin ese parámetro no se discute.
 */
export function checkStackOwner(observation: StackObservation, name: string): void {
  const recorded = observation.parameters[STACK_PARAMETERS.agentDisplayName];
  if (recorded !== undefined && recorded !== "" && recorded !== name) {
    throw new AgentCoreError("name_conflict", { runtimeName: runtimeNameFor(name), name });
  }
}

/** Las seis salidas de la plantilla, ya leídas. */
export interface StackOutputs {
  readonly repositoryName: string;
  readonly repositoryUri: string;
  readonly repositoryArn: string;
  readonly buildProjectName: string;
  readonly buildLogGroupName: string;
  readonly runtimeRoleArn: string;
}

/**
 * Qué hacer con la pila: crearla si falta, actualizarla si sirve (CloudFormation
 * no toca nada si la plantilla y los parámetros son los mismos), esperarla si
 * hay una operación en curso, y `stack_blocked` si está en un estado que sólo
 * se arregla borrándola. Una pila utilizable sin las seis salidas de la
 * plantilla no es de Rayito: `stack_failed`, y no se actualiza.
 */
export function decideStack(observation: StackObservation, name: string): StackAction {
  const condition = classifyStack(observation);
  if (condition === "missing") {
    return "create";
  }
  checkStackOwner(observation, name);
  if (condition === "in_progress") {
    return "wait";
  }
  if (condition === "blocked") {
    throw new AgentCoreError("stack_blocked", {
      stackName: observation.name,
      status: observation.status ?? "",
      name,
    });
  }
  requireStackOutputs(observation);
  return "update";
}

/** Las seis salidas de una pila usable; sin alguna no es la plantilla de Rayito (`stack_failed`). */
export function requireStackOutputs(observation: StackObservation): StackOutputs {
  const read = (key: string): string => {
    const value = observation.outputs[key];
    if (!value) {
      throw new AgentCoreError("stack_failed", {
        stackName: observation.name,
        status: observation.status ?? "",
      });
    }
    return value;
  };
  return {
    repositoryName: read(STACK_OUTPUTS.repositoryName),
    repositoryUri: read(STACK_OUTPUTS.repositoryUri),
    repositoryArn: read(STACK_OUTPUTS.repositoryArn),
    buildProjectName: read(STACK_OUTPUTS.buildProjectName),
    buildLogGroupName: read(STACK_OUTPUTS.buildLogGroupName),
    runtimeRoleArn: read(STACK_OUTPUTS.runtimeRoleArn),
  };
}

/**
 * Qué hacer con la imagen: reutilizarla si la etiqueta (por contenido) ya está
 * en el repositorio y construirla si no. Con `buildImage: false` (`--no-build`)
 * y sin imagen es `image_missing`: nada se construye. `image` es `undefined` si
 * la pila no existía (el repositorio tampoco).
 */
export function decideImage(input: {
  readonly image: ImageObservation | undefined;
  readonly buildImage: boolean | undefined;
  readonly imageTag: string;
  readonly repositoryName: string;
}): ImageAction {
  if (input.image?.exists === true) {
    return "reuse";
  }
  if (input.buildImage === false) {
    throw new AgentCoreError("image_missing", {
      imageTag: input.imageTag,
      repositoryName: input.repositoryName,
    });
  }
  return "build";
}

/** La imagen no puede pasar de 2 GB (`image_too_large`). */
export function checkImageSize(sizeBytes: number | undefined): void {
  if (sizeBytes !== undefined && sizeBytes > AGENTCORE_MAX_IMAGE_BYTES) {
    throw new AgentCoreError("image_too_large", {
      megabytes: Math.floor(sizeBytes / BYTES_PER_MEGABYTE),
      maxMegabytes: Math.floor(AGENTCORE_MAX_IMAGE_BYTES / BYTES_PER_MEGABYTE),
    });
  }
}

function sameEnvironment(
  left: Readonly<Record<string, string>>,
  right: Readonly<Record<string, string>>,
): boolean {
  const leftKeys = Object.keys(left);
  return (
    leftKeys.length === Object.keys(right).length &&
    leftKeys.every((key) => Object.hasOwn(right, key) && left[key] === right[key])
  );
}

/** Si el runtime ya tiene la configuración deseada: los campos de `constants.runtimeComparedFields` (con MMDSv2 siempre exigido). */
export function runtimeMatches(details: RuntimeDetails, desired: DesiredRuntime): boolean {
  return (
    details.containerUri === desired.containerUri &&
    details.roleArn === desired.roleArn &&
    details.networkMode === AGENTCORE_NETWORK_MODE &&
    details.serverProtocol === AGENTCORE_SERVER_PROTOCOL &&
    sameEnvironment(details.environmentVariables, desired.environmentVariables) &&
    details.idleRuntimeSessionTimeout === desired.idleRuntimeSessionTimeout &&
    details.maxLifetime === desired.maxLifetime &&
    details.requireMMDSV2
  );
}

/**
 * `name_conflict` si el runtime con ese nombre no es del agente `name` y
 * `runtime_blocked` si está en un estado que no se toca. Se comprueba antes de
 * crear la pila y construir la imagen: ahorra el coste de un despliegue que
 * acabaría fallando.
 */
export function checkRuntimeUsable(
  listed: ListedRuntime | undefined,
  runtimeName: string,
  name: string,
): void {
  if (listed === undefined) {
    return;
  }
  if (!isOwned(listed, name)) {
    throw new AgentCoreError("name_conflict", { runtimeName, name });
  }
  if (RUNTIME_BLOCKED_STATES.has(listed.status)) {
    throw new AgentCoreError("runtime_blocked", { runtimeName, status: listed.status, name });
  }
}

/**
 * Qué hacer con el runtime `runtimeName`: crearlo si falta, esperarlo si está
 * en `CREATING` o `UPDATING`, repararlo (actualizarlo) si quedó en
 * `UPDATE_FAILED`, actualizarlo si algún campo comparado difiere y reutilizarlo
 * si todos coinciden. Uno con el nombre y sin la marca de Rayito es
 * `name_conflict` y nunca se toca; `CREATE_FAILED`, `DELETING` y `DELETE_FAILED`
 * son `runtime_blocked`. `details` es lo que devolvió `GetAgentRuntime` de un
 * runtime `READY`.
 */
export function decideRuntime(input: {
  readonly name: string;
  readonly runtimeName: string;
  readonly listed: ListedRuntime | undefined;
  readonly details: RuntimeDetails | undefined;
  readonly desired: DesiredRuntime;
}): RuntimeAction {
  const { listed } = input;
  if (listed === undefined) {
    return "create";
  }
  checkRuntimeUsable(listed, input.runtimeName, input.name);
  if (RUNTIME_IN_PROGRESS_STATES.has(listed.status)) {
    return "wait";
  }
  if (listed.status === RUNTIME_REPAIRABLE_STATE || input.details === undefined) {
    return "update";
  }
  return runtimeMatches(input.details, input.desired) ? "reuse" : "update";
}

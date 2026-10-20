/**
 * El paso del runtime de un despliegue en AgentCore (ADR-031, D5 y D6): lo crea
 * (y exige MMDSv2 con una actualización, porque `CreateAgentRuntime` no admite
 * `metadataConfiguration`), lo actualiza, lo espera o lo reutiliza según
 * `decideRuntime`, y devuelve lo que `GetAgentRuntime` ve al terminar. Nunca
 * crea un segundo runtime con el mismo nombre. Sin imports de AWS.
 */

import { AgentCoreError } from "../errors.js";
import { AGENTCORE_READY_POLL_SECONDS, AGENTCORE_READY_TIMEOUT_SECONDS } from "../limits.js";
import { stackTags } from "../stacks/model.js";
import { MS_PER_SECOND } from "../units.js";
import type { Clock } from "./clock.js";
import {
  decideRuntime,
  RUNTIME_FAILED_STATES,
  RUNTIME_READY_STATE,
  type StackOutputs,
} from "./decisions.js";
import { clientTokenFor } from "./identifiers.js";
import type {
  AgentCoreDeployPlan,
  AgentCoreStep,
  DesiredRuntime,
  ListedRuntime,
  RuntimeAction,
  RuntimeDetails,
} from "./models.js";
import { isOwned, runtimeDescriptionFor, runtimeLogGroupFor } from "./names.js";
import type { RuntimeControl, RuntimeWriteRequest } from "./ports.js";
import { RuntimeConflictError } from "./ports.js";
import type { Emit } from "./progress.js";
import { AGENTCORE_STACK_COMPONENT } from "./stack.js";

interface Executed {
  readonly runtimeId: string;
  readonly action: RuntimeAction;
}

export class RuntimeApplier {
  readonly #control: RuntimeControl;
  readonly #clock: Clock;

  constructor(control: RuntimeControl, clock: Clock) {
    this.#control = control;
    this.#clock = clock;
  }

  /** Lleva el runtime del agente a la configuración deseada y devuelve el paso y lo que se ve al final. */
  async apply(
    plan: AgentCoreDeployPlan,
    outputs: StackOutputs,
    emit: Emit,
  ): Promise<{ readonly step: AgentCoreStep; readonly details: RuntimeDetails }> {
    const desired: DesiredRuntime = {
      containerUri: `${outputs.repositoryUri}:${plan.imageTag}`,
      roleArn: outputs.runtimeRoleArn,
      environmentVariables: plan.environment,
      idleRuntimeSessionTimeout: plan.lifecycle.idleSessionTimeoutSeconds,
      maxLifetime: plan.lifecycle.maxSessionLifetimeSeconds,
    };
    const listed = await this.#find(plan.runtimeName);
    const details =
      listed?.status === RUNTIME_READY_STATE
        ? await this.#control.getRuntime(listed.runtimeId)
        : undefined;
    const action = decideRuntime({
      name: plan.name,
      runtimeName: plan.runtimeName,
      listed,
      details,
      desired,
    });
    const executed = await this.#execute(plan, { action, listed, desired }, emit);
    const final = await this.#control.getRuntime(executed.runtimeId);
    if (final === undefined) {
      throw new AgentCoreError("runtime_not_found", { name: plan.name, region: plan.region });
    }
    emit(executed.action === "reuse" ? "runtimeReuse" : "runtimeReady", {
      runtimeName: plan.runtimeName,
      version: final.runtimeVersion,
    });
    return {
      step: {
        resource: "runtime",
        name: plan.runtimeName,
        action: executed.action,
        status: listed?.status ?? null,
      },
      details: final,
    };
  }

  async #find(runtimeName: string): Promise<ListedRuntime | undefined> {
    return (await this.#control.listRuntimes()).find(
      (listed) => listed.runtimeName === runtimeName,
    );
  }

  async #execute(
    plan: AgentCoreDeployPlan,
    choice: {
      readonly action: RuntimeAction;
      readonly listed: ListedRuntime | undefined;
      readonly desired: DesiredRuntime;
    },
    emit: Emit,
  ): Promise<Executed> {
    const { listed, desired } = choice;
    if (choice.action === "create" || listed === undefined) {
      return this.#create(plan, desired, emit);
    }
    const { runtimeId } = listed;
    if (choice.action === "reuse") {
      return { runtimeId, action: "reuse" };
    }
    if (choice.action === "update") {
      await this.#update(plan, runtimeId, desired, emit);
      return { runtimeId, action: "update" };
    }
    emit("runtimeWait", { runtimeName: plan.runtimeName, status: listed.status });
    const ready = await this.#waitReady(plan, runtimeId);
    const afterWait = decideRuntime({
      name: plan.name,
      runtimeName: plan.runtimeName,
      listed: { ...listed, status: ready.status },
      details: ready,
      desired,
    });
    if (afterWait === "update") {
      await this.#update(plan, runtimeId, desired, emit);
      return { runtimeId, action: "update" };
    }
    return { runtimeId, action: "wait" };
  }

  #writeRequest(plan: AgentCoreDeployPlan, desired: DesiredRuntime) {
    return {
      containerUri: desired.containerUri,
      roleArn: desired.roleArn,
      environmentVariables: desired.environmentVariables,
      idleRuntimeSessionTimeout: desired.idleRuntimeSessionTimeout,
      maxLifetime: desired.maxLifetime,
      description: runtimeDescriptionFor(plan.name),
    };
  }

  async #create(plan: AgentCoreDeployPlan, desired: DesiredRuntime, emit: Emit): Promise<Executed> {
    emit("runtimeCreate", { runtimeName: plan.runtimeName });
    const unsigned = {
      ...this.#writeRequest(plan, desired),
      runtimeName: plan.runtimeName,
      tags: stackTags(AGENTCORE_STACK_COMPONENT, plan.tags),
    };
    const request: RuntimeWriteRequest = { ...unsigned, clientToken: clientTokenFor(unsigned) };
    let runtimeId: string;
    try {
      runtimeId = (await this.#control.createRuntime(request)).runtimeId;
    } catch (error) {
      if (!(error instanceof RuntimeConflictError)) {
        throw error;
      }
      return this.#updateAfterConflict(plan, desired, emit);
    }
    await this.#waitReady(plan, runtimeId);
    emit("runtimeRequireMmdsV2", { runtimeName: plan.runtimeName });
    await this.#enforceMmds(plan, runtimeId, desired);
    return { runtimeId, action: "create" };
  }

  /** Otro despliegue creó el runtime a la vez: se vuelve a listar y se actualiza el que ganó, nunca se crea un duplicado. */
  async #updateAfterConflict(
    plan: AgentCoreDeployPlan,
    desired: DesiredRuntime,
    emit: Emit,
  ): Promise<Executed> {
    const existing = await this.#find(plan.runtimeName);
    if (existing === undefined || !isOwned(existing, plan.name)) {
      throw new AgentCoreError("name_conflict", { runtimeName: plan.runtimeName, name: plan.name });
    }
    await this.#update(plan, existing.runtimeId, desired, emit);
    return { runtimeId: existing.runtimeId, action: "update" };
  }

  async #update(
    plan: AgentCoreDeployPlan,
    runtimeId: string,
    desired: DesiredRuntime,
    emit: Emit,
  ): Promise<void> {
    emit("runtimeUpdate", { runtimeName: plan.runtimeName });
    await this.#enforceMmds(plan, runtimeId, desired);
  }

  /**
   * `UpdateAgentRuntime` con la configuración completa y `requireMMDSV2`. El
   * `clientToken` incluye la versión que se ve ahora: un reintento de la misma
   * petición lo repite, pero volver a una configuración anterior después de
   * otra versión no se confunde con la petición vieja.
   */
  async #enforceMmds(
    plan: AgentCoreDeployPlan,
    runtimeId: string,
    desired: DesiredRuntime,
  ): Promise<void> {
    const current = await this.#control.getRuntime(runtimeId);
    const unsigned = {
      ...this.#writeRequest(plan, desired),
      runtimeId,
      requireMMDSV2: true as const,
    };
    await this.#control.updateRuntime({
      ...unsigned,
      clientToken: clientTokenFor({ ...unsigned, baseVersion: current?.runtimeVersion ?? "" }),
    });
    await this.#waitReady(plan, runtimeId);
  }

  async #waitReady(plan: AgentCoreDeployPlan, runtimeId: string): Promise<RuntimeDetails> {
    const started = this.#clock.now();
    for (;;) {
      const details = await this.#control.getRuntime(runtimeId);
      if (details === undefined) {
        throw new AgentCoreError("runtime_not_found", { name: plan.name, region: plan.region });
      }
      if (details.status === RUNTIME_READY_STATE) {
        return details;
      }
      if (RUNTIME_FAILED_STATES.has(details.status)) {
        throw new AgentCoreError("runtime_failed", {
          runtimeName: plan.runtimeName,
          status: details.status,
          logGroup: runtimeLogGroupFor(runtimeId),
        });
      }
      if (this.#clock.now() - started >= AGENTCORE_READY_TIMEOUT_SECONDS * MS_PER_SECOND) {
        throw new AgentCoreError("runtime_timeout", {
          runtimeName: plan.runtimeName,
          status: details.status,
          seconds: AGENTCORE_READY_TIMEOUT_SECONDS,
        });
      }
      await this.#clock.sleep(AGENTCORE_READY_POLL_SECONDS * MS_PER_SECOND);
    }
  }
}

/**
 * `SandboxLease`: el único sandbox de un proceso servidor (espejo de
 * `rayito.mcp._lease`). Se crea de forma perezosa en la primera herramienta
 * que lo necesita (dos primeras llamadas concurrentes comparten una sola
 * creación y un solo MicroVM), la auto-suspensión la hace AWS con el
 * `idlePolicy` del sandbox (ningún timer aquí) y `close()` lo termina cuando
 * el servidor acaba.
 */

import type { ControlPlane } from "../aws/control-plane.js";
import {
  AuthenticationError,
  CapacityError,
  QuotaExceededError,
  SandboxError,
  SandboxNotFoundError,
} from "../errors.js";
import type { Logger } from "../logger.js";
import { TEMPLATE_ENV_VAR } from "../sandbox/launch.js";
import { Sandbox } from "../sandbox/sandbox.js";
import type { TransportSettings } from "../transport/transport.js";
import { MS_PER_SECOND } from "../units.js";
import { describeAwsFailure, isAwsSdkFailure } from "./failures.js";
import type { McpSettings } from "./settings.js";

export const CREATION_FAILURE_PREFIX = "no se pudo crear el sandbox: ";
const MCP_INGRESS = ["ALL_INGRESS"] as const;

/** `RAYITO_TEMPLATE` no está definido y una herramienta lo necesita. */
export class MissingTemplateError extends Error {
  constructor() {
    super(
      `${TEMPLATE_ENV_VAR} no está definido: configura la imagen (nombre o ARN) ` +
        "en el entorno del servidor",
    );
    this.name = "MissingTemplateError";
  }
}

/** `Sandbox.create` falló; `cause` es el error del SDK y el lease queda vacío para que la siguiente llamada reintente. */
export class SandboxCreationError extends Error {
  constructor(cause: unknown) {
    super(`${CREATION_FAILURE_PREFIX}${describeFailure(cause)}`, { cause });
    this.name = "SandboxCreationError";
  }
}

/** Lo que el servidor inyecta en el SDK; sólo lo cambian los tests (rayd falso). */
export interface SandboxEnvironment {
  readonly controlPlane?: ControlPlane | undefined;
  readonly transport?: Partial<TransportSettings> | undefined;
  readonly logger?: Logger | undefined;
}

function isCreationFailure(error: unknown): boolean {
  if (
    error instanceof SandboxError ||
    error instanceof AuthenticationError ||
    error instanceof CapacityError ||
    error instanceof QuotaExceededError
  ) {
    return true;
  }
  return error instanceof Error && isAwsSdkFailure(error);
}

function describeFailure(cause: unknown): string {
  if (!(cause instanceof Error)) {
    return String(cause);
  }
  return isAwsSdkFailure(cause) ? describeAwsFailure(cause) : cause.message;
}

export class SandboxLease {
  readonly settings: McpSettings;
  readonly #environment: SandboxEnvironment;
  #sandbox: Sandbox | undefined;
  #creating: Promise<Sandbox> | undefined;

  constructor(settings: McpSettings, environment: SandboxEnvironment = {}) {
    this.settings = settings;
    this.#environment = environment;
  }

  get sandboxId(): string | undefined {
    return this.#sandbox?.sandboxId;
  }

  requireTemplate(): string {
    if (this.settings.template === undefined) {
      throw new MissingTemplateError();
    }
    return this.settings.template;
  }

  /** El sandbox del proceso, creándolo si aún no existe. */
  async acquire(): Promise<Sandbox> {
    if (this.#sandbox !== undefined) {
      return this.#sandbox;
    }
    this.#creating ??= this.#create().finally(() => {
      this.#creating = undefined;
    });
    this.#sandbox = await this.#creating;
    return this.#sandbox;
  }

  /** El sandbox si existe; nunca crea uno. */
  peek(): Sandbox | undefined {
    return this.#sandbox;
  }

  /**
   * Olvida un sandbox que el SDK reportó como terminal
   * (`SandboxNotFoundError`: expiró o lo mataron fuera). El MicroVM ya no
   * existe, así que sólo se liberan los canales; la siguiente `acquire()`
   * crea otro.
   */
  async reset(): Promise<void> {
    await this.#settleCreation();
    const stale = this.#sandbox;
    this.#sandbox = undefined;
    stale?.close();
  }

  /** `kill()` y liberación una sola vez; nada si nunca hubo sandbox. */
  async close(): Promise<void> {
    await this.#settleCreation();
    const sandbox = this.#sandbox;
    this.#sandbox = undefined;
    if (sandbox === undefined) {
      return;
    }
    this.#environment.logger?.info?.("terminando el sandbox", { sandboxId: sandbox.sandboxId });
    try {
      await sandbox.kill();
    } catch (error) {
      if (!(error instanceof SandboxNotFoundError)) {
        throw error;
      }
    } finally {
      sandbox.close();
    }
  }

  async #settleCreation(): Promise<void> {
    await this.#creating?.catch(() => undefined);
  }

  async #create(): Promise<Sandbox> {
    const template = this.requireTemplate();
    const logger = this.#environment.logger;
    logger?.info?.("creando el sandbox", { template });
    try {
      const sandbox = await Sandbox.create({
        template,
        templateVersion: this.settings.templateVersion,
        timeoutMs: this.settings.timeoutSeconds * MS_PER_SECOND,
        idle: this.settings.idlePolicy,
        executionRoleArn: this.settings.executionRoleArn,
        ingress: MCP_INGRESS,
        logging: this.settings.logging,
        controlPlane: this.#environment.controlPlane,
        transport: this.#environment.transport,
        logger,
      });
      logger?.info?.("sandbox listo", { sandboxId: sandbox.sandboxId });
      return sandbox;
    } catch (error) {
      if (!isCreationFailure(error)) {
        throw error;
      }
      logger?.warn?.("no se pudo crear el sandbox", { error: errorName(error) });
      throw new SandboxCreationError(error);
    }
  }
}

function errorName(error: unknown): string {
  return error instanceof Error ? error.name : typeof error;
}

/**
 * Plano de control falso: implementa el puerto `ControlPlane` con estados
 * de `getMicrovm` guionizados (una cola; el último se repite), llamadas
 * grabadas y un contador de `createAuthToken`. Sirve a todos los tests de
 * `Sandbox`; el adaptador real se prueba en `aws.test.ts` con un grabador.
 */

import type {
  ControlPlane,
  LaunchRequest,
  ListMicrovmsOptions,
  PortSpec,
} from "../../../src/aws/control-plane.js";
import { SandboxNotFoundError, SandboxStateError } from "../../../src/errors.js";
import { TERMINAL_STATES } from "../../../src/limits.js";
import {
  type IdlePolicy,
  type SandboxInfo,
  type SandboxListItem,
  sandboxInfo,
  sandboxListItem,
} from "../../../src/models.js";

export const REGION = "us-east-1";
export const ACCOUNT_ID = "123456789012";
export const IMAGE_NAME = "rayito-base-2gb";
export const IMAGE_ARN = `arn:aws:lambda:${REGION}:${ACCOUNT_ID}:microvm-image:${IMAGE_NAME}`;
export const SANDBOX_ID = "microvm-00000000-0000-0000-0000-000000000001";
export const JWE = "eyJhbGciOiJkaXIiLCJlbmMiOiJBMjU2R0NNIn0..fake.jwe";
export const STARTED_AT = new Date(Date.UTC(2026, 8, 15, 14, 39, 2));

export interface FakeControlPlaneOptions {
  readonly endpoint: string;
  readonly states?: readonly string[];
  readonly idle?: IdlePolicy | undefined;
  readonly jwe?: string;
}

export interface RecordedCall {
  readonly operation: string;
  readonly sandboxId: string;
  readonly ports?: readonly PortSpec[];
}

export class FakeControlPlane implements ControlPlane {
  readonly region = REGION;
  readonly endpoint: string;
  readonly calls: RecordedCall[] = [];
  readonly launches: LaunchRequest[] = [];
  readonly listed: SandboxListItem[] = [];
  #states: string[];
  #idle: IdlePolicy | undefined;
  #jwe: string;
  #mints = 0;
  stateReason: string | undefined;
  suspendConflicts = false;
  resumeConflicts = false;
  terminateMissing = false;
  getMicrovmError: Error | undefined;
  listMicrovmsError: Error | undefined;
  runMicrovmError: Error | undefined;
  /** Ids que `runMicrovm` asigna en orden (`SANDBOX_ID` cuando se agota); `reincarnate()` necesita dos. */
  readonly sandboxIds: string[] = [];
  #lastSandboxId = SANDBOX_ID;

  constructor(options: FakeControlPlaneOptions) {
    this.endpoint = options.endpoint;
    this.#states = [...(options.states ?? ["PENDING"])];
    this.#idle = options.idle;
    this.#jwe = options.jwe ?? JWE;
  }

  /** Cola de estados para `getMicrovm`: se consume uno por llamada y el último se repite. */
  setStates(states: readonly string[]): void {
    this.#states = [...states];
  }

  get currentState(): string {
    return this.#states[0] ?? "RUNNING";
  }

  get idle(): IdlePolicy | undefined {
    return this.#idle;
  }

  set idle(value: IdlePolicy | undefined) {
    this.#idle = value;
  }

  get mints(): number {
    return this.#mints;
  }

  set jwe(value: string) {
    this.#jwe = value;
  }

  get jwe(): string {
    return this.#jwe;
  }

  callsTo(operation: string): RecordedCall[] {
    return this.calls.filter((call) => call.operation === operation);
  }

  info(state = this.currentState, sandboxId = this.#lastSandboxId): SandboxInfo {
    return sandboxInfo({
      sandboxId,
      state,
      endpoint: this.endpoint,
      template: IMAGE_ARN,
      templateVersion: "1.0",
      startedAt: STARTED_AT,
      maximumDurationSeconds: 3600,
      stateReason: this.stateReason,
      idle: this.#idle,
    });
  }

  async resolveTemplateArn(template: string): Promise<string> {
    this.calls.push({ operation: "resolveTemplateArn", sandboxId: template });
    return template.startsWith("arn:")
      ? template
      : `arn:aws:lambda:${REGION}:${ACCOUNT_ID}:microvm-image:${template}`;
  }

  async runMicrovm(request: LaunchRequest): Promise<SandboxInfo> {
    if (this.runMicrovmError !== undefined) {
      this.calls.push({ operation: "runMicrovm", sandboxId: "" });
      throw this.runMicrovmError;
    }
    this.#lastSandboxId = this.sandboxIds.shift() ?? SANDBOX_ID;
    this.calls.push({ operation: "runMicrovm", sandboxId: this.#lastSandboxId });
    this.launches.push(request);
    this.#idle = request.idle;
    return this.info("PENDING");
  }

  async getMicrovm(sandboxId: string): Promise<SandboxInfo> {
    this.calls.push({ operation: "getMicrovm", sandboxId });
    if (this.getMicrovmError !== undefined) {
      throw this.getMicrovmError;
    }
    const state = this.#states.length > 1 ? (this.#states.shift() as string) : this.currentState;
    return this.info(state, sandboxId);
  }

  async *listMicrovms(_options: ListMicrovmsOptions = {}): AsyncIterable<SandboxListItem> {
    this.calls.push({ operation: "listMicrovms", sandboxId: "" });
    if (this.listMicrovmsError !== undefined) {
      throw this.listMicrovmsError;
    }
    const wanted = _options.states === undefined ? undefined : new Set(_options.states);
    for (const item of this.listed) {
      const keep = wanted === undefined ? !TERMINAL_STATES.has(item.state) : wanted.has(item.state);
      if (keep) {
        yield item;
      }
    }
  }

  async terminateMicrovm(sandboxId: string): Promise<boolean> {
    this.calls.push({ operation: "terminateMicrovm", sandboxId });
    if (this.terminateMissing) {
      return false;
    }
    this.#states = ["TERMINATED"];
    return true;
  }

  async suspendMicrovm(sandboxId: string): Promise<boolean> {
    this.calls.push({ operation: "suspendMicrovm", sandboxId });
    if (this.suspendConflicts) {
      return false;
    }
    return true;
  }

  async resumeMicrovm(sandboxId: string): Promise<boolean> {
    this.calls.push({ operation: "resumeMicrovm", sandboxId });
    if (this.resumeConflicts) {
      return false;
    }
    return true;
  }

  async createAuthToken(sandboxId: string, ports: readonly PortSpec[]): Promise<string> {
    this.calls.push({ operation: "createAuthToken", sandboxId, ports });
    this.#mints += 1;
    return `${this.#jwe}.${this.#mints}`;
  }

  addListed(sandboxId: string, state: string): void {
    this.listed.push(
      sandboxListItem({
        sandboxId,
        state,
        template: IMAGE_ARN,
        templateVersion: "1.0",
        startedAt: STARTED_AT,
      }),
    );
  }
}

export { SandboxNotFoundError, SandboxStateError };

/**
 * `StackProvisioner` falso en memoria (M15 foundations): espejo de
 * `tests/unit/fake_stacks.py`. Compartido por foundations y por cada
 * feature que necesite probar su propia fachada sin tocar AWS.
 */

import { StackError } from "../../src/errors.js";
import {
  describeFailedResource,
  firstFailedResource,
  type StackComponent,
  type StackEvent,
  type StackStatus,
} from "../../src/stacks/model.js";
import type { DeployTarget, StackProvisioner, UpdateOutcome } from "../../src/stacks/port.js";

export class FakeStackProvisioner implements StackProvisioner {
  readonly stacks = new Map<string, StackStatus>();
  readonly artifacts = new Map<string, Uint8Array>();
  readonly calls: Array<readonly [string, ...unknown[]]> = [];
  nextUpdateOutcome: UpdateOutcome = "changed";
  /** Los `parameters`/`keepPrevious` de la última `create`/`update`. */
  sentParameters: Readonly<Record<string, string>> = {};
  sentKeepPrevious: readonly string[] = [];
  /** Las `tags` de la última `create`/`update`. */
  sentTags: Readonly<Record<string, string>> = {};
  failWait = false;
  /** Si se pone, `wait()` deja la pila en ese estado y lanza el `StackError`
   * (`code: "failed"`, `state`) del adaptador real. */
  failWaitState: string | undefined;
  /** `create()`/`update()` rechazan la llamada, como un `AccessDenied`:
   * `StackError` con `code: "failed"` y sin `state`. */
  failCall = false;
  /** `stackName -> eventos` (del más nuevo al más antiguo) que lee `failureReason`. */
  readonly events = new Map<string, StackEvent[]>();
  /** Cada `timeoutMs` que `wait()` recibió, en orden; espejo de
   * `fake_stacks.FakeStackProvisioner.wait_timeouts`. */
  readonly waitTimeoutsMs: number[] = [];

  async describe(stackName: string): Promise<StackStatus | undefined> {
    this.calls.push(["describe", stackName]);
    return this.stacks.get(stackName);
  }

  async create(
    _component: StackComponent,
    options: {
      readonly stackName: string;
      readonly parameters: Readonly<Record<string, string>>;
      readonly tags?: Readonly<Record<string, string>>;
    },
  ): Promise<void> {
    this.calls.push(["create", options.stackName]);
    if (this.failCall) {
      throw new StackError("llamada rechazada", { code: "failed" });
    }
    this.sentTags = { ...options.tags };
    this.sentParameters = { ...options.parameters };
    this.sentKeepPrevious = [];
    this.stacks.set(options.stackName, {
      name: options.stackName,
      state: "CREATE_COMPLETE",
      outputs: { StackName: options.stackName },
      parameters: { ...options.parameters },
      tags: { ...options.tags },
    });
  }

  async update(
    _component: StackComponent,
    options: {
      readonly stackName: string;
      readonly parameters: Readonly<Record<string, string>>;
      readonly keepPrevious?: readonly string[];
      readonly tags?: Readonly<Record<string, string>>;
    },
  ): Promise<UpdateOutcome> {
    this.calls.push(["update", options.stackName]);
    if (this.failCall) {
      throw new StackError("llamada rechazada", { code: "failed" });
    }
    this.sentTags = { ...options.tags };
    const keepPrevious = options.keepPrevious ?? [];
    this.sentParameters = { ...options.parameters };
    this.sentKeepPrevious = keepPrevious;
    if (this.nextUpdateOutcome === "changed") {
      const existing = this.stacks.get(options.stackName);
      const previous = existing?.parameters ?? {};
      const merged: Record<string, string> = {};
      for (const key of keepPrevious) {
        const value = previous[key];
        if (value !== undefined) {
          merged[key] = value;
        }
      }
      this.stacks.set(options.stackName, {
        name: options.stackName,
        state: "UPDATE_COMPLETE",
        outputs: existing?.outputs ?? {},
        parameters: { ...merged, ...options.parameters },
        tags: { ...options.tags },
      });
    }
    return this.nextUpdateOutcome;
  }

  async delete(stackName: string): Promise<void> {
    this.calls.push(["delete", stackName]);
    this.stacks.delete(stackName);
  }

  async wait(stackName: string, target: DeployTarget, timeoutMs: number): Promise<void> {
    this.calls.push(["wait", stackName, target]);
    this.waitTimeoutsMs.push(timeoutMs);
    if (this.failWait) {
      throw new StackError(`tiempo agotado esperando ${JSON.stringify(stackName)}`, {
        code: "in_progress",
      });
    }
    if (this.failWaitState !== undefined) {
      const existing = this.stacks.get(stackName);
      this.stacks.set(stackName, {
        name: stackName,
        outputs: {},
        ...existing,
        state: this.failWaitState,
      });
      throw new StackError(
        `la pila ${JSON.stringify(stackName)} terminó en ${this.failWaitState}`,
        { code: "failed", state: this.failWaitState },
      );
    }
  }

  async putArtifact(bucket: string, key: string, data: Uint8Array): Promise<void> {
    this.calls.push(["putArtifact", bucket, key]);
    this.artifacts.set(`${bucket}/${key}`, data);
  }

  async failureReason(stackName: string): Promise<string | undefined> {
    this.calls.push(["failureReason", stackName]);
    const failed = firstFailedResource(this.events.get(stackName) ?? [], stackName);
    return failed === undefined
      ? this.stacks.get(stackName)?.reasonCode
      : describeFailedResource(failed);
  }
}

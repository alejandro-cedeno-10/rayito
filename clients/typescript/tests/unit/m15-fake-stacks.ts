/**
 * `StackProvisioner` falso en memoria (M15 foundations): espejo de
 * `tests/unit/fake_stacks.py`. Compartido por foundations y por cada
 * feature que necesite probar su propia fachada sin tocar AWS.
 */

import { StackError } from "../../src/errors.js";
import type { StackComponent, StackStatus } from "../../src/stacks/model.js";
import type { DeployTarget, StackProvisioner, UpdateOutcome } from "../../src/stacks/port.js";

export class FakeStackProvisioner implements StackProvisioner {
  readonly stacks = new Map<string, StackStatus>();
  readonly artifacts = new Map<string, Uint8Array>();
  readonly calls: Array<readonly [string, ...unknown[]]> = [];
  nextUpdateOutcome: UpdateOutcome = "changed";
  failWait = false;
  /** Cada `timeoutMs` que `wait()` recibió, en orden; espejo de
   * `fake_stacks.FakeStackProvisioner.wait_timeouts`. */
  readonly waitTimeoutsMs: number[] = [];

  async describe(stackName: string): Promise<StackStatus | undefined> {
    this.calls.push(["describe", stackName]);
    return this.stacks.get(stackName);
  }

  async create(_component: StackComponent, options: { readonly stackName: string }): Promise<void> {
    this.calls.push(["create", options.stackName]);
    this.stacks.set(options.stackName, {
      name: options.stackName,
      state: "CREATE_COMPLETE",
      outputs: { StackName: options.stackName },
    });
  }

  async update(
    _component: StackComponent,
    options: { readonly stackName: string },
  ): Promise<UpdateOutcome> {
    this.calls.push(["update", options.stackName]);
    if (this.nextUpdateOutcome === "changed") {
      const existing = this.stacks.get(options.stackName);
      this.stacks.set(options.stackName, {
        name: options.stackName,
        state: "UPDATE_COMPLETE",
        outputs: existing?.outputs ?? {},
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
  }

  async putArtifact(bucket: string, key: string, data: Uint8Array): Promise<void> {
    this.calls.push(["putArtifact", bucket, key]);
    this.artifacts.set(`${bucket}/${key}`, data);
  }

  async failureReason(stackName: string): Promise<string | undefined> {
    this.calls.push(["failureReason", stackName]);
    return this.stacks.get(stackName)?.reasonCode;
  }
}

/**
 * M14 contra AWS real (`RAYITO_E2E=1` y `RAYITO_E2E_INDEX_TABLE` con la tabla
 * desplegada desde `infra/metadata-index.yaml`): tres sandboxes con
 * `index`, dos en pausa; `Sandbox.list({ metadata, states: ["SUSPENDED"],
 * index })` devuelve exactamente esos dos y, después, `get-microvm` muestra
 * que siguen `SUSPENDED` (el listado no los despertó). Luego se reanuda uno:
 * su `startedAt` no cambia (±1 s, IDX-1 c) y el listado con índice lo sigue
 * encontrando, ya `RUNNING`. Coste: tres sandboxes
 * durante < 5 min (~$0,05) + 3 `PutItem` + 1 `BatchGetItem` (< $0,00001).
 */

import { randomBytes } from "node:crypto";
import { describe, expect, test } from "vitest";
import { DynamoDbIndex, Sandbox } from "../../src/index.js";
import { e2eEnabled, TEST_SANDBOX_TIMEOUT_MS, useE2E } from "./helpers.js";

const INDEX_TABLE_VAR = "RAYITO_E2E_INDEX_TABLE";

describe.runIf(e2eEnabled())("M14 metadata index (AWS real)", () => {
  const e2e = useE2E();

  test("list by metadata over suspended sandboxes without waking them", async () => {
    const tableName = process.env[INDEX_TABLE_VAR];
    if (!tableName) {
      throw new Error(`este e2e necesita ${INDEX_TABLE_VAR} (despliega infra/metadata-index.yaml)`);
    }
    const index = new DynamoDbIndex({ tableName, region: e2e.controlPlane.region });
    const run = randomBytes(6).toString("hex");
    const sandboxes: Sandbox[] = [];
    for (let n = 0; n < 3; n += 1) {
      const sandbox = await Sandbox.create({
        template: e2e.templateArn,
        timeoutMs: TEST_SANDBOX_TIMEOUT_MS,
        idle: { maxIdleSeconds: 600, autoResume: true },
        controlPlane: e2e.controlPlane,
        metadata: { suite: "m14", run },
        index,
      });
      e2e.created.push(sandbox);
      sandboxes.push(sandbox);
    }
    const paused = sandboxes.slice(0, 2);
    const started = new Map<string, number>();
    for (const sandbox of paused) {
      const info = await e2e.controlPlane.getMicrovm(sandbox.sandboxId);
      started.set(sandbox.sandboxId, info.startedAt.getTime());
    }
    for (const sandbox of paused) {
      expect(await sandbox.pause()).toBe(true);
    }
    const found = [];
    for await (const item of Sandbox.list({
      metadata: { run },
      states: ["SUSPENDED"],
      index,
      controlPlane: e2e.controlPlane,
    })) {
      found.push(item);
    }
    expect(found.map((item) => item.sandboxId).sort()).toEqual(
      paused.map((sandbox) => sandbox.sandboxId).sort(),
    );
    for (const sandbox of paused) {
      expect((await e2e.controlPlane.getMicrovm(sandbox.sandboxId)).state).toBe("SUSPENDED");
    }

    const resumed = paused[0] as Sandbox;
    await resumed.resume();
    const after = (await e2e.controlPlane.getMicrovm(resumed.sandboxId)).startedAt.getTime();
    expect(Math.abs(after - (started.get(resumed.sandboxId) ?? 0))).toBeLessThanOrEqual(1000);
    const running: string[] = [];
    for await (const item of Sandbox.list({
      metadata: { run },
      states: ["RUNNING"],
      index,
      controlPlane: e2e.controlPlane,
    })) {
      running.push(item.sandboxId);
    }
    expect(running).toContain(resumed.sandboxId);
  });
});

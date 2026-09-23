// Sigue https://docs.e2b.dev/quickstart, https://docs.e2b.dev/sandbox/metadata y https://docs.e2b.dev/sandbox/list.
import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { Sandbox } from "rayito/e2b";

const run = randomUUID();
const sbx = await Sandbox.create(process.env.RAYITO_TEMPLATE, {
  metadata: { corpus: "create-info-list", run },
  timeoutMs: 300_000,
});
let killed = false;
try {
  const execution = await sbx.runCode("x = 1; x + 1");
  assert.equal(execution.text, "2");

  const info = await Sandbox.getInfo(sbx.sandboxId);
  assert.equal(info.sandboxId, sbx.sandboxId);
  assert.equal(info.state, "running");
  assert.deepEqual(info.metadata, { corpus: "create-info-list", run });
  assert.ok(info.templateId);
  assert.ok(info.endAt instanceof Date && info.endAt > info.startedAt);
  assert.deepEqual(info.volumeMounts, []);

  const paginator = Sandbox.list({ query: { metadata: { run } } });
  const found = [];
  do {
    for (const item of await paginator.nextItems()) {
      found.push(item.sandboxId);
    }
  } while (paginator.hasNext);
  assert.deepEqual(found, [sbx.sandboxId]);
} finally {
  killed = await Sandbox.kill(sbx.sandboxId);
}
assert.equal(killed, true);
console.log("create-info-list ok");

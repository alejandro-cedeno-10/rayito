// Sigue https://docs.e2b.dev/sandbox/persistence y https://docs.e2b.dev/sandbox/connect.
import assert from "node:assert/strict";
import { Sandbox } from "rayito/e2b";

const sbx = await Sandbox.create();
try {
  await sbx.runCode("counter = 41");
  assert.equal(await sbx.pause(), true);
  assert.equal(await sbx.pause(), false);

  const resumed = await Sandbox.connect(sbx.sandboxId);
  assert.equal((await resumed.runCode("counter + 1")).text, "42");
} finally {
  await sbx.kill();
}
console.log("pause ok");

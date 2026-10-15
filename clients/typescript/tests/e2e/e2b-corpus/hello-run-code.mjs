// Sigue https://docs.e2b.dev/quickstart ("Running your first Sandbox").
import assert from "node:assert/strict";
import { Sandbox } from "rayito/e2b";

const sbx = await Sandbox.create();
try {
  const execution = await sbx.runCode("x = 1; x + 1");
  assert.equal(execution.text, "2");
} finally {
  await sbx.kill();
}
console.log("hello-run-code ok");

// Sigue https://docs.e2b.dev/client ("SDK client").
import assert from "node:assert/strict";
import { E2B } from "rayito/e2b";

const client = new E2B();
const sbx = await client.Sandbox.create();
try {
  assert.equal((await sbx.runCode("1 + 1")).text, "2");
} finally {
  await sbx.kill();
}
console.log("bound-client ok");

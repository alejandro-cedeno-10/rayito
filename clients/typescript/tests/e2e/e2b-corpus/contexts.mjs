// Sigue https://docs.e2b.dev/code-interpreting/contexts.
import assert from "node:assert/strict";
import { Sandbox } from "rayito/e2b";

const sbx = await Sandbox.create();
try {
  const context = await sbx.createCodeContext();
  assert.equal((await sbx.runCode("value = 7", { context })).error, undefined);
  assert.equal((await sbx.runCode("value * 6", { context })).text, "42");
  assert.ok((await sbx.listCodeContexts()).some((item) => item.id === context.id));

  await sbx.restartCodeContext(context);
  const afterRestart = await sbx.runCode("value", { context });
  assert.equal(afterRestart.error?.name, "NameError");

  await sbx.removeCodeContext(context);
  assert.ok(!(await sbx.listCodeContexts()).some((item) => item.id === context.id));
} finally {
  await sbx.kill();
}
console.log("contexts ok");

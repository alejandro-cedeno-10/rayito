// Sigue https://docs.e2b.dev/quickstart (runCode) con la cancelación estándar de JS (AbortController).
import assert from "node:assert/strict";
import { Sandbox } from "rayito/e2b";

const sbx = await Sandbox.create();
try {
  const controller = new AbortController();
  const reason = new Error("corpus: cancelado a los 2 s");
  const timer = setTimeout(() => controller.abort(reason), 2000);
  const started = performance.now();
  await assert.rejects(
    sbx.runCode("import time; time.sleep(60)", { signal: controller.signal }),
    (error) => error === reason,
  );
  clearTimeout(timer);
  const elapsed = performance.now() - started;
  assert.ok(elapsed < 10_000, `runCode rechazó en ${elapsed} ms`);

  const next = await sbx.runCode("1+1");
  assert.equal(next.text, "2");
} finally {
  await sbx.kill();
}
console.log("abort-run-code ok");

// Sigue https://docs.e2b.dev/commands y https://docs.e2b.dev/commands/background.
import assert from "node:assert/strict";
import { Sandbox } from "rayito/e2b";

const sbx = await Sandbox.create();
try {
  const result = await sbx.commands.run("echo hi");
  assert.equal(result.stdout, "hi\n");
  assert.equal(result.exitCode, 0);

  const lines = [];
  const command = await sbx.commands.run("sleep 1; echo done", {
    background: true,
    onStdout: (data) => lines.push(data),
  });
  const running = (await sbx.commands.list()).map((process) => process.pid);
  assert.ok(running.includes(command.pid), `pid ${command.pid} no está en ${running}`);

  const finished = await command.wait();
  assert.equal(finished.exitCode, 0);
  assert.equal(lines.join(""), "done\n");
} finally {
  await sbx.kill();
}
console.log("commands ok");

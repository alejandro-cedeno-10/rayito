// Sigue https://docs.e2b.dev/sandbox/pty ("Interactive terminal (PTY)").
import assert from "node:assert/strict";
import { setTimeout as sleep } from "node:timers/promises";
import { Sandbox } from "rayito/e2b";

const marker = "hola-2";
const sbx = await Sandbox.create();
try {
  const decoder = new TextDecoder();
  let output = "";
  const terminal = await sbx.pty.create({
    cols: 80,
    rows: 24,
    onData: (data) => {
      output += decoder.decode(data, { stream: true });
    },
  });
  const finished = terminal.wait().catch(() => undefined);
  await sbx.pty.sendInput(terminal.pid, new TextEncoder().encode("echo hola-$((1+1))\n"));
  const deadline = Date.now() + 30_000;
  while (!output.includes(marker) && Date.now() < deadline) {
    await sleep(250);
  }
  assert.ok(output.includes(marker), `sin ${marker} en la PTY: ${JSON.stringify(output)}`);
  assert.equal(await sbx.pty.kill(terminal.pid), true);
  await finished;
} finally {
  await sbx.kill();
}
console.log("pty ok");

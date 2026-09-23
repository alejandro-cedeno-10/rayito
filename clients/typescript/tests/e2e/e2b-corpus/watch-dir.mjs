// Sigue https://docs.e2b.dev/filesystem/watch.
import assert from "node:assert/strict";
import { setTimeout as sleep } from "node:timers/promises";
import { FilesystemEventType, Sandbox } from "rayito/e2b";

const watched = "/home/user/watched";
const sbx = await Sandbox.create();
try {
  await sbx.files.makeDir(watched);
  const events = [];
  const handle = await sbx.files.watchDir(watched, (event) => {
    events.push(event);
  });
  await sbx.files.write(`${watched}/note.txt`, "hola");
  const wrote = () =>
    events.some((event) => event.name === "note.txt" && event.type === FilesystemEventType.WRITE);
  const deadline = Date.now() + 15_000;
  while (!wrote() && Date.now() < deadline) {
    await sleep(250);
  }
  await handle.stop();
  assert.ok(wrote(), `sin evento write de note.txt: ${JSON.stringify(events)}`);
} finally {
  await sbx.kill();
}
console.log("watch-dir ok");

// Sigue https://docs.e2b.dev/filesystem/read-write.
import assert from "node:assert/strict";
import { Sandbox } from "rayito/e2b";

const sbx = await Sandbox.create();
try {
  const written = await sbx.files.writeFiles([
    { path: "/home/user/corpus/a.txt", data: "alpha" },
    { path: "/home/user/corpus/b.txt", data: new TextEncoder().encode("beta") },
  ]);
  assert.deepEqual(
    written.map((info) => info.name),
    ["a.txt", "b.txt"],
  );

  assert.equal(await sbx.files.read("/home/user/corpus/a.txt"), "alpha");
  const bytes = await sbx.files.read("/home/user/corpus/b.txt", { format: "bytes" });
  assert.equal(new TextDecoder().decode(bytes), "beta");

  const names = (await sbx.files.list("/home/user/corpus")).map((entry) => entry.name).sort();
  assert.deepEqual(names, ["a.txt", "b.txt"]);

  assert.equal(await sbx.files.exists("/home/user/corpus/a.txt"), true);
  await sbx.files.remove("/home/user/corpus/a.txt");
  assert.equal(await sbx.files.exists("/home/user/corpus/a.txt"), false);
} finally {
  await sbx.kill();
}
console.log("files ok");

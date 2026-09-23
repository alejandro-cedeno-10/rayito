// Sigue https://docs.e2b.dev/network/internet-access ("Connecting to the sandbox via a public URL").
import assert from "node:assert/strict";
import { Sandbox } from "rayito/e2b";

const sbx = await Sandbox.create();
try {
  const host = sbx.getHost(3000);
  assert.equal(typeof host, "string");
  assert.ok(host.length > 0);
  assert.equal(sbx.getHost(8080), host);
} finally {
  await sbx.kill();
}
console.log("get-host ok");

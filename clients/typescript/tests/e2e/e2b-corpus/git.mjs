// Sigue https://docs.e2b.dev/sandbox/git-integration.
import assert from "node:assert/strict";
import { Sandbox } from "rayito/e2b";

const repo = process.env.RAYITO_E2E_GIT_REPO || "https://github.com/octocat/Hello-World.git";
const path = "/home/user/hello";
const sbx = await Sandbox.create();
try {
  await sbx.git.clone(repo, { path, depth: 1, timeoutMs: 120_000 });
  const status = await sbx.git.status(path);
  assert.equal(status.isClean, true);
  assert.ok(status.currentBranch);
} finally {
  await sbx.kill();
}
console.log("git ok");

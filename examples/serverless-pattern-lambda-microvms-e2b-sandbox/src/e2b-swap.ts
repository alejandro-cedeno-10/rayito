// SPDX-License-Identifier: MIT-0
// The same program written for the E2B SDK, moved to your own AWS account by
// changing one import line:
//   - import { Sandbox } from "@e2b/code-interpreter";
//   + import { Sandbox } from "rayito/e2b";
// The image comes from RAYITO_TEMPLATE, like in sandbox.ts.
import { Sandbox } from "rayito/e2b";

const sbx = await Sandbox.create({ timeoutMs: 300_000, metadata: { pattern: "e2b-swap" } });
try {
  console.log("runCode:", (await sbx.runCode("sum(range(10))")).text);
  console.log("commands.run:", (await sbx.commands.run("echo hello from E2B code")).stdout.trim());
  await sbx.files.write("/home/user/e2b.txt", "same API");
  console.log("files.read:", await sbx.files.read("/home/user/e2b.txt"));
} finally {
  await sbx.kill();
}

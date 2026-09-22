import { expect, test } from "vitest";
import { type CommandHandle, Sandbox, type WatchHandle } from "../../src/index.js";

/**
 * El ejemplo del README compilado (no ejecutado contra AWS): `pnpm typecheck`
 * y este fichero fallan si la superficie deja de admitir el programa.
 */
async function readmeExample(): Promise<void> {
  await using sbx = await Sandbox.create({ template: "rayito-base-2gb", timeoutMs: 3_600_000 });
  console.log(await sbx.isRunning());

  console.log((await sbx.commands.run("echo hola")).stdout);
  const server: CommandHandle = await sbx.commands.run("python3 -m http.server 3000", {
    background: true,
    timeoutMs: 0,
  });
  const host = await sbx.getHost(3000);
  console.log(`https://${host}`, host.headers);
  await server.kill();

  const info = await sbx.files.write("/home/user/data.csv", "a,b\n1,2\n");
  console.log(info.size, await sbx.files.read("data.csv"));
  {
    await using watch: WatchHandle = await sbx.files.watchDir("/home/user");
    await sbx.commands.run("touch /home/user/new.txt");
    console.log(watch.getNewEvents());
  }

  await sbx.runCode("x = 42");
  console.log((await sbx.runCode("x")).text);
  const plot = await sbx.runCode(
    "import matplotlib.pyplot as plt; plt.plot([1, 2, 3]); plt.show()",
  );
  console.log(plot.results[0]?.formats());
  const failed = await sbx.runCode("1/0");
  console.log(failed.error?.name);
  const ctx = await sbx.createCodeContext({ cwd: "/tmp" });
  console.log((await sbx.runCode("x", { context: ctx })).error?.name);

  const pty = await sbx.pty.create({
    size: { cols: 120, rows: 40 },
    onData: (chunk) => process.stdout.write(chunk),
    timeoutMs: 0,
  });
  await pty.sendInput("echo hola\n");
  for await (const { pty: data } of pty) {
    if (data !== undefined && new TextDecoder().decode(data).includes("hola\r\n")) {
      break;
    }
  }
  await pty.resize({ cols: 80, rows: 24 });
  await pty.kill();

  await sbx.pause();
  await sbx.resume();
  console.log((await sbx.runCode("x")).text);
  console.log((await sbx.getHealth()).resumeGeneration);
}

test("the README example type-checks and is a function", () => {
  expect(typeof readmeExample).toBe("function");
});

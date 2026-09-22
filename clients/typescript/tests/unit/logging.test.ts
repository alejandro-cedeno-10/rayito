import { describe, expect, test } from "vitest";
import { FilesystemEventType as EventTypeProto } from "../../src/gen/rayito/v1/filesystem_pb.js";
import { ACCESS_TOKEN, createTestSandbox, useFastStateChecks, waitUntil } from "./helpers.js";

const HOME = "/home/user";

describe("logging hygiene", () => {
  useFastStateChecks();

  test("no logged string contains the JWE, the access token or the payload", async () => {
    const { sandbox, rayd, plane, logger } = await createTestSandbox({
      create: { envs: { SECRET_ENV: "super-secret-value" } },
    });
    plane.setStates(["RUNNING"]);
    await sandbox.commands.run("echo hola");
    const background = await sandbox.commands.run("seq 5", { background: true, timeoutMs: 0 });
    await sandbox.files.write(`${HOME}/f.txt`, "file-contents-are-secret");
    await sandbox.files.read(`${HOME}/f.txt`);
    const watch = await sandbox.files.watchDir(HOME);
    const pty = await sandbox.pty.create({ timeoutMs: 0 });
    await pty.sendInput("echo pty-secret\n");
    await sandbox.runCode("x = 'kernel-secret'");
    const host = await sandbox.getHost(3000);
    expect(host.headers["x-aws-proxy-auth"]).toBeDefined();
    rayd.forbidNext(1);
    await sandbox.commands.list();
    rayd.suspendResume({ unavailableCalls: 1, clockOffsetMs: 9000, kernelStateLost: true });
    await background.wait();
    await waitUntil(() => watch.reconnects === 1 && pty.reconnects >= 0);
    rayd.filesystem.emit(HOME, "after.txt", EventTypeProto.CREATE);
    await watch.stop();
    pty.disconnect();
    sandbox.close();

    const dump = logger.dump();
    expect(logger.lines.length).toBeGreaterThan(3);
    expect(dump).not.toContain(ACCESS_TOKEN);
    expect(dump).not.toContain(plane.jwe);
    expect(dump).not.toContain("super-secret-value");
    expect(dump).not.toContain("token_sha256");
    expect(dump).not.toContain("file-contents-are-secret");
    expect(dump).not.toContain("pty-secret");
    expect(dump).not.toContain("kernel-secret");
    expect(dump).not.toContain("hola");
    for (const launch of plane.launches) {
      expect(dump).not.toContain(launch.runHookPayload);
    }
    expect(logger.at("warn").some((line) => line.message.includes("desfase"))).toBe(true);
    expect(logger.at("warn").some((line) => line.message.includes("kernel"))).toBe(true);
  });
});

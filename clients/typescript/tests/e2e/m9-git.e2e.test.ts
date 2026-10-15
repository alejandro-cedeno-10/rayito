/**
 * Aceptación del módulo git de `m9-e2b-v2-surface` en TypeScript contra AWS
 * real, espejo de `clients/python/tests/e2e/test_m9_git.py` (design D23): clone
 * público superficial, operaciones locales, un remoto bare dentro del propio
 * sandbox (push y pull sin red), el error de upstream, los errores de
 * autenticación (sin credenciales y con un token falso) y la visibilidad
 * documentada de `dangerouslyAuthenticate`. El repositorio es el de
 * `RAYITO_E2E_GIT_REPO`, o uno público de ejemplo si no se define.
 *
 * Higiene: el token falso y el secreto de `dangerouslyAuthenticate` se generan
 * en cada corrida, nunca se imprimen y no pueden aparecer ni en los mensajes de
 * las excepciones ni en el logger del SDK, ni quedar en la URL del remoto.
 */

import { randomBytes } from "node:crypto";
import { describe, expect, test } from "vitest";
import { GitAuthError, GitUpstreamError, type Logger, Sandbox } from "../../src/index.js";
import { e2eEnabled, TEST_SANDBOX_TIMEOUT_MS, useE2E } from "./helpers.js";

const REPO_VAR = "RAYITO_E2E_GIT_REPO";
const DEFAULT_REPO = "https://github.com/octocat/Hello-World.git";
const HOME = "/home/user";
const HELLO = `${HOME}/hello`;
const REMOTE = `${HOME}/remote.git`;
const WORK = `${HOME}/work`;
const COPY = `${HOME}/copy`;
const NETWORK_TIMEOUT_MS = 120_000;
const CREDENTIAL_HOST = "example.invalid";
const TOKEN_BYTES = 16;
const FAKE_USERNAME = "rayito-e2e";

function note(label: string, value: string): void {
  console.log(`[m9-git] ${label}: ${value}`);
}

/** Falla sin repetir ni el secreto ni el texto: el fallo no puede copiarlos al log de CI. */
function assertAbsent(secret: string, text: string, where: string): void {
  const leaked = text.includes(secret);
  expect(leaked, `el secreto aparece en ${where}`).toBe(false);
}

describe.skipIf(!e2eEnabled())("M9 git (TypeScript)", () => {
  const context = useE2E();

  test("git sequence: clone, local operations, bare remote, upstream and auth errors", async () => {
    const fakeToken = `rayito-e2e-fake-${randomBytes(TOKEN_BYTES).toString("hex")}`;
    const storedSecret = `rayito-e2e-store-${randomBytes(TOKEN_BYTES).toString("hex")}`;
    const sdkLines: string[] = [];
    const record = (message: string, fields?: Readonly<Record<string, unknown>>): void => {
      sdkLines.push(fields === undefined ? message : `${message} ${JSON.stringify(fields)}`);
    };
    const logger: Logger = { debug: record, info: record, warn: record, error: record };
    const sandbox = await Sandbox.create({
      template: context.templateArn,
      timeoutMs: TEST_SANDBOX_TIMEOUT_MS,
      idle: null,
      executionRoleArn: context.settings.executionRoleArn,
      ingress: ["ALL_INGRESS"],
      logging: context.settings.logging,
      controlPlane: context.controlPlane,
      logger,
    });
    context.created.push(sandbox);
    const git = sandbox.git;
    const repo = process.env[REPO_VAR] || DEFAULT_REPO;

    note("git_version", (await sandbox.commands.run("git --version")).stdout.trim());
    await git.clone(repo, { path: HELLO, depth: 1, timeoutMs: NETWORK_TIMEOUT_MS });

    await git.configureUser("Rayito E2E", "e2e@example.invalid");
    expect((await git.status(HELLO)).isClean).toBe(true);
    const branches = await git.branches(HELLO);
    const defaultBranch = branches.currentBranch;
    expect(defaultBranch).toBeDefined();
    expect(branches.branches).toContain(defaultBranch);
    await git.createBranch(HELLO, "feature");
    await git.checkoutBranch(HELLO, defaultBranch as string);
    await git.checkoutBranch(HELLO, "feature");
    const after = await git.branches(HELLO);
    expect(after.currentBranch).toBe("feature");
    expect(after.branches).toContain("feature");

    await sandbox.files.write(`${HELLO}/e2e.txt`, "hola\n");
    expect((await git.status(HELLO)).hasUntracked).toBe(true);
    await git.add(HELLO);
    expect((await git.status(HELLO)).hasStaged).toBe(true);
    await git.commit(HELLO, "e2e: añade e2e.txt");
    expect((await git.status(HELLO)).isClean).toBe(true);
    await git.reset(HELLO, { mode: "soft", target: "HEAD~1" });
    expect((await git.status(HELLO)).stagedCount).toBe(1);
    await git.restore(HELLO, { paths: ["e2e.txt"], staged: true });
    const restored = await git.status(HELLO);
    expect(restored.hasStaged).toBe(false);
    expect(restored.hasUntracked).toBe(true);
    await git.setConfig("core.editor", "true", { scope: "local", path: HELLO });
    expect(await git.getConfig("core.editor", { scope: "local", path: HELLO })).toBe("true");

    await git.init(REMOTE, { bare: true, initialBranch: "main" });
    await git.init(WORK, { initialBranch: "main" });
    await sandbox.files.write(`${WORK}/one.txt`, "uno\n");
    await git.add(WORK);
    await git.commit(WORK, "e2e: primero");
    await git.remoteAdd(WORK, "origin", REMOTE);
    await git.push(WORK, { remote: "origin", branch: "main" });
    await git.clone(REMOTE, { path: COPY });
    await sandbox.files.write(`${WORK}/two.txt`, "dos\n");
    await git.add(WORK);
    await git.commit(WORK, "e2e: segundo");
    await git.push(WORK);
    await git.pull(COPY);
    const log = (await sandbox.commands.run(`git -C ${COPY} log --format=%s`)).stdout;
    expect(log.split("\n").slice(0, 2)).toEqual(["e2e: segundo", "e2e: primero"]);

    await git.createBranch(COPY, "no-upstream");
    await expect(git.pull(COPY)).rejects.toBeInstanceOf(GitUpstreamError);

    const missing = await git
      .push(HELLO, {
        remote: "origin",
        branch: defaultBranch as string,
        timeoutMs: NETWORK_TIMEOUT_MS,
      })
      .catch((error: unknown) => error);
    expect(missing).toBeInstanceOf(GitAuthError);
    note("push sin credenciales", (missing as Error).message);

    const rejected = await git
      .push(HELLO, {
        remote: "origin",
        branch: defaultBranch as string,
        username: FAKE_USERNAME,
        password: fakeToken,
        timeoutMs: NETWORK_TIMEOUT_MS,
      })
      .catch((error: unknown) => error);
    expect(rejected).toBeInstanceOf(GitAuthError);
    assertAbsent(fakeToken, String((rejected as Error).message), "la excepción");
    const remote = await git.remoteGet(HELLO, "origin");
    expect(remote).toBeDefined();
    assertAbsent(fakeToken, remote as string, "la URL del remoto");
    const config = (await sandbox.commands.run(`git -C ${HELLO} config --list`)).stdout;
    assertAbsent(fakeToken, config, "la configuración del repo");

    await git.dangerouslyAuthenticate({
      username: "e2e",
      password: storedSecret,
      host: CREDENTIAL_HOST,
    });
    const stored = (await sandbox.commands.run("cat ~/.git-credentials")).stdout;
    const visible = stored
      .split("\n")
      .some((line) => line.includes(CREDENTIAL_HOST) && line.includes(storedSecret));
    expect(visible, "~/.git-credentials no lleva la línea del host").toBe(true);

    const logs = sdkLines.join("\n");
    assertAbsent(fakeToken, logs, "el logger del SDK");
    assertAbsent(storedSecret, logs, "el logger del SDK");
  });
});

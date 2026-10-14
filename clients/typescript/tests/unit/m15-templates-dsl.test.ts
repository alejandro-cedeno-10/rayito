/**
 * `src/templates/dsl.ts` (m15-templates): espejo de
 * `test_m15_templates_dsl.py`.
 */

import { describe, expect, test } from "vitest";
import { InvalidArgumentError, UnimplementedError } from "../../src/errors.js";
import { NODE_PACKAGES, NPM_MISSING_EXIT_CODE, Template } from "../../src/templates/dsl.js";
import { waitForPort } from "../../src/templates/ready-cmds.js";

describe("templates/dsl", () => {
  test("the builder never mutates a previous instance", () => {
    const base = new Template();
    const withCopy = base.copy("app/", "/srv/app/");
    expect(base.spec.steps).toEqual([]);
    expect(withCopy.spec.steps).toEqual([{ kind: "copy", src: "app/", dst: "/srv/app/" }]);
  });

  test("fromBaseImage records the base without any I/O", () => {
    const t = new Template().fromBaseImage("rayito-base");
    expect(t.spec.base).toEqual({ kind: "rayito-base", name: "rayito-base", version: undefined });
  });

  test("steps compile in call order", () => {
    const t = new Template()
      .fromBaseImage()
      .pipInstall(["pandas==2.2.3"])
      .copy("app/", "/srv/app/")
      .setEnvs({ MODE: "prod" });
    expect(t.spec.steps).toEqual([
      {
        kind: "run",
        cmd: "python3 -m pip install --no-cache-dir --break-system-packages pandas==2.2.3",
      },
      { kind: "copy", src: "app/", dst: "/srv/app/" },
      { kind: "env", key: "MODE", value: "prod" },
    ]);
  });

  test.each(["fromImage", "fromTemplate", "fromDockerfile", "fromGcpRegistry"] as const)(
    "%s raises UnimplementedError before any I/O",
    (method) => {
      expect(() => (new Template()[method] as (...args: unknown[]) => unknown)("whatever")).toThrow(
        UnimplementedError,
      );
    },
  );

  test("aptInstall names dnfInstall instead", () => {
    expect(() => new Template().aptInstall("curl")).toThrow(/dnfInstall/);
  });

  test("dnfInstall generates a quoted dnf step", () => {
    expect(new Template().fromBaseImage().dnfInstall(["tree", "jq"]).spec.steps).toEqual([
      {
        kind: "run",
        cmd: "dnf install -y --setopt=install_weak_deps=0 tree jq && dnf clean all",
      },
    ]);
    expect(new Template().dnfInstall("tree; rm -rf /").spec.steps).toEqual([
      {
        kind: "run",
        cmd: "dnf install -y --setopt=install_weak_deps=0 'tree; rm -rf /' && dnf clean all",
      },
    ]);
  });

  test("npmInstall guards on npm and supports global", () => {
    const local = new Template().npmInstall(["left-pad@1.3.0"]).spec.steps[0];
    const scoped = new Template().npmInstall("@scope/pkg", { g: true }).spec.steps[0];
    expect(local).toMatchObject({ kind: "run" });
    const localCmd = (local as { cmd: string }).cmd;
    const scopedCmd = (scoped as { cmd: string }).cmd;
    expect(localCmd.startsWith("command -v npm >/dev/null 2>&1 || { echo ")).toBe(true);
    expect(localCmd).toContain(
      `exit ${NPM_MISSING_EXIT_CODE}; } && npm install --no-audit --no-fund left-pad@1.3.0`,
    );
    expect(localCmd).toContain('dnfInstall(["nodejs20", "nodejs20-npm"])');
    expect(NODE_PACKAGES).toEqual(["nodejs20", "nodejs20-npm"]);
    expect(scopedCmd.endsWith("npm install --no-audit --no-fund -g @scope/pkg")).toBe(true);
  });

  test("npmInstall without packages runs a plain install in the workdir", () => {
    const step = new Template().npmInstall().spec.steps[0] as { cmd: string };
    expect(step.cmd.endsWith("&& npm install --no-audit --no-fund")).toBe(true);
  });

  test("dnfInstall runs as root and restores a non-root user", () => {
    const steps = new Template().setUser("user").dnfInstall("tree").spec.steps;
    expect(steps.map((s) => s.kind)).toEqual(["user", "user", "run", "user"]);
    expect(steps[1]).toEqual({ kind: "user", user: "root" });
    expect(steps[3]).toEqual({ kind: "user", user: "user" });
  });

  test.each([undefined, "root", "0"])("dnfInstall does not wrap when already root (%s)", (user) => {
    const base = user === undefined ? new Template() : new Template().setUser(user);
    const kinds = base.dnfInstall("tree").spec.steps.map((s) => s.kind);
    expect(kinds.filter((k) => k === "user")).toHaveLength(user === undefined ? 0 : 1);
    expect(kinds.at(-1)).toBe("run");
  });

  test("dnfInstall wraps around the latest user only", () => {
    const steps = new Template().setUser("a").setUser("b").dnfInstall("tree").spec.steps;
    expect(steps.slice(-3).map((s) => s.kind)).toEqual(["user", "run", "user"]);
    expect(steps.at(-1)).toEqual({ kind: "user", user: "b" });
  });

  test("npmInstall global runs as root but local keeps the user", () => {
    const base = new Template().setUser("user");
    expect(base.npmInstall("typescript", { g: true }).spec.steps.map((s) => s.kind)).toEqual([
      "user",
      "user",
      "run",
      "user",
    ]);
    expect(base.npmInstall("left-pad").spec.steps.map((s) => s.kind)).toEqual(["user", "run"]);
  });

  test.each(["dnfInstall", "npmInstall"] as const)(
    "%s rejects empty and option-like names",
    (method) => {
      for (const packages of [[], [" "], [""], ["--help"], "-y"]) {
        expect(() => new Template()[method](packages)).toThrow(InvalidArgumentError);
      }
    },
  );

  test("invalid inputs raise before any I/O", () => {
    expect(() => new Template().copy("", "/dst")).toThrow(InvalidArgumentError);
    expect(() => new Template().copy("src", "relative")).toThrow(InvalidArgumentError);
    expect(() => new Template().runCmd("   ")).toThrow(InvalidArgumentError);
    expect(() => new Template().pipInstall([])).toThrow(InvalidArgumentError);
    expect(() => new Template().workdir("relative")).toThrow(InvalidArgumentError);
    expect(() => new Template().setUser("")).toThrow(InvalidArgumentError);
    expect(() => new Template().setStartCmd("   ")).toThrow(InvalidArgumentError);
  });

  test("setStartCmd accepts a ReadyCommand, a string or nothing", () => {
    const withReadyCommand = new Template().setStartCmd("run.sh", waitForPort(8000));
    expect(withReadyCommand.spec.start?.readyCmd).toBeDefined();
    expect(withReadyCommand.spec.start?.readyPoll).toBeDefined();

    const withRawString = new Template().setStartCmd("run.sh", "test -e /tmp/ready");
    expect(withRawString.spec.start?.readyCmd).toBe("test -e /tmp/ready");
    expect(withRawString.spec.start?.readyPoll).toBeUndefined();

    const withNothing = new Template().setStartCmd("run.sh");
    expect(withNothing.spec.start?.readyCmd).toBeUndefined();
  });

  test("skipCache is a pure flag", () => {
    expect(new Template().spec.skipCache).toBe(false);
    expect(new Template().skipCache().spec.skipCache).toBe(true);
    expect(new Template().skipCache(false).spec.skipCache).toBe(false);
  });

  test("toJSON is stable for the same spec", () => {
    const t = new Template().fromBaseImage().pipInstall("pandas");
    expect(t.toJSON()).toBe(t.toJSON());
  });

  test("the builder stays synchronous and pure (toDockerfile needs no await)", () => {
    const dockerfile = new Template()
      .fromBaseImage("rayito-base")
      .pipInstall(["pandas"])
      .copy("app/", "/srv/app/")
      .setEnvs({ MODE: "prod" })
      .setStartCmd("python -m http.server 8000", waitForPort(8000))
      .toDockerfile();
    expect(dockerfile).toContain("pip install");
  });
});

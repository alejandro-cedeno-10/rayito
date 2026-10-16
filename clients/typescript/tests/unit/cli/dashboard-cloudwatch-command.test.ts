/**
 * `rayito dashboard cloudwatch`: sólo genera texto, así que ninguna prueba
 * crea servicios de AWS (los servicios falsos fallan al tocarlos) y la salida
 * es la del generador compartido con Python.
 */

import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { runCli } from "../../../src/cli/run.js";
import { buildBoard, renderBoard } from "../../../src/dashboard/cloudwatch-board.js";
import { FakeTerminal } from "./terminal.js";

const REGION = "us-east-1";
const folders: string[] = [];

afterEach(() => {
  for (const folder of folders.splice(0)) {
    rmSync(folder, { recursive: true, force: true });
  }
});

async function invoke(args: string[], env: Record<string, string> = {}) {
  const terminal = new FakeTerminal();
  const exitCode = await runCli(args, {
    terminal,
    env,
    now: () => new Date(),
    sleep: async () => undefined,
    signal: new AbortController().signal,
    createServices: async () => {
      throw new Error("dashboard cloudwatch no necesita AWS");
    },
  });
  return { exitCode, stdout: terminal.stdoutText, stderr: terminal.stderrText };
}

describe("dashboard cloudwatch", () => {
  it("imprime el JSON del generador sin crear ninguna sesión de AWS", async () => {
    const outcome = await invoke(["--region", REGION, "dashboard", "cloudwatch"]);
    expect(outcome.exitCode).toBe(0);
    expect(outcome.stdout).toBe(`${renderBoard(buildBoard({ region: REGION }), true)}\n`);
  });

  it("--otlp, --quota, --log-group y --log-group-prefix llegan al generador", async () => {
    const outcome = await invoke([
      "--region",
      "eu-west-1",
      "dashboard",
      "cloudwatch",
      "--otlp",
      "--quota",
      "--log-group",
      "/rayito/a",
      "--log-group",
      "/rayito/b",
      "--log-group-prefix",
      "/otro/",
    ]);
    const expected = buildBoard({
      region: "eu-west-1",
      logGroupPrefix: "/otro/",
      logGroups: ["/rayito/a", "/rayito/b"],
      includeOtlp: true,
      includeQuota: true,
    });
    expect(outcome.stdout).toBe(`${renderBoard(expected, true)}\n`);
  });

  it("la región también sale de AWS_REGION y --region gana", async () => {
    const fromEnv = await invoke(["dashboard", "cloudwatch"], { AWS_REGION: "eu-west-1" });
    expect(fromEnv.stdout).toContain('"region": "eu-west-1"');
    const explicit = await invoke(["--region", REGION, "dashboard", "cloudwatch"], {
      AWS_REGION: "eu-west-1",
    });
    expect(explicit.stdout).toContain(`"region": "${REGION}"`);
  });

  it("sin región sale con 2", async () => {
    const outcome = await invoke(["dashboard", "cloudwatch"], { AWS_DEFAULT_REGION: REGION });
    expect(outcome.exitCode).toBe(2);
    expect(outcome.stderr).toContain("sin región: pasa --region o exporta AWS_REGION");
  });

  it("un prefijo o un grupo inválido salen con 2", async () => {
    const prefix = await invoke([
      "--region",
      REGION,
      "dashboard",
      "cloudwatch",
      "--log-group-prefix",
      "rayito/",
    ]);
    expect(prefix.exitCode).toBe(2);
    expect(prefix.stderr).toContain("prefijo de grupo de logs inválido");
    const group = await invoke([
      "--region",
      REGION,
      "dashboard",
      "cloudwatch",
      "--log-group",
      "/a b",
    ]);
    expect(group.exitCode).toBe(2);
    expect(group.stderr).toContain("nombre de grupo de logs inválido");
  });

  it("--output escribe el fichero y dice sólo su nombre", async () => {
    const folder = mkdtempSync(join(tmpdir(), "rayito-board-"));
    folders.push(folder);
    const path = join(folder, "board.json");
    const outcome = await invoke(["--region", REGION, "dashboard", "cloudwatch", "-o", path]);
    expect(outcome.exitCode).toBe(0);
    expect(outcome.stdout).toBe("Dashboard escrito en board.json.\n");
    expect(readFileSync(path, "utf8")).toBe(
      `${renderBoard(buildBoard({ region: REGION }), true)}\n`,
    );
  });

  it("un --output ilegible sale con 2 y no nombra el camino", async () => {
    const outcome = await invoke([
      "--region",
      REGION,
      "dashboard",
      "cloudwatch",
      "--output",
      join(tmpdir(), "rayito-no-existe", "board.json"),
    ]);
    expect(outcome.exitCode).toBe(2);
    expect(outcome.stderr).toContain("--output: no se pudo escribir el fichero");
    expect(outcome.stderr).not.toContain("rayito-no-existe");
  });
});

/**
 * Las banderas de la entrada estructurada en `rayito agent` (ADR-033 D3):
 * `--graph` en `detect`, y `--input` / `--input-file` en `invoke`, con sus
 * errores de uso antes de cualquier llamada y la salida impresa como JSON.
 * Espejo de `cli/test_agent_structured_input_cli.py`.
 */

import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import type { AgentEvent, AgentResult } from "../../../src/agent/events.js";
import {
  INVOKE_INPUT_INVALID,
  INVOKE_INPUT_REQUIRED,
  INVOKE_INPUT_UNREADABLE,
  invokeRequestOf,
} from "../../../src/cli/agent-output.js";
import type { CommandContext } from "../../../src/cli/command.js";
import type { AgentDetectOverrides, DetectedProject } from "../../../src/cli/ops-ports.js";
import { FakeAgentProjects, RESULT, SANDBOX_ID } from "./agent-project-fakes.js";
import { buildWorld } from "./case-world.js";
import { runWithServices } from "./run-case.js";

const WITH_TOKEN = { RAYITO_ACCESS_TOKEN: "fake-access-token" };
const TICKET = { ticket_id: "T-1" };
const OUTPUT = { priority: "high" };

class StructuredProjects extends FakeAgentProjects {
  readonly overrides: AgentDetectOverrides[] = [];

  override async detect(path: string, overrides: AgentDetectOverrides): Promise<DetectedProject> {
    this.overrides.push(overrides);
    return super.detect(path, overrides);
  }
}

function structuredResult(): AgentResult {
  return { ...RESULT, text: "", output: OUTPUT };
}

async function run(projects: FakeAgentProjects, args: string[]) {
  return runWithServices({
    args,
    services: buildWorld({}).services,
    agentProjects: projects,
    env: WITH_TOKEN,
  });
}

function contextWith(values: Record<string, string | undefined>): CommandContext {
  return { values: { string: (name: string) => values[name] } } as unknown as CommandContext;
}

describe("agent invoke --input", () => {
  it("sends the input object and prints the output as JSON", async () => {
    const projects = new FakeAgentProjects();
    projects.deployed.invoke = async (request) => {
      projects.deployed.calls.push(["invoke", request, undefined]);
      return structuredResult();
    };
    const plain = await run(projects, [
      "agent",
      "invoke",
      SANDBOX_ID,
      "--input",
      JSON.stringify(TICKET),
    ]);
    expect(plain.exitCode).toBe(0);
    expect(JSON.parse(plain.stdout)).toEqual(OUTPUT);
    expect(projects.deployed.calls).toEqual([["invoke", { input: TICKET }, undefined]]);
    const asJson = await run(projects, [
      "--json",
      "agent",
      "invoke",
      SANDBOX_ID,
      "--input",
      JSON.stringify(TICKET),
    ]);
    expect(JSON.parse(asJson.stdout).output).toEqual(OUTPUT);
  });

  it("reads the input from a file and streams the output", async () => {
    const projects = new FakeAgentProjects();
    const done: AgentEvent = {
      type: "done",
      sessionId: "ses_1",
      exitCode: 0,
      usage: RESULT.usage,
      output: OUTPUT,
    };
    projects.deployed.events = [done];
    const file = join(mkdtempSync(join(tmpdir(), "rayito-input-")), "ticket.json");
    writeFileSync(file, JSON.stringify(TICKET));
    const streamed = await run(projects, [
      "agent",
      "invoke",
      SANDBOX_ID,
      "--input-file",
      file,
      "--stream",
    ]);
    expect(streamed.exitCode).toBe(0);
    expect(JSON.parse(streamed.stdout)).toEqual(OUTPUT);
    expect(projects.deployed.calls).toEqual([["stream", { input: TICKET }, undefined]]);
  });

  it.each([
    [[]],
    [["hola", "--input", "{}"]],
    [["--input", "{}", "--input-file", "-"]],
    [["--input", "[1]"]],
    [["--input", "no-es-json"]],
    [["--input-file", "no-existe.json"]],
  ])("%j is a usage error before any call", async (extra) => {
    const projects = new FakeAgentProjects();
    const outcome = await run(projects, ["agent", "invoke", SANDBOX_ID, ...extra]);
    expect(outcome.exitCode).toBe(2);
    expect(projects.calls).toEqual([]);
    expect(projects.deployed.calls).toEqual([]);
    expect(outcome.stderr).not.toContain("no-es-json");
  });

  it("reads stdin with -", () => {
    const read = (path: string | number) => {
      expect(path).toBe(0);
      return JSON.stringify(TICKET);
    };
    expect(invokeRequestOf(contextWith({ input_file: "-" }), read)).toEqual({ input: TICKET });
    expect(invokeRequestOf(contextWith({ prompt: "hola" }))).toBe("hola");
    expect(() => invokeRequestOf(contextWith({}))).toThrowError(INVOKE_INPUT_REQUIRED);
    expect(() =>
      invokeRequestOf(contextWith({ input_file: "x" }), () => {
        throw new Error("sin permiso");
      }),
    ).toThrowError(INVOKE_INPUT_UNREADABLE);
    expect(() => invokeRequestOf(contextWith({ input_json: "null" }))).toThrowError(
      INVOKE_INPUT_INVALID,
    );
  });
});

describe("agent detect --graph", () => {
  it("passes the graph to the detection", async () => {
    const projects = new StructuredProjects();
    const outcome = await run(projects, ["agent", "detect", ".", "--graph", "triage"]);
    expect(outcome.exitCode).toBe(0);
    expect(projects.overrides[0]?.graph).toBe("triage");
  });
});

/**
 * Entrada estructurada en el puente de Node (`node_bridge.mjs`, adaptador
 * `langgraph`, ADR-033 D3): un grafo con `getInputJsonSchema` sin `messages`
 * se ejecuta con el `input` de la petición y cierra con `done` y su `output`;
 * un prompt es `input_required` si el esquema tiene claves `required` (si no,
 * sigue el camino de mensajes), un `input` que no cumple el esquema es
 * `invalid_input` y un grafo de mensajes sigue como siempre. Se ejecuta el
 * proceso real del puente con un grafo de doble.
 */

import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { afterEach, beforeEach, describe, expect, test } from "vitest";
import { AGENT_SERVICE_MAX_OUTPUT_BYTES } from "../../src/limits.js";

type Json = Record<string, unknown>;

const REPO = join(import.meta.dirname, "..", "..", "..", "..");
const BRIDGE = join(
  REPO,
  "clients",
  "python",
  "src",
  "rayito",
  "_agent",
  "_assets",
  "node_bridge.mjs",
);
const SESSION = `rda_${"e".repeat(32)}`;
const INPUT_VALIDATION = JSON.parse(
  readFileSync(join(REPO, "testdata", "agent-deploy", "input-validation.json"), "utf8"),
) as {
  schemas: Record<string, Json>;
  cases: { name: string; schema: string; input: Json; valid: boolean }[];
};
const TICKET = { ticket_id: "T-1" };

const STRUCTURED_GRAPH = `
export const calls = [];
export const graph = {
  getInputJsonSchema() {
    return {
      type: "object",
      properties: { ticket_id: { type: "string" } },
      required: ["ticket_id"],
      additionalProperties: false,
    };
  },
  getOutputJsonSchema() {
    return { properties: { priority: {}, summary: {} } };
  },
  async stream(input, options) {
    const line = JSON.stringify({ input, options });
    process.stderr.write(line + "\\n");
    return (async function* () {
      yield ["updates", { classify: { priority: "high" } }];
      yield ["values", { ticket_id: input.ticket_id, priority: "high", summary: "printer offline" }];
    })();
  },
};
`;

const MESSAGES_GRAPH = `
export const graph = {
  getInputJsonSchema() {
    return { properties: { messages: { type: "array" } } };
  },
  async stream(input, options) {
    process.stderr.write(JSON.stringify({ input, options }) + "\\n");
    return (async function* () {})();
  },
};
`;

const OPTIONAL_INPUT_GRAPH = `
export const graph = {
  getInputJsonSchema() {
    return { type: "object", properties: { result: { type: "string" } } };
  },
  async stream(input, options) {
    process.stderr.write(JSON.stringify({ input, options }) + "\\n");
    return (async function* () {})();
  },
};
`;

let directory = "";

beforeEach(() => {
  directory = mkdtempSync(join(tmpdir(), "bridge-structured-"));
});

afterEach(() => {
  rmSync(directory, { recursive: true });
});

function runBridge(source: string, request: Json): { lines: Json[]; seen: Json[] } {
  writeFileSync(join(directory, "agent.mjs"), source);
  const config = join(directory, "config.json");
  writeFileSync(
    config,
    JSON.stringify({
      adapter: "langgraph",
      app_dir: directory,
      entrypoint: "agent.mjs",
      sessions_dir: join(directory, "sessions"),
    }),
  );
  const result = spawnSync(process.execPath, [BRIDGE, config], {
    input: JSON.stringify({ v: 1, session_id: SESSION, ...request }),
    encoding: "utf8",
  });
  expect(result.status).toBe(0);
  const parse = (text: string) =>
    text
      .trim()
      .split("\n")
      .filter((line) => line.startsWith("{"))
      .map((line) => JSON.parse(line) as Json);
  return { lines: parse(result.stdout), seen: parse(result.stderr) };
}

describe("input-validation.json", () => {
  for (const testCase of INPUT_VALIDATION.cases) {
    test(testCase.name, async () => {
      const bridge = (await import(pathToFileURL(BRIDGE).href)) as {
        validateInput: (schema: Json, data: Json) => void;
      };
      const run = () =>
        bridge.validateInput(INPUT_VALIDATION.schemas[testCase.schema] ?? {}, testCase.input);
      if (testCase.valid) {
        expect(run).not.toThrow();
        return;
      }
      expect(run).toThrow(expect.objectContaining({ code: "invalid_input" }));
    });
  }
});

describe("structured langgraph graphs in the bridge", () => {
  test("the output limit is the shared limit", async () => {
    const bridge = (await import(pathToFileURL(BRIDGE).href)) as Json;
    expect(bridge.MAX_SERVICE_OUTPUT_BYTES).toBe(AGENT_SERVICE_MAX_OUTPUT_BYTES);
  });

  test("run with the input and close with the filtered output", () => {
    const { lines, seen } = runBridge(STRUCTURED_GRAPH, { prompt: "", input: TICKET });
    expect(lines.at(-1)).toEqual({
      v: 1,
      type: "done",
      session_id: SESSION,
      output: { priority: "high", summary: "printer offline" },
    });
    expect(seen[0]).toEqual({
      input: TICKET,
      options: { configurable: { thread_id: SESSION }, streamMode: ["updates", "values"] },
    });
  });

  test.each([
    ["a prompt", { prompt: "hola" }, "input_required"],
    ["a missing key", { prompt: "", input: {} }, "invalid_input"],
    ["an extra key", { prompt: "", input: { ...TICKET, extra: 1 } }, "invalid_input"],
    ["a wrong type", { prompt: "", input: { ticket_id: 7 } }, "invalid_input"],
  ])("refuse %s", (_name, request, detail) => {
    const { lines, seen } = runBridge(STRUCTURED_GRAPH, request);
    expect(lines.at(-1)).toEqual({
      v: 1,
      type: "agent_failed",
      reason: "protocol_error",
      detail_code: detail,
      session_id: SESSION,
    });
    expect(seen).toEqual([]);
  });

  test("a prompt to a graph without required input keeps the prompt path", () => {
    const { lines, seen } = runBridge(OPTIONAL_INPUT_GRAPH, { prompt: "hola" });
    expect(seen[0]).toEqual({
      input: { messages: [{ role: "user", content: "hola" }] },
      options: { configurable: { thread_id: SESSION }, streamMode: "updates" },
    });
    expect(lines.at(-1)).toEqual({ v: 1, type: "done", session_id: SESSION });
  });

  test("a message graph keeps the prompt path and takes an input as is", () => {
    const prompt = runBridge(MESSAGES_GRAPH, { prompt: "hola" });
    expect(prompt.seen[0]).toEqual({
      input: { messages: [{ role: "user", content: "hola" }] },
      options: { configurable: { thread_id: SESSION }, streamMode: "updates" },
    });
    expect(prompt.lines.at(-1)).toEqual({ v: 1, type: "done", session_id: SESSION });
    const input = runBridge(MESSAGES_GRAPH, {
      prompt: "",
      input: { locale: "es", messages: [{ role: "user", content: "sigue" }] },
    });
    expect(input.seen[0]).toMatchObject({ input: { locale: "es" } });
    expect(input.lines.at(-1)).toEqual({ v: 1, type: "done", session_id: SESSION });
  });

  test("an input to a message graph without checkpointer is kept in the session", () => {
    const turn = { role: "user", content: "sigue" };
    runBridge(MESSAGES_GRAPH, { prompt: "", input: { locale: "es", messages: [turn] } });
    const saved = JSON.parse(
      readFileSync(join(directory, "sessions", `${SESSION}.json`), "utf8"),
    ) as Json[];
    expect(saved).toEqual([turn]);
    const next = runBridge(MESSAGES_GRAPH, { prompt: "hola" });
    expect(next.seen[0]).toMatchObject({
      input: { messages: [turn, { role: "user", content: "hola" }] },
    });
  });
});

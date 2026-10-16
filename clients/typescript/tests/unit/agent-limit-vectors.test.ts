/**
 * `LimitTracker` contra los vectores compartidos
 * (`testdata/agent/limit-vectors.json`): los lee también el SDK de Python y
 * los tests del host del agente, así que los tres imponen `max_steps` y
 * `max_total_tokens` igual.
 */

import { readFile } from "node:fs/promises";
import { join } from "node:path";
import { describe, expect, test } from "vitest";
import { finishV1, newProtocolV1State, parseV1Line } from "../../src/agent/protocol-v1.js";
import { LimitTracker } from "../../src/agent/stream.js";
import {
  type AgentEvent,
  type AgentFailed,
  AgentLimits,
  agentFailed,
  type StepStarted,
  TokenUsage,
} from "../../src/index.js";

const TESTDATA = join(import.meta.dirname, "..", "..", "..", "..", "testdata", "agent");

interface RawEvent {
  readonly type: string;
  readonly index?: number;
  readonly usage?: { input?: number; output?: number };
  readonly finish_reason?: string;
  readonly text?: string;
  readonly call_id?: string;
  readonly name?: string;
  readonly status?: "completed" | "error";
  readonly session_id?: string;
  readonly exit_code?: number;
  readonly reason?: string;
}

interface LimitCase {
  readonly name: string;
  readonly limits: { max_steps: number; max_total_tokens: number | null };
  readonly session_id: string | null;
  readonly events: RawEvent[];
  readonly expected: Record<string, unknown>[];
  readonly final: {
    steps: number;
    total_tokens: number;
    last_text: string;
    tool_calls: number;
    session_id: string | null;
  };
}

function eventOf(raw: RawEvent): AgentEvent {
  switch (raw.type) {
    case "step_started":
      return { type: "step_started", index: raw.index as number };
    case "step_finished":
      return {
        type: "step_finished",
        index: 0,
        usage: new TokenUsage(raw.usage ?? {}),
        finishReason: raw.finish_reason,
      };
    case "text":
      return { type: "text", text: raw.text as string };
    case "tool_call":
      return {
        type: "tool_call",
        callId: raw.call_id as string,
        name: raw.name as string,
        status: raw.status as "completed" | "error",
        input: undefined,
        output: undefined,
        outputTruncated: false,
      };
    case "done":
      return {
        type: "done",
        sessionId: raw.session_id as string,
        exitCode: raw.exit_code as number,
        usage: new TokenUsage(),
      };
    default:
      return agentFailed(raw.reason as string);
  }
}

function projection(event: AgentEvent): Record<string, unknown> {
  const shown: Record<string, unknown> = { type: event.type };
  if (event.type === "step_started") {
    shown.index = (event as StepStarted).index;
  }
  if (event.type === "agent_failed") {
    shown.reason = (event as AgentFailed).reason;
    shown.session_id = (event as AgentFailed).sessionId ?? null;
  }
  return shown;
}

describe("LimitTracker shared vectors", async () => {
  const { cases } = JSON.parse(await readFile(join(TESTDATA, "limit-vectors.json"), "utf8")) as {
    cases: LimitCase[];
  };

  test.each(cases.map((c) => [c.name, c] as const))("%s", (_name, c) => {
    const tracker = new LimitTracker(
      new AgentLimits({ maxSteps: c.limits.max_steps, maxTotalTokens: c.limits.max_total_tokens }),
      c.session_id ?? undefined,
    );
    const produced = c.events.map((raw) => projection(tracker.track(eventOf(raw))));

    expect(produced).toHaveLength(c.expected.length);
    produced.forEach((shown, position) => {
      const expected = c.expected[position] as Record<string, unknown>;
      const picked = Object.fromEntries(Object.keys(expected).map((key) => [key, shown[key]]));
      expect(picked).toEqual(expected);
    });
    expect(tracker.steps).toBe(c.final.steps);
    expect(tracker.usage.total).toBe(c.final.total_tokens);
    expect(tracker.lastText).toBe(c.final.last_text);
    expect(tracker.toolCalls).toHaveLength(c.final.tool_calls);
    expect(tracker.sessionId ?? null).toBe(c.final.session_id);
  });
});

describe("the v1 decoder", () => {
  test("alone produces the golden events", async () => {
    const expected = JSON.parse(
      await readFile(join(TESTDATA, "rayito-protocol-v1-expected.json"), "utf8"),
    ) as { events: unknown[]; ignored_lines: number; done: unknown };
    const lines = (await readFile(join(TESTDATA, "rayito-protocol-v1.jsonl"), "utf8"))
      .split("\n")
      .filter((line) => line !== "");
    const state = newProtocolV1State();
    const events = lines.flatMap((line) => [...parseV1Line(new TextEncoder().encode(line), state)]);
    expect(events.length).toBe(expected.events.length);
    expect(state.ignoredLines).toBe(expected.ignored_lines);
    expect(finishV1(state, 0).type).toBe("done");
  });
});

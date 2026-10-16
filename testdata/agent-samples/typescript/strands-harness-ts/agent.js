import { tool } from "@strands-agents/sdk";
import { createHarness } from "@strands-agents/harness";
import { z } from "zod";

const RUNBOOKS = { "disk-full": "rotate the logs", "high-cpu": "scale the service out" };

const add = tool({
  name: "add",
  description: "Add two integers.",
  inputSchema: z.object({ a: z.number().int(), b: z.number().int() }),
  callback: ({ a, b }) => a + b,
});

const runbook = tool({
  name: "runbook",
  description: "Return the runbook step of an alert name.",
  inputSchema: z.object({ alert: z.string() }),
  callback: ({ alert }) => RUNBOOKS[alert] ?? "escalate to the on-call engineer",
});

export async function createAgent() {
  return createHarness({
    model: `bedrock/${process.env.MODEL_ID}`,
    instructions: "You assist with operations questions. Use the tools when they apply and answer in one short sentence.",
    tools: [add, runbook],
  });
}

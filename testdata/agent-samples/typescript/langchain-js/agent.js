import { ChatBedrockConverse } from "@langchain/aws";
import { createAgent, tool } from "langchain";
import { z } from "zod";

const DEFAULT_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0";

const SYSTEM_PROMPT =
  "You triage support tickets. Use the tools when they apply and answer in one short sentence.";
const PRIORITIES = { login: "high", invoice: "medium" };

const add = tool(({ a, b }) => String(a + b), {
  name: "add",
  description: "Add two integers.",
  schema: z.object({ a: z.number().int(), b: z.number().int() }),
});

const ticketPriority = tool(({ topic }) => PRIORITIES[topic] ?? "low", {
  name: "ticket_priority",
  description: "Return the priority of a ticket topic.",
  schema: z.object({ topic: z.string() }),
});

export const agent = createAgent({
  model: new ChatBedrockConverse({
    model: process.env.MODEL_ID ?? DEFAULT_MODEL_ID,
    region: process.env.AWS_REGION,
  }),
  tools: [add, ticketPriority],
  systemPrompt: SYSTEM_PROMPT,
});

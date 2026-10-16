import { Agent, BedrockModel, tool } from "@strands-agents/sdk";
import { z } from "zod";

const DEFAULT_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0";

const TAX_RATES = { CO: 19, MX: 16 };

const add = tool({
  name: "add",
  description: "Add two integers.",
  inputSchema: z.object({ a: z.number().int(), b: z.number().int() }),
  callback: ({ a, b }) => a + b,
});

const taxRate = tool({
  name: "tax_rate",
  description: "Return the sales tax percentage of a country code.",
  inputSchema: z.object({ country: z.string() }),
  callback: ({ country }) => TAX_RATES[country.toUpperCase()] ?? 0,
});

export const agent = new Agent({
  model: new BedrockModel({ modelId: process.env.MODEL_ID ?? DEFAULT_MODEL_ID, region: process.env.AWS_REGION }),
  systemPrompt: "You help with billing questions. Use the tools when they apply and answer in one short sentence.",
  tools: [add, taxRate],
});

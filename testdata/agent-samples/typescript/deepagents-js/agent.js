import { ChatBedrockConverse } from "@langchain/aws";
import { tool } from "@langchain/core/tools";
import { createDeepAgent } from "deepagents";
import { z } from "zod";

const DEFAULT_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0";

const GLOSSARY = { microvm: "a lightweight virtual machine", snapshot: "a saved machine state" };

const add = tool(({ a, b }) => String(a + b), {
  name: "add",
  description: "Add two integers.",
  schema: z.object({ a: z.number().int(), b: z.number().int() }),
});

const define = tool(({ term }) => GLOSSARY[term.toLowerCase()] ?? "not in the glossary", {
  name: "define",
  description: "Return the glossary definition of a term.",
  schema: z.object({ term: z.string() }),
});

export const agent = createDeepAgent({
  model: new ChatBedrockConverse({ model: process.env.MODEL_ID ?? DEFAULT_MODEL_ID, region: process.env.AWS_REGION }),
  tools: [add, define],
  systemPrompt: "You keep research notes. Use the tools when they apply and answer in one short sentence.",
});

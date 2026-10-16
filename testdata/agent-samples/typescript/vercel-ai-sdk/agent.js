import { createAmazonBedrock } from "@ai-sdk/amazon-bedrock";
import { ToolLoopAgent, tool } from "ai";
import { z } from "zod";

const DEFAULT_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0";

const MEETINGS = { standup: "09:30 daily", retro: "every other friday" };

const bedrock = createAmazonBedrock({ region: process.env.AWS_REGION });

export const agent = new ToolLoopAgent({
  model: bedrock(process.env.MODEL_ID ?? DEFAULT_MODEL_ID),
  instructions: "You keep meeting notes. Use the tools when they apply and answer in one short sentence.",
  tools: {
    add: tool({
      description: "Add two integers.",
      inputSchema: z.object({ a: z.number().int(), b: z.number().int() }),
      execute: async ({ a, b }) => a + b,
    }),
    meetingTime: tool({
      description: "Return when a recurring meeting happens.",
      inputSchema: z.object({ name: z.string() }),
      execute: async ({ name }) => MEETINGS[name.toLowerCase()] ?? "not scheduled",
    }),
  },
});

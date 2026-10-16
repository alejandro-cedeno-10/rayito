import { ChatBedrockConverse } from "@langchain/aws";
import { tool } from "@langchain/core/tools";
import { createReactAgent } from "@langchain/langgraph/prebuilt";
import { z } from "zod";

const ORDERS = { "A-100": "shipped", "A-200": "processing" };

const add = tool(({ a, b }) => String(a + b), {
  name: "add",
  description: "Add two integers.",
  schema: z.object({ a: z.number().int(), b: z.number().int() }),
});

const orderStatus = tool(({ orderId }) => ORDERS[orderId] ?? "unknown", {
  name: "order_status",
  description: "Return the status of an order.",
  schema: z.object({ orderId: z.string() }),
});

export const graph = createReactAgent({
  llm: new ChatBedrockConverse({ model: process.env.MODEL_ID, region: process.env.AWS_REGION }),
  tools: [add, orderStatus],
  prompt: "You are an orders assistant. Use the tools when they apply and answer in one short sentence.",
});

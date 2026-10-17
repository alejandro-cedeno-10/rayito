import { ChatBedrockConverse } from "@langchain/aws";
import { createReactAgent } from "@langchain/langgraph/prebuilt";

export const graph = createReactAgent({ llm: new ChatBedrockConverse({ model: "us.anthropic.claude-haiku-4-5-20251001-v1:0" }), tools: [] });

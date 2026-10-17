import { bedrock } from "@ai-sdk/amazon-bedrock";
import { ToolLoopAgent } from "ai";

export const agent = new ToolLoopAgent({ model: bedrock("us.anthropic.claude-haiku-4-5-20251001-v1:0"), tools: {} });

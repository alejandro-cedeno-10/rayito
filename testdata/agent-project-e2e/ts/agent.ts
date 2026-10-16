import { Agent, type Model } from "@strands-agents/sdk";

export function agent(ctx: { model: Model }): Agent {
  return new Agent({ model: ctx.model, systemPrompt: "Answer with one short sentence.", tools: [] });
}

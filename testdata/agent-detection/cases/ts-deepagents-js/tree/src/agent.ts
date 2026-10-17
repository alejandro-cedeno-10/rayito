import { createDeepAgent } from "deepagents";

export const agent = createDeepAgent({ model: "claude-haiku-4-5", tools: [] });

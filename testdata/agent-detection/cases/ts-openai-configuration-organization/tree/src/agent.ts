import { ChatOpenAI } from "@langchain/openai";
import { createReactAgent } from "@langchain/langgraph/prebuilt";

const llm = new ChatOpenAI({
  model: "gpt-4o-mini",
  configuration: { organization: "org-demo" },
});

export const graph = createReactAgent({ llm, tools: [] });

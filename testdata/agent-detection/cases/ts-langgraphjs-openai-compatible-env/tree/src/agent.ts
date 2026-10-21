import { ChatOpenAI } from "@langchain/openai";
import { createReactAgent } from "@langchain/langgraph/prebuilt";

const llm = new ChatOpenAI({
  model: process.env.LLM_MODEL,
  apiKey: process.env.LLM_API_KEY,
  configuration: { baseURL: process.env.LLM_BASE_URL },
});

export const graph = createReactAgent({ llm, tools: [] });

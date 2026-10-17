import { createHarness } from "@strands-agents/harness";

export async function buildAgent() {
  return createHarness({ model: "us.anthropic.claude-haiku-4-5-20251001-v1:0" });
}

"""Agente nativo de AgentCore: `BedrockAgentCoreApp` con un agente de Strands."""

import os

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent, tool
from strands.models import BedrockModel

app = BedrockAgentCoreApp()
NOTES = {"standup": "09:30 daily", "retro": "every other friday"}


@tool
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


@tool
def meeting_time(name: str) -> str:
    """Return when a recurring meeting happens."""
    return NOTES.get(name.lower(), "not scheduled")


agent = Agent(
    model=BedrockModel(model_id=os.environ["MODEL_ID"], region_name=os.environ.get("AWS_REGION")),
    system_prompt="You keep meeting notes. Use the tools when they apply and answer in one short sentence.",
    tools=[add, meeting_time],
)


@app.entrypoint
def invoke(payload):
    return {"result": str(agent(payload.get("prompt", "")))}


if __name__ == "__main__":
    app.run()

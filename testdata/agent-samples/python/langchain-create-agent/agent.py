"""Agente de soporte de LangChain: `create_agent` con dos herramientas."""

import os

from langchain.agents import create_agent
from langchain_aws import ChatBedrockConverse
from langchain_core.tools import tool

SYSTEM_PROMPT = "You triage support tickets. Use the tools when they apply and answer in one short sentence."
PRIORITIES = {"login": "high", "invoice": "medium"}


@tool
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


@tool
def ticket_priority(topic: str) -> str:
    """Return the priority of a ticket topic."""
    return PRIORITIES.get(topic, "low")


agent = create_agent(
    ChatBedrockConverse(model=os.environ["MODEL_ID"], region_name=os.environ.get("AWS_REGION")),
    tools=[add, ticket_priority],
    system_prompt=SYSTEM_PROMPT,
)

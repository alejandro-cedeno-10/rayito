"""Agente de deepagents con dos herramientas y su sistema de ficheros virtual."""

import os

from deepagents import create_deep_agent
from langchain_aws import ChatBedrockConverse
from langchain_core.tools import tool

SYSTEM_PROMPT = "You keep research notes. Use the tools when they apply and answer in one short sentence."
GLOSSARY = {"microvm": "a lightweight virtual machine", "snapshot": "a saved machine state"}


@tool
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


@tool
def define(term: str) -> str:
    """Return the glossary definition of a term."""
    return GLOSSARY.get(term.lower(), "not in the glossary")


agent = create_deep_agent(
    model=ChatBedrockConverse(model=os.environ["MODEL_ID"], region_name=os.environ.get("AWS_REGION")),
    tools=[add, define],
    system_prompt=SYSTEM_PROMPT,
)

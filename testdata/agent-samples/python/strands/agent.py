"""Agente de Strands Agents con un modelo de Bedrock y dos herramientas."""

import os

from strands import Agent, tool
from strands.models import BedrockModel

DEFAULT_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

SYSTEM_PROMPT = "You help with billing questions. Use the tools when they apply and answer in one short sentence."
TAX_RATES = {"CO": 19, "MX": 16}


@tool
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


@tool
def tax_rate(country: str) -> int:
    """Return the sales tax percentage of a country code."""
    return TAX_RATES.get(country.upper(), 0)


agent = Agent(
    model=BedrockModel(model_id=os.environ.get("MODEL_ID", DEFAULT_MODEL_ID), region_name=os.environ.get("AWS_REGION")),
    system_prompt=SYSTEM_PROMPT,
    tools=[add, tax_rate],
)

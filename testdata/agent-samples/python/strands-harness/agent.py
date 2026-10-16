"""Agente de Strands harness: `create_harness` con dos herramientas propias."""

import os

from strands import tool
from strands_harness import create_harness

DEFAULT_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

INSTRUCTIONS = "You assist with operations questions. Use the tools when they apply and answer in one short sentence."
RUNBOOKS = {"disk-full": "rotate the logs", "high-cpu": "scale the service out"}


@tool
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


@tool
def runbook(alert: str) -> str:
    """Return the runbook step of an alert name."""
    return RUNBOOKS.get(alert, "escalate to the on-call engineer")


agent = create_harness(
    model=f"bedrock/{os.environ.get("MODEL_ID", DEFAULT_MODEL_ID)}",
    instructions=INSTRUCTIONS,
    tools=[add, runbook],
)

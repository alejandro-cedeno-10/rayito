"""Grafo ReAct de pedidos: un modelo de Bedrock y dos herramientas."""

import os

from langchain_aws import ChatBedrockConverse
from langchain_core.tools import tool
from langgraph.prebuilt import create_react_agent

SYSTEM_PROMPT = "You are an orders assistant. Use the tools when they apply and answer in one short sentence."
ORDERS = {"A-100": "shipped", "A-200": "processing"}


@tool
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


@tool
def order_status(order_id: str) -> str:
    """Return the status of an order."""
    return ORDERS.get(order_id, "unknown")


def build_graph():
    model = ChatBedrockConverse(model=os.environ["MODEL_ID"], region_name=os.environ.get("AWS_REGION"))
    return create_react_agent(model, [add, order_status], prompt=SYSTEM_PROMPT)

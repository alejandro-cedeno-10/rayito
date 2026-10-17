from langchain_aws import ChatBedrockConverse
from langgraph.prebuilt import create_react_agent


def get_weather(city: str) -> str:
    """Devuelve el tiempo de una ciudad."""
    return f"Soleado en {city}"


def build_graph():
    model = ChatBedrockConverse(model="us.anthropic.claude-haiku-4-5-20251001-v1:0")
    return create_react_agent(model, [get_weather])

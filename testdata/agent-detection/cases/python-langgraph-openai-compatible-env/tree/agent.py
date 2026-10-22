from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent

from settings import settings


def lookup_ticket(ticket_id: str) -> str:
    """Devuelve el estado de un ticket."""
    return f"El ticket {ticket_id} está abierto"


def build_graph():
    model = ChatOpenAI(model=settings.model, base_url=settings.base_url, api_key=settings.api_key)
    return create_react_agent(model, [lookup_ticket])

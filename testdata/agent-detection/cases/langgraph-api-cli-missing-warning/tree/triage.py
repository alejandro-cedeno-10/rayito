from typing import TypedDict

from langgraph.graph import StateGraph


class Ticket(TypedDict):
    ticket_id: str


workflow = StateGraph(Ticket)
graph = workflow.compile()

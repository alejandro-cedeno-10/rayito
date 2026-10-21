"""Triaje de un ticket sin modelo: entra `ticket_id` y sale `priority` y
`summary`. Los nodos son deterministas para que la prueba no dependa de un
LLM."""

from typing import TypedDict

from langgraph.graph import END, START, StateGraph

URGENT_PREFIX = "T-1"


class InputState(TypedDict):
    ticket_id: str


class OutputState(TypedDict):
    priority: str
    summary: str


class State(InputState, OutputState):
    pass


def classify(state: InputState) -> dict[str, str]:
    urgent = state["ticket_id"].startswith(URGENT_PREFIX)
    return {"priority": "high" if urgent else "normal"}


def summarize(state: State) -> dict[str, str]:
    return {"summary": f"ticket {state['ticket_id']}: prioridad {state['priority']}"}


builder = StateGraph(State, input_schema=InputState, output_schema=OutputState)
builder.add_node("classify", classify)
builder.add_node("summarize", summarize)
builder.add_edge(START, "classify")
builder.add_edge("classify", "summarize")
builder.add_edge("summarize", END)
graph = builder.compile()

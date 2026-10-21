"""Grafo de eco determinista para probar el protocolo langgraph-api sin un
modelo: contesta al último mensaje en mayúsculas y, si la entrada trae
`ticket_id`, lo repite."""

from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages


class State(TypedDict, total=False):
    messages: Annotated[list[Any], add_messages]
    ticket_id: str


def reply(state: State) -> dict[str, Any]:
    ticket = state.get("ticket_id")
    if ticket is not None:
        return {"messages": [{"role": "assistant", "content": f"ticket {ticket}"}]}
    last = state["messages"][-1]
    return {"messages": [{"role": "assistant", "content": str(last.content).upper()}]}


builder = StateGraph(State)
builder.add_node("reply", reply)
builder.add_edge(START, "reply")
builder.add_edge("reply", END)
graph = builder.compile()

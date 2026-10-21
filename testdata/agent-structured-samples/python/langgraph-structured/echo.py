"""Eco sin modelo: devuelve el texto que recibe."""

from typing import TypedDict

from langgraph.graph import END, START, StateGraph


class EchoState(TypedDict):
    text: str


def echo(state: EchoState) -> EchoState:
    return {"text": state["text"]}


builder = StateGraph(EchoState)
builder.add_node("echo", echo)
builder.add_edge(START, "echo")
builder.add_edge("echo", END)
graph = builder.compile()

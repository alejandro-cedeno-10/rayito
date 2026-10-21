import os

from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent


def build_graph():
    model = ChatOpenAI(model=os.getenv("OPENAI_MODEL"))
    return create_react_agent(model, [])

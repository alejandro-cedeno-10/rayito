import os

from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent


def build_graph():
    model = ChatOpenAI(model="gpt-4o-mini", base_url=os.environ["LLM_BASE_URL"])
    return create_react_agent(model, [])

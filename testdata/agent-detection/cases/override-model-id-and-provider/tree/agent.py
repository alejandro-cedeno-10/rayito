import os

from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent


def build_graph():
    model = ChatOpenAI(model=os.environ["CHAT_MODEL"], base_url=os.environ["CHAT_API_BASE"])
    return create_react_agent(model, [])

from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent


def build_graph():
    model = ChatOpenAI(model="gpt-4o-mini", base_url="https://api.openai.com/v1")
    return create_react_agent(model, [])

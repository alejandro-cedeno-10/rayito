import os

from langchain_aws import ChatBedrockConverse
from langgraph.prebuilt import create_react_agent


def build_graph():
    model = ChatBedrockConverse(model=os.environ["MODEL_ID"])
    return create_react_agent(model, [])

from langgraph.prebuilt import create_react_agent


def build_graph(model, tools):
    return create_react_agent(model, tools)

from langchain.agents import create_agent

agent = create_agent("openai:gpt-5-mini", tools=[], system_prompt="Ayuda al usuario.")

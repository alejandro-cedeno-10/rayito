from strands import Agent
from strands.models.openai import OpenAIModel

agent = Agent(model=OpenAIModel(model_id="gpt-5-mini"))

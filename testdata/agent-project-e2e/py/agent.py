"""Agente de Strands del e2e: `ctx.model` ya apunta a la pasarela."""

from strands import Agent


def agent(ctx):
    return Agent(model=ctx.model, system_prompt="Answer with one short sentence.", tools=[])

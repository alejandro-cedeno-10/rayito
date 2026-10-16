"""Grafo de deepagents/LangGraph del e2e."""

from deepagents import create_deep_agent


def build(ctx):
    return create_deep_agent(
        model=ctx.model,
        system_prompt=ctx.instructions,
        subagents=ctx.subagents,
        backend=ctx.backend,
        middleware=ctx.middleware,
    )

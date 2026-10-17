"""Qué agente describe un `entrypoint` del host: el objeto ya construido, una
fábrica con el contexto, una fábrica sin argumentos o una `async`. Es lo que
permite servir un proyecto detectado (`module-level agent = ...`,
`def build_graph()`) sin escribir `fn(ctx)`."""

from __future__ import annotations

from typing import Any

import pytest

from rayito._agent._runner.agent_host.drivers._common import entry_value, requires_context
from rayito._agent._runner.agent_host.protocol import AgentFailure

READY: tuple[str, ...] = ("stream",)
CONTEXT = object()


class Built:
    def stream(self) -> None:
        return None


def test_an_object_that_is_already_an_agent_is_used_as_it_is() -> None:
    agent = Built()
    assert entry_value(agent, CONTEXT, ready_attributes=READY) is agent


def test_a_factory_that_asks_for_the_context_receives_it() -> None:
    seen: list[Any] = []

    def build(ctx: Any) -> Built:
        seen.append(ctx)
        return Built()

    assert isinstance(entry_value(build, CONTEXT, ready_attributes=READY), Built)
    assert seen == [CONTEXT]


def test_a_factory_without_arguments_is_called_without_the_context() -> None:
    def build() -> Built:
        return Built()

    assert isinstance(entry_value(build, CONTEXT, ready_attributes=READY), Built)


def test_an_async_factory_is_awaited() -> None:
    async def build() -> Built:
        return Built()

    assert isinstance(entry_value(build, CONTEXT, ready_attributes=READY), Built)


def test_a_factory_with_only_optional_arguments_is_called_without_the_context() -> None:
    def build(extra: int = 1) -> Built:
        return Built()

    assert not requires_context(build)
    assert isinstance(entry_value(build, CONTEXT, ready_attributes=READY), Built)


@pytest.mark.parametrize("target", [42, "text", None])
def test_anything_else_is_an_invalid_entrypoint(target: object) -> None:
    with pytest.raises(AgentFailure, match="invalid_entrypoint"):
        entry_value(target, CONTEXT, ready_attributes=READY)

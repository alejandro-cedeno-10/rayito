"""`import rayito` no carga el agente de IA (`rayito._agent`) ni el
adaptador `boto3` de los eventos de ciclo de vida
(`rayito._lifecycle_events._aws`): sus nombres públicos se resuelven en el
primer acceso (PEP 562) y `Sandbox.agent` se construye en el primer uso."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

import rayito

DEFERRED_MODULE_PREFIXES = (
    "rayito._agent",
    "rayito._lifecycle_events._aws",
    "rayito.sandbox_sync.agent",
    "rayito.sandbox_async.agent",
)


def _loaded_deferred_modules(statement: str) -> list[str]:
    prefixes = DEFERRED_MODULE_PREFIXES
    probe = (
        f"import json, sys; {statement}; "
        f"print(json.dumps(sorted(m for m in sys.modules if m.startswith({prefixes!r}))))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    loaded: list[str] = json.loads(result.stdout)
    return loaded


@pytest.mark.parametrize("statement", ["import rayito", "import rayito.e2b"])
def test_importing_rayito_does_not_load_the_agent_or_the_events_adapter(statement: str) -> None:
    assert _loaded_deferred_modules(statement) == []


def test_lazy_names_resolve_on_first_access() -> None:
    assert _loaded_deferred_modules("from rayito import AgentSpec") != []
    assert "rayito._lifecycle_events._aws" in _loaded_deferred_modules(
        "from rayito import LifecycleEvents"
    )


def test_every_lazy_export_is_public_and_resolves() -> None:
    lazy = set(rayito._LAZY_EXPORTS)
    assert lazy <= set(rayito.__all__)
    assert lazy <= set(dir(rayito))
    for name in sorted(lazy):
        assert getattr(rayito, name) is not None


def test_unknown_attribute_still_raises_attribute_error() -> None:
    with pytest.raises(AttributeError, match="no_such_name"):
        _ = rayito.no_such_name  # type: ignore[attr-defined]

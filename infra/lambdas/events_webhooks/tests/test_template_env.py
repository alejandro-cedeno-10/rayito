"""Every environment variable a handler reads is declared on its function in
`infra/events-webhooks.yaml` (regression for the deliverer reading a
`STACK_KEY_SECRET_ID` its function never had, which failed every batch).
Each handler lists what it reads in `REQUIRED_ENV`; every `*_ENV` constant
it defines must be in that list.
"""

from __future__ import annotations

from types import ModuleType

import pytest
from conftest import template_environment
from handlers import deliverer, forwarder, reconciler

_FUNCTIONS: tuple[tuple[str, ModuleType], ...] = (
    ("ForwarderFunction", forwarder),
    ("DelivererFunction", deliverer),
    ("ReconcilerFunction", reconciler),
)


@pytest.mark.parametrize(("function", "module"), _FUNCTIONS)
def test_every_env_var_a_handler_reads_is_declared(function: str, module: ModuleType) -> None:
    declared = set(template_environment(function))
    assert set(module.REQUIRED_ENV) <= declared, function


@pytest.mark.parametrize(("function", "module"), _FUNCTIONS)
def test_every_env_constant_is_listed_in_required_env(function: str, module: ModuleType) -> None:
    env_constants = {
        value
        for name, value in vars(module).items()
        if name.endswith("_ENV") and name.isupper() and isinstance(value, str)
    }
    assert env_constants == set(module.REQUIRED_ENV), function

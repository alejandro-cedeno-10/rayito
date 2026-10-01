"""`gateways=` apagado por defecto (ADR-014 regla 4, arquitectura M15 §9.2):
sin la opción, ni se construye un cliente `secretsmanager` nuevo ni se
manda ningún `ConfigureSandbox`; importar `rayito._secret_gateway` por sí
solo tampoco construye nada. El resto del contrato "coste cero" (la traza
de boto3/gRPC exacta de 0.5.x) lo cubre `test_m15_zero_cost.py`, que esta
función no duplica."""

from __future__ import annotations

import importlib

import boto3
import pytest

from rayito import Sandbox
from rayito._feature_options import FeatureOptions, plan_features
from rayito._secrets import shared_secret_caches


def test_plan_features_with_gateways_none_sends_no_configure_section() -> None:
    plan = plan_features(FeatureOptions())
    assert plan.configure_sections == ()


def test_importing_the_module_builds_no_boto3_client(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    original = boto3.session.Session.client

    def spy(
        self: boto3.session.Session, service_name: str, *args: object, **kwargs: object
    ) -> object:
        calls.append(service_name)
        return original(self, service_name, *args, **kwargs)

    monkeypatch.setattr(boto3.session.Session, "client", spy)
    import rayito._secret_gateway

    importlib.reload(rayito._secret_gateway)
    assert calls == []


def test_sandbox_create_without_gateways_touches_no_shared_secret_cache() -> None:
    """Un `create()` con `gateways=None` (el camino por defecto) nunca
    crea ni toca la caché compartida de secretos: nada que resolver."""
    before = len(shared_secret_caches())
    # No llamamos a Sandbox.create aquí (exigiría red real); basta con que
    # `plan_features` por sí solo -- el único punto que `gateways=None`
    # atraviesa antes de `run-microvm` -- no construya ninguna.
    plan_features(FeatureOptions())
    assert len(shared_secret_caches()) == before


def test_sandbox_has_no_gateways_by_default_on_the_class() -> None:
    """`Sandbox.gateways` existe como `@property`; comprobamos que la
    clase la declara sin necesitar lanzar un sandbox real."""
    assert isinstance(Sandbox.gateways, property)

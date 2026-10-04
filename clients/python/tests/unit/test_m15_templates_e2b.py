"""Shim `e2b.Template` (m15-templates): la firma de build de E2B
(`alias=`, `skip_cache=`, `cpu_count=`, `memory_mb=`) se traduce a la
nativa; `bucket`/`region`/`session` llegan del cliente `E2B(...)`; un
tamaño no soportado se redondea con aviso; sin bucket, error claro."""

from __future__ import annotations

from typing import Any

import pytest

from rayito._templates import _dsl
from rayito.e2b import E2B, Template
from rayito.e2b._template import resolve_memory_mb
from rayito.exceptions import InvalidArgumentException, RayitoCompatWarning


@pytest.fixture
def native_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def _fake_build(template: Any, name: str, **kwargs: Any) -> str:
        calls.append({"name": name, **kwargs})
        return "info"

    monkeypatch.setattr("rayito._templates._build.build", _fake_build)
    monkeypatch.setattr("rayito._templates._build.build_in_background", _fake_build)
    return calls


def test_build_accepts_e2bs_alias_and_skip_cache(native_calls: list[dict[str, Any]]) -> None:
    Template.build(Template().from_base_image(), alias="mi-template", skip_cache=True, bucket="b")

    assert native_calls[0]["name"] == "mi-template"
    assert native_calls[0]["force"] is True
    assert native_calls[0]["bucket"] == "b"


def test_the_client_binds_region_session_and_bucket(native_calls: list[dict[str, Any]]) -> None:
    session = object()
    client = E2B(region="eu-west-1", session=session, bucket="bound-bucket")

    client.Template.build(Template().from_base_image(), "mi-template")
    client.Template.build_in_background(Template().from_base_image(), "otro", bucket="call-wins")

    assert native_calls[0]["region"] == "eu-west-1"
    assert native_calls[0]["session"] is session
    assert native_calls[0]["bucket"] == "bound-bucket"
    assert native_calls[1]["bucket"] == "call-wins"


def test_without_a_bucket_the_error_names_the_option() -> None:
    with pytest.raises(InvalidArgumentException, match=r"E2B\(bucket=\.\.\.\)"):
        Template.build(Template().from_base_image(), "mi-template")


def test_memory_mb_rounds_up_with_a_warning(native_calls: list[dict[str, Any]]) -> None:
    with pytest.warns(RayitoCompatWarning, match="redondeado a 2048"):
        Template.build(Template().from_base_image(), "t", memory_mb=1500, bucket="b")
    assert native_calls[0]["memory_mb"] == 2048


def test_memory_mb_above_the_maximum_is_rejected() -> None:
    with pytest.raises(InvalidArgumentException):
        resolve_memory_mb(16384)


def test_an_exact_size_does_not_warn() -> None:
    assert resolve_memory_mb(1024) == 1024
    assert resolve_memory_mb(None) is None


def test_cpu_count_is_ignored_with_a_warning(native_calls: list[dict[str, Any]]) -> None:
    with pytest.warns(RayitoCompatWarning, match="cpu_count ignorado"):
        Template.build(Template().from_base_image(), "t", cpu_count=4, bucket="b")
    assert native_calls[0].get("cpu_count") is None


def test_e2b_api_params_are_ignored_with_a_warning(native_calls: list[dict[str, Any]]) -> None:
    with pytest.warns(RayitoCompatWarning, match="api_key ignorado"):
        Template.build(Template().from_base_image(), "t", bucket="b", api_key="e2b_x")


def test_an_unknown_keyword_is_a_type_error() -> None:
    with pytest.raises(TypeError):
        Template.build(Template().from_base_image(), "t", bucket="b", no_such_option=1)


def test_the_dsl_is_still_the_native_one() -> None:
    assert isinstance(Template().from_base_image(), _dsl.Template)

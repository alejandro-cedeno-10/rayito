"""`rayito.e2b.Secret`/`AsyncSecret` frente a `e2b/secret/` de E2B 2.51.0
(PyPI `e2b==2.51.0`, `e2b/secret/{base,secret_sync,secret_async,types}.py`,
descargado y leído el 2026-09-30): mismos nombres, parámetros y resultados,
y las divergencias escritas en docs/site/docs/e2b-compat.md."""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import subprocess
import sys
import warnings
from collections.abc import Iterator
from typing import Any, cast

import pytest

import rayito.e2b._secret as shim
from rayito.e2b import (
    E2B,
    AsyncSecret,
    AsyncSecretPaginator,
    InvalidArgumentException,
    NotFoundException,
    RayitoCompatWarning,
    Secret,
    SecretException,
    SecretInfo,
    SecretNotFoundException,
    SecretPaginator,
    UnimplementedError,
)
from rayito.exceptions import NotFoundException as NativeNotFound

from .fake_secrets import SENTINEL_NAME, SENTINEL_VALUE, FakeSecretsManager, SpySession

# Firmas de E2B 2.51.0, copiadas del paquete descargado (sin `cls`/`self`).
E2B_SIGNATURES: dict[str, list[tuple[str, str]]] = {
    "create": [
        ("name", "POSITIONAL_OR_KEYWORD"),
        ("value", "POSITIONAL_OR_KEYWORD"),
        ("metadata", "POSITIONAL_OR_KEYWORD"),
        ("opts", "VAR_KEYWORD"),
    ],
    "update": [
        ("secret", "POSITIONAL_OR_KEYWORD"),
        ("value", "POSITIONAL_OR_KEYWORD"),
        ("metadata", "POSITIONAL_OR_KEYWORD"),
        ("opts", "VAR_KEYWORD"),
    ],
    "get_info": [("secret", "POSITIONAL_OR_KEYWORD"), ("opts", "VAR_KEYWORD")],
    "list": [
        ("limit", "POSITIONAL_OR_KEYWORD"),
        ("next_token", "POSITIONAL_OR_KEYWORD"),
        ("opts", "VAR_KEYWORD"),
    ],
    "exists": [("secret", "POSITIONAL_OR_KEYWORD"), ("opts", "VAR_KEYWORD")],
    "destroy": [("secret", "POSITIONAL_OR_KEYWORD"), ("opts", "VAR_KEYWORD")],
    "fill": [("secret", "POSITIONAL_OR_KEYWORD")],
    "iam_token": [("audience", "KEYWORD_ONLY"), ("token_type", "KEYWORD_ONLY")],
}
E2B_ASYNC_METHODS = {"create", "update", "get_info", "exists", "destroy"}
E2B_SECRET_INFO_FIELDS = ["secret_id", "name", "version", "metadata", "created_at", "updated_at"]


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeSecretsManager]:
    fake = FakeSecretsManager()
    session = SpySession(api=fake)
    monkeypatch.setattr(shim, "_stores", {})
    real = shim.resolve_store

    def with_session(bound: Any, opts: Any, *, call: str) -> Any:
        return real({"session": session, **bound}, opts, call=call)

    monkeypatch.setattr(shim, "resolve_store", with_session)
    yield fake


@pytest.mark.parametrize("resource", [Secret, AsyncSecret])
@pytest.mark.parametrize("method", sorted(E2B_SIGNATURES))
def test_every_e2b_method_exists_with_the_e2b_signature(resource: Any, method: str) -> None:
    signature = inspect.signature(getattr(resource, method))
    shape = [(name, param.kind.name) for name, param in signature.parameters.items()]
    assert shape == E2B_SIGNATURES[method]
    is_async = inspect.iscoroutinefunction(getattr(resource, method))
    assert is_async == (resource is AsyncSecret and method in E2B_ASYNC_METHODS)


def test_secret_info_has_the_e2b_fields() -> None:
    assert [field.name for field in dataclasses.fields(SecretInfo)] == E2B_SECRET_INFO_FIELDS


def test_the_exception_hierarchy_matches_e2b_plus_the_native_not_found() -> None:
    assert issubclass(SecretNotFoundException, SecretException)
    assert issubclass(SecretNotFoundException, NotFoundException)
    assert NotFoundException is NativeNotFound


def test_fill_is_the_literal_e2b_placeholder_without_any_call(api: FakeSecretsManager) -> None:
    assert Secret.fill("openai-api-key") == "${e2b.secrets.openai-api-key}"
    assert AsyncSecret.fill("x") == "${e2b.secrets.x}"
    with pytest.raises(InvalidArgumentException):
        Secret.fill("bad}name")
    assert api.calls == {}


def test_crud_round_trip_over_secrets_manager(api: FakeSecretsManager) -> None:
    info = Secret.create("OpenAI-Key", SENTINEL_VALUE, {"team": "ml"})
    assert info.name == "openai-key"
    assert info.secret_id.startswith("arn:aws:secretsmanager:")
    assert info.version == 1
    assert "rayito/openai-key" in api.secrets
    assert Secret.exists("openai-key") is True
    updated = Secret.update(info.secret_id, "rotated")
    assert updated.version == 2
    assert updated.metadata == {"team": "ml"}
    got = Secret.get_info("openai-key")
    assert (got.version, got.metadata) == (2, {"team": "ml"})
    assert Secret.destroy("openai-key") is True
    assert Secret.destroy("openai-key") is False
    assert Secret.exists("openai-key") is False


def test_destroy_of_a_name_that_never_existed_is_false_like_e2b(api: FakeSecretsManager) -> None:
    assert Secret.destroy("never-created") is False
    assert asyncio.run(AsyncSecret.destroy("never-created")) is False
    assert api.count("DeleteSecret") == 0


def test_not_found_is_secret_not_found_and_native_not_found(api: FakeSecretsManager) -> None:
    with pytest.raises(SecretNotFoundException) as excinfo:
        Secret.get_info(SENTINEL_NAME)
    assert isinstance(excinfo.value, NativeNotFound)
    assert SENTINEL_NAME not in str(excinfo.value)
    with pytest.raises(SecretNotFoundException):
        Secret.update(SENTINEL_NAME, "v")


@pytest.mark.parametrize("name", ["", "x" * 129, "has space", "sec_reserved", "SEC_upper"])
def test_names_are_validated_like_the_e2b_api_before_calling_aws(
    api: FakeSecretsManager, name: str
) -> None:
    with pytest.raises(InvalidArgumentException) as excinfo:
        Secret.create(name, "v")
    if name:
        assert name not in str(excinfo.value)
    assert api.calls == {}


def test_the_paginator_walks_pages_like_e2b(api: FakeSecretsManager) -> None:
    for index in range(5):
        Secret.create(f"s{index}", "v")
    paginator = Secret.list(limit=2)
    assert isinstance(paginator, SecretPaginator)
    pages: list[list[str]] = []
    while paginator.has_next:
        pages.append([info.name for info in paginator.next_items()])
    assert pages == [["s0", "s1"], ["s2", "s3"], ["s4"]]
    assert paginator.next_token is None
    with pytest.raises(Exception, match="No more items to fetch"):
        paginator.next_items()


def test_async_secret_and_paginator(api: FakeSecretsManager) -> None:
    async def main() -> list[str]:
        await AsyncSecret.create("a", "v")
        await AsyncSecret.create("b", "v")
        assert await AsyncSecret.exists("a")
        paginator = AsyncSecret.list(limit=1)
        assert isinstance(paginator, AsyncSecretPaginator)
        names: list[str] = []
        while paginator.has_next:
            names.extend(info.name for info in await paginator.next_items())
        assert await AsyncSecret.destroy("a")
        return names

    assert asyncio.run(main()) == ["a", "b"]


def test_iam_token_stays_unimplemented_with_the_row_24_reason() -> None:
    for resource in (Secret, AsyncSecret):
        with pytest.raises(UnimplementedError) as excinfo:
            resource.iam_token(audience="sts.amazonaws.com", token_type="JWT-SVID")
        assert excinfo.value.feature == "iam"


def test_e2b_api_params_warn_and_are_ignored(api: FakeSecretsManager) -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        Secret.create("a", "v", api_key="e2b_live_key", domain="e2b.app")
    names = sorted(
        str(w.message).split(" ")[0] for w in caught if w.category is RayitoCompatWarning
    )
    assert names == ["api_key", "domain"]
    assert all("e2b_live_key" not in str(w.message) for w in caught)
    with pytest.raises(TypeError):
        Secret.exists("a", not_an_e2b_param=1)


def test_the_bound_client_class_carries_region_and_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeSecretsManager()
    session = SpySession(api=fake)
    monkeypatch.setattr(shim, "_stores", {})
    client = E2B(region="eu-west-1", session=cast(Any, session))
    client.Secret.create("bound", "v")
    assert "rayito/bound" in fake.secrets
    assert session.built == ["secretsmanager"]
    store = next(iter(shim._stores.values()))
    assert store.region == "eu-west-1"
    assert client.AsyncSecret.fill("x") == "${e2b.secrets.x}"


def test_secret_prefix_and_kms_key_are_rayito_options(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeSecretsManager()
    monkeypatch.setattr(shim, "_stores", {})
    Secret.create(
        "k",
        "v",
        session=cast(Any, SpySession(api=fake)),
        secret_prefix="team/",
        kms_key_id="alias/x",
    )
    assert "team/k" in fake.secrets
    assert fake.requests[0][1]["KmsKeyId"] == "alias/x"


def test_importing_rayito_and_the_shim_builds_no_client() -> None:
    probe = (
        "import boto3.session as s\n"
        "built = []\n"
        "real = s.Session.client\n"
        "def spy(self, name, *a, **k):\n"
        "    built.append(name)\n"
        "    return real(self, name, *a, **k)\n"
        "s.Session.client = spy\n"
        "import rayito, rayito.e2b, rayito.e2b._secret as shim\n"
        "assert shim._stores == {} and 'secretsmanager' not in built, built\n"
    )
    subprocess.run([sys.executable, "-c", probe], check=True)

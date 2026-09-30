"""`secrets=` del SDK síncrono contra el `rayd` falso: el valor llega a
`StartRequest.envs`, `PtyStart.envs`, `ExecuteRequest.envs` y
`CreateContextRequest.envs`; tres comandos hacen una sola lectura; nunca
viaja en el `runHookPayload` ni en `LaunchOptions`; sin `secrets=` no se
construye ningún cliente `secretsmanager`."""

from __future__ import annotations

import threading
import warnings
from collections.abc import Iterator
from typing import Any, cast

import boto3
import pytest

import rayito._secrets as secrets_module
from rayito import (
    CodeContext,
    PtySize,
    RayitoCompatWarning,
    Sandbox,
    SecretCache,
    SecretRef,
    SecretStore,
)
from rayito._secrets import code_secrets_scope
from rayito.exceptions import InvalidArgumentException, SecretNotFoundException

from .conftest import ACCESS_TOKEN, IMAGE_ARN, SANDBOX_ID, RaydEndpoint, StubbedControlPlane
from .fake_secrets import SENTINEL_NAME, SENTINEL_VALUE, FakeSecretsManager, SpySession
from .test_commands_sync import stub_launch

GH_VALUE = "ghp-SECOND-SENTINEL"


@pytest.fixture
def secret_api() -> FakeSecretsManager:
    api = FakeSecretsManager()
    api.put(f"rayito/{SENTINEL_NAME}", SENTINEL_VALUE)
    api.put("rayito/gh", GH_VALUE)
    return api


@pytest.fixture
def cache(secret_api: FakeSecretsManager) -> SecretCache:
    return SecretCache(store=SecretStore(session=cast(Any, SpySession(api=secret_api))))


@pytest.fixture
def run_payloads(control_plane: StubbedControlPlane) -> list[dict[str, Any]]:
    """Los parámetros reales de cada `RunMicrovm` (con su `runHookPayload`)."""
    captured: list[dict[str, Any]] = []

    def capture(params: dict[str, Any], **kwargs: Any) -> None:
        captured.append(dict(params))

    control_plane.plane._client.meta.events.register(
        "provide-client-params.lambda-microvms.RunMicrovm", capture
    )
    return captured


@pytest.fixture
def sandbox(
    control_plane: StubbedControlPlane,
    fake_rayd: RaydEndpoint,
    cache: SecretCache,
    run_payloads: list[dict[str, Any]],
) -> Iterator[Sandbox]:
    stub_launch(control_plane, fake_rayd)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RayitoCompatWarning)
        created = Sandbox.create(
            IMAGE_ARN,
            idle=None,
            access_token=ACCESS_TOKEN,
            control_plane=control_plane.plane,
            transport=fake_rayd.transport,
            envs={"PLAIN": "1"},
            secrets={"OPENAI_API_KEY": SENTINEL_NAME},
            secret_cache=cache,
        )
    try:
        yield created
    finally:
        control_plane.microvms.add_response(
            "terminate_microvm", {}, expected_params={"microvmIdentifier": SANDBOX_ID}
        )
        created.kill()


def test_three_commands_make_one_secrets_manager_read(
    sandbox: Sandbox, fake_rayd: RaydEndpoint, secret_api: FakeSecretsManager
) -> None:
    for _ in range(3):
        result = sandbox.commands.run("env")
        assert f"OPENAI_API_KEY={SENTINEL_VALUE}\n" in result.stdout
    assert secret_api.count("GetSecretValue") == 1
    for request in fake_rayd.process.start_requests[-3:]:
        assert dict(request.process.envs)["OPENAI_API_KEY"] == SENTINEL_VALUE


def test_the_run_hook_payload_never_carries_the_value_or_the_name(
    sandbox: Sandbox, run_payloads: list[dict[str, Any]]
) -> None:
    assert len(run_payloads) == 1
    payload = run_payloads[0]["runHookPayload"]
    assert '"PLAIN"' in payload
    assert SENTINEL_VALUE not in payload
    assert SENTINEL_NAME not in payload
    assert "OPENAI_API_KEY" not in payload
    assert SENTINEL_VALUE not in repr(run_payloads)


def test_launch_options_and_repr_hold_only_references(sandbox: Sandbox) -> None:
    options = sandbox._launch_options
    assert options is not None
    assert options.secrets == {"OPENAI_API_KEY": SecretRef(SENTINEL_NAME)}
    rendered = repr(options)
    assert "secrets=<1 keys>" in rendered
    assert SENTINEL_NAME not in rendered
    assert SENTINEL_VALUE not in rendered
    assert SENTINEL_VALUE not in repr(sandbox)
    assert SENTINEL_VALUE not in repr(sandbox._secrets)
    assert SENTINEL_NAME not in repr(sandbox._secrets)


def test_call_secrets_merge_with_the_handle_and_the_call_wins(
    sandbox: Sandbox, fake_rayd: RaydEndpoint
) -> None:
    sandbox.commands.run("env", secrets={"GH_TOKEN": "gh"})
    envs = dict(fake_rayd.process.start_requests[-1].process.envs)
    assert envs["GH_TOKEN"] == GH_VALUE
    assert envs["OPENAI_API_KEY"] == SENTINEL_VALUE
    sandbox.commands.run("env", secrets={"OPENAI_API_KEY": "gh"})
    assert dict(fake_rayd.process.start_requests[-1].process.envs)["OPENAI_API_KEY"] == GH_VALUE


def test_a_key_in_both_envs_and_secrets_is_rejected_before_starting(
    sandbox: Sandbox, fake_rayd: RaydEndpoint
) -> None:
    before = len(fake_rayd.process.start_requests)
    with pytest.raises(InvalidArgumentException, match="OPENAI_API_KEY") as excinfo:
        sandbox.commands.run("env", envs={"OPENAI_API_KEY": "shadow"})
    assert SENTINEL_NAME not in str(excinfo.value)
    with pytest.raises(InvalidArgumentException, match="GH"):
        sandbox.commands.run("env", envs={"GH": "x"}, secrets={"GH": "gh"})
    assert len(fake_rayd.process.start_requests) == before


def test_background_commands_get_the_secrets_too(sandbox: Sandbox, fake_rayd: RaydEndpoint) -> None:
    handle = sandbox.commands.run("env", background=True)
    handle.wait()
    assert dict(fake_rayd.process.start_requests[-1].process.envs)["OPENAI_API_KEY"] == (
        SENTINEL_VALUE
    )


def test_pty_create_carries_the_secrets(sandbox: Sandbox, fake_rayd: RaydEndpoint) -> None:
    terminal = sandbox.pty.create(size=PtySize(cols=80, rows=24), timeout=None)
    try:
        envs = dict(fake_rayd.pty.create_requests[-1].envs)
        assert envs["OPENAI_API_KEY"] == SENTINEL_VALUE
    finally:
        terminal.kill()
    other = sandbox.pty.create(timeout=None, secrets={"GH": "gh"})
    try:
        assert dict(fake_rayd.pty.create_requests[-1].envs)["GH"] == GH_VALUE
    finally:
        other.kill()


def test_run_code_and_contexts_carry_the_secrets(
    sandbox: Sandbox, fake_rayd: RaydEndpoint, secret_api: FakeSecretsManager
) -> None:
    sandbox.run_code("1+1", secrets={"GH": "gh"})
    envs = dict(fake_rayd.code.execute_requests[-1].envs)
    assert envs == {"OPENAI_API_KEY": SENTINEL_VALUE, "GH": GH_VALUE}
    context = sandbox.create_code_context(secrets={"GH": "gh"})
    created = dict(fake_rayd.code.create_requests[-1].envs)
    assert created == {"OPENAI_API_KEY": SENTINEL_VALUE, "GH": GH_VALUE}
    sandbox.remove_code_context(context)
    assert secret_api.count("GetSecretValue") == 2


def test_run_code_secrets_on_a_non_python_language_point_to_contexts(sandbox: Sandbox) -> None:
    with pytest.raises(InvalidArgumentException, match="create_code_context"):
        sandbox.run_code("echo $GH", language="bash", secrets={"GH": "gh"})
    bash = CodeContext(id="ctx-bash", language="bash", cwd="/home/user")
    with pytest.raises(InvalidArgumentException, match="create_code_context"):
        sandbox.run_code("echo $GH", context=bash, secrets={"GH": "gh"})


def test_handle_secrets_are_not_added_to_non_python_cells() -> None:
    assert code_secrets_scope(None, None, None) is True
    assert code_secrets_scope("python", None, {"A": "a"}) is True
    assert code_secrets_scope(None, "bash", None) is False
    assert code_secrets_scope("typescript", None, {}) is False


def test_connect_rebinds_and_none_keeps_the_handle_secrets(
    sandbox: Sandbox,
    control_plane: StubbedControlPlane,
    fake_rayd: RaydEndpoint,
    cache: SecretCache,
) -> None:
    from .conftest import microvm_response

    for _ in range(2):
        control_plane.microvms.add_response(
            "get_microvm", microvm_response(endpoint=fake_rayd.host)
        )
    sandbox.connect(secrets={"OPENAI_API_KEY": "gh"}, secret_cache=cache)
    sandbox.commands.run("env")
    assert dict(fake_rayd.process.start_requests[-1].process.envs)["OPENAI_API_KEY"] == GH_VALUE
    sandbox.connect()
    sandbox.commands.run("env")
    assert dict(fake_rayd.process.start_requests[-1].process.envs)["OPENAI_API_KEY"] == GH_VALUE


def test_a_missing_secret_fails_before_run_microvm(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint, cache: SecretCache
) -> None:
    with pytest.raises(SecretNotFoundException) as excinfo:
        Sandbox.create(
            IMAGE_ARN,
            idle=None,
            access_token=ACCESS_TOKEN,
            control_plane=control_plane.plane,
            transport=fake_rayd.transport,
            secrets={"MISSING": "does-not-exist"},
            secret_cache=cache,
        )
    assert "does-not-exist" not in str(excinfo.value)


def test_the_first_use_warns_once_that_the_value_is_visible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(secrets_module, "_visibility_warned", threading.Event())
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        secrets_module.normalize_secrets({"A": "a"})
        secrets_module.normalize_secrets({"B": "b"})
        secrets_module.normalize_secrets(None)
    visible = [w for w in caught if issubclass(w.category, RayitoCompatWarning)]
    assert len(visible) == 1
    assert "visible para el código del sandbox" in str(visible[0].message)


def test_invalid_secrets_arguments_name_the_env_key_never_the_secret() -> None:
    with pytest.raises(InvalidArgumentException):
        secrets_module.normalize_secrets({"": "a"})
    with pytest.raises(InvalidArgumentException):
        secrets_module.normalize_secrets({"A=B": "a"})
    with pytest.raises(InvalidArgumentException, match="'KEY'"):
        secrets_module.normalize_secrets({"KEY": cast(Any, 42)})
    with pytest.raises(InvalidArgumentException):
        secrets_module.bind_secrets(None, cast(Any, "not-a-cache"))


def test_without_secrets_no_secretsmanager_client_is_ever_built(
    control_plane: StubbedControlPlane,
    fake_rayd: RaydEndpoint,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: list[str] = []
    real_client = boto3.session.Session.client

    def spy(self: boto3.session.Session, service: str, *args: Any, **kwargs: Any) -> Any:
        built.append(service)
        return real_client(self, service, *args, **kwargs)

    monkeypatch.setattr(boto3.session.Session, "client", spy)
    shared_before = dict(secrets_module._shared_caches)
    stub_launch(control_plane, fake_rayd)
    plain = Sandbox.create(
        IMAGE_ARN,
        idle=None,
        access_token=ACCESS_TOKEN,
        control_plane=control_plane.plane,
        transport=fake_rayd.transport,
    )
    try:
        plain.commands.run("env")
        plain.run_code("1+1")
        assert plain._secrets is None
        assert plain._launch_options is not None and plain._launch_options.secrets is None
    finally:
        control_plane.microvms.add_response(
            "terminate_microvm", {}, expected_params={"microvmIdentifier": SANDBOX_ID}
        )
        plain.kill()
    assert "secretsmanager" not in built
    assert secrets_module._shared_caches == shared_before

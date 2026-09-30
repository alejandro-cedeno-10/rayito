"""`secrets=` del SDK asíncrono: la misma superficie que la versión síncrona
contra el mismo `rayd` falso; un fallo de caché lee Secrets Manager en
`asyncio.to_thread` y un acierto no sale del bucle."""

from __future__ import annotations

import asyncio
import warnings
from collections.abc import AsyncIterator
from typing import Any, cast

import pytest

from rayito import AsyncSandbox, RayitoCompatWarning, SecretCache, SecretRef, SecretStore
from rayito.exceptions import InvalidArgumentException, SecretNotFoundException

from .conftest import ACCESS_TOKEN, IMAGE_ARN, SANDBOX_ID, RaydEndpoint, StubbedControlPlane
from .fake_secrets import SENTINEL_NAME, SENTINEL_VALUE, FakeSecretsManager, SpySession
from .test_commands_async import stub_launch

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
async def sandbox(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint, cache: SecretCache
) -> AsyncIterator[AsyncSandbox]:
    stub_launch(control_plane, fake_rayd)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RayitoCompatWarning)
        created = await AsyncSandbox.create(
            IMAGE_ARN,
            idle=None,
            access_token=ACCESS_TOKEN,
            control_plane=control_plane.plane,
            transport=fake_rayd.transport,
            secrets={"OPENAI_API_KEY": SENTINEL_NAME},
            secret_cache=cache,
        )
    try:
        yield created
    finally:
        control_plane.microvms.add_response(
            "terminate_microvm", {}, expected_params={"microvmIdentifier": SANDBOX_ID}
        )
        await created.kill()


async def test_three_commands_make_one_read(
    sandbox: AsyncSandbox, fake_rayd: RaydEndpoint, secret_api: FakeSecretsManager
) -> None:
    for _ in range(3):
        result = await sandbox.commands.run("env")
        assert f"OPENAI_API_KEY={SENTINEL_VALUE}\n" in result.stdout
    assert secret_api.count("GetSecretValue") == 1
    assert sandbox._launch_options is not None
    assert sandbox._launch_options.secrets == {"OPENAI_API_KEY": SecretRef(SENTINEL_NAME)}
    assert SENTINEL_VALUE not in repr(sandbox._launch_options)


async def test_concurrent_commands_share_one_read(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint, secret_api: FakeSecretsManager
) -> None:
    secret_api.get_delay = 0.1
    fresh = SecretCache(store=SecretStore(session=cast(Any, SpySession(api=secret_api))))
    stub_launch(control_plane, fake_rayd)
    created = await AsyncSandbox.create(
        IMAGE_ARN,
        idle=None,
        access_token=ACCESS_TOKEN,
        control_plane=control_plane.plane,
        transport=fake_rayd.transport,
        secret_cache=fresh,
    )
    try:
        await asyncio.gather(*(created.commands.run("env", secrets={"GH": "gh"}) for _ in range(5)))
        assert secret_api.count("GetSecretValue") == 1
    finally:
        control_plane.microvms.add_response(
            "terminate_microvm", {}, expected_params={"microvmIdentifier": SANDBOX_ID}
        )
        await created.kill()


async def test_pty_code_and_contexts_carry_the_secrets(
    sandbox: AsyncSandbox, fake_rayd: RaydEndpoint
) -> None:
    terminal = await sandbox.pty.create(timeout=None, secrets={"GH": "gh"})
    try:
        envs = dict(fake_rayd.pty.create_requests[-1].envs)
        assert (envs["OPENAI_API_KEY"], envs["GH"]) == (SENTINEL_VALUE, GH_VALUE)
    finally:
        await terminal.kill()
    await sandbox.run_code("1+1")
    assert dict(fake_rayd.code.execute_requests[-1].envs) == {"OPENAI_API_KEY": SENTINEL_VALUE}
    context = await sandbox.create_code_context(secrets={"GH": "gh"})
    assert dict(fake_rayd.code.create_requests[-1].envs)["GH"] == GH_VALUE
    await sandbox.remove_code_context(context)


async def test_conflicts_and_non_python_cells_are_rejected(
    sandbox: AsyncSandbox, fake_rayd: RaydEndpoint
) -> None:
    before = len(fake_rayd.process.start_requests)
    with pytest.raises(InvalidArgumentException, match="OPENAI_API_KEY"):
        await sandbox.commands.run("env", envs={"OPENAI_API_KEY": "shadow"})
    assert len(fake_rayd.process.start_requests) == before
    with pytest.raises(InvalidArgumentException, match="create_code_context"):
        await sandbox.run_code("echo", language="bash", secrets={"GH": "gh"})


async def test_connect_rebinds(
    sandbox: AsyncSandbox,
    control_plane: StubbedControlPlane,
    fake_rayd: RaydEndpoint,
    cache: SecretCache,
) -> None:
    from .conftest import microvm_response

    control_plane.microvms.add_response("get_microvm", microvm_response(endpoint=fake_rayd.host))
    await sandbox.connect(secrets={"OPENAI_API_KEY": "gh"}, secret_cache=cache)
    await sandbox.commands.run("env")
    assert dict(fake_rayd.process.start_requests[-1].process.envs)["OPENAI_API_KEY"] == GH_VALUE


async def test_a_missing_secret_fails_before_run_microvm(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint, cache: SecretCache
) -> None:
    with pytest.raises(SecretNotFoundException):
        await AsyncSandbox.create(
            IMAGE_ARN,
            idle=None,
            access_token=ACCESS_TOKEN,
            control_plane=control_plane.plane,
            transport=fake_rayd.transport,
            secrets={"MISSING": "does-not-exist"},
            secret_cache=cache,
        )

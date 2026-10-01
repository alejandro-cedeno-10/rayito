"""M13a contra AWS real (`RAYITO_E2E=1`): CRUD por el shim `Secret` de E2B
sobre Secrets Manager → `Sandbox.create(secrets=)` → `printenv` tres veces con
UNA sola `GetSecretValue` (contada con el hook de eventos de botocore) →
`destroy`. Mide SEC-9 (recreación tras borrado forzado, choque de
`ClientRequestToken`) y SEC-10 (el valor no aparece en los logs del SDK).

Coste: un secreto durante < 5 min (≈ $0,0005) + ~10 llamadas a Secrets
Manager + un sandbox (~$0,03)."""

from __future__ import annotations

import contextlib
import logging
import secrets as stdlib_secrets
import time
import warnings
from typing import Any

import pytest
from botocore.exceptions import ClientError

from rayito import Sandbox, SecretCache, SecretStore
from rayito._aws import LambdaMicrovmsControlPlane
from rayito._secrets import is_scheduled_for_deletion, version_token
from rayito.e2b import Secret
from rayito.exceptions import SandboxNotFoundException

from .conftest import TEST_SANDBOX_TIMEOUT_SECONDS, E2ESettings

pytestmark = pytest.mark.e2e

RECREATE_BUDGET_SECONDS = 60.0


def count_calls(store: SecretStore, operation: str) -> list[int]:
    counter = [0]

    def on_call(**kwargs: Any) -> None:
        counter[0] += 1

    store.api().meta.events.register(  # type: ignore[attr-defined]
        f"before-call.secrets-manager.{operation}", on_call
    )
    return counter


def test_secret_crud_injection_and_hygiene(
    e2e_settings: E2ESettings,
    control_plane: LambdaMicrovmsControlPlane,
    template_arn: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    name = f"e2e-{stdlib_secrets.token_hex(6)}"
    value = f"sentinel-{stdlib_secrets.token_hex(16)}"
    region = e2e_settings.region or control_plane.region
    info = Secret.create(name, value, {"suite": "m13a"}, region=region)
    try:
        assert info.version == 1
        assert Secret.exists(name, region=region)
        cache = SecretCache(ttl_seconds=300, region=region)
        reads = count_calls(cache.store, "GetSecretValue")
        sandbox = Sandbox.create(
            template_arn,
            template_version=e2e_settings.template_version,
            timeout=TEST_SANDBOX_TIMEOUT_SECONDS,
            idle=None,
            execution_role_arn=e2e_settings.execution_role_arn,
            logging=e2e_settings.logging,
            control_plane=control_plane,
            secrets={"RAYITO_E2E_SECRET": name},
            secret_cache=cache,
        )
        try:
            for _ in range(3):
                result = sandbox.commands.run("printenv RAYITO_E2E_SECRET")
                assert result.stdout.strip() == value
            assert reads[0] == 1, f"se esperaba 1 GetSecretValue, hubo {reads[0]}"
            assert value not in repr(sandbox._launch_options)
            assert value not in repr(sandbox._secrets)
        finally:
            with contextlib.suppress(SandboxNotFoundException):
                sandbox.kill()

        updated = Secret.update(name, f"{value}-2", region=region)
        assert updated.version == 2
        measure_token_clash(name, region)
    finally:
        assert Secret.destroy(name, region=region)
    measure_recreate_after_force_delete(name, region)
    assert value not in caplog.text, "SEC-10: el valor apareció en los logs del SDK"
    assert name not in caplog.text, "SEC-10: el nombre apareció en los logs del SDK"


def measure_token_clash(name: str, region: str) -> None:
    """SEC-9 (b): `PutSecretValue` con un token ya usado y otro valor."""
    store = SecretStore(region=region)
    secret_id = f"rayito/{name}"
    try:
        store.api().put_secret_value(
            SecretId=secret_id,
            SecretString="different",
            ClientRequestToken="rayito-secret-version-00000000000000000002",
        )
        print("\nSEC-9 (b): PutSecretValue con token repetido y otro valor NO falló", flush=True)
    except Exception as exc:  # medición: se imprime el código tal cual
        code = getattr(exc, "response", {}).get("Error", {}).get("Code")
        print(f"\nSEC-9 (b): token repetido + otro valor -> {code}", flush=True)


def measure_recreate_after_force_delete(name: str, region: str) -> None:
    """SEC-9 (a): con qué código y mensaje falla un `CreateSecret` inmediato
    tras el borrado forzado (`is_scheduled_for_deletion` supone
    `InvalidRequestException` con "delet" en el mensaje) y cuánto tarda en
    aceptarse con el reintento de `create`. SEC-9 (d): el nombre recreado
    vuelve a empezar en la versión 1 y `update` escribe el token 2 sin chocar
    con los tokens del secreto borrado."""
    store = SecretStore(region=region)
    started = time.monotonic()
    try:
        store.api().create_secret(
            Name=f"rayito/{name}", SecretString="probe", ClientRequestToken=version_token(1)
        )
        print("\nSEC-9 (a): CreateSecret inmediato aceptado", flush=True)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        print(
            f"\nSEC-9 (a): CreateSecret inmediato -> {code}; "
            f"is_scheduled_for_deletion={is_scheduled_for_deletion(exc)}",
            flush=True,
        )
        try:
            store.create(name, "recreated")
        finally:
            elapsed = time.monotonic() - started
            print(f"\nSEC-9 (a): recreación con reintento en {elapsed:.2f} s", flush=True)
        assert elapsed < RECREATE_BUDGET_SECONDS
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            recreated = store.update(name, "recreated-2")
        print(f"\nSEC-9 (d): update tras recrear -> versión {recreated.version}", flush=True)
        assert recreated.version == 2
    finally:
        assert store.destroy(name)

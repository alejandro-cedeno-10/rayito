"""M15 `gateways=` contra AWS real (`RAYITO_E2E=1`, `RAYITO_TEMPLATE` = una
imagen con `rayd` 0.6.0 o posterior). Corre sólo en la etapa de aceptación
serializada, nunca desde una rama de función.

Un secreto centinela de un solo uso en Secrets Manager -> `Sandbox.create(
gateways=)` hacia un `upstream` de eco HTTPS público -> desde dentro del
sandbox, con `curl` contra `http://127.0.0.1:<puerto>`:

- inyección: el eco devuelve la cabecera vaultada, y una cabecera del mismo
  nombre puesta por el sandbox nunca llega (T24, anti-suplantación);
- 403 fuera de `allow` y ante un segmento `..` bajo una regla `/*`
  (confuso-diputado) sin tocar el upstream;
- 429 por encima de `rate_per_minute`;
- rotación: `SecretStore.update` + `sbx.gateways.refresh()` conserva el
  puerto y la siguiente petición lleva el valor nuevo;
- SEC-7: una subida troceada (`Transfer-Encoding: chunked`) atraviesa la
  pasarela, y se imprime la latencia añadida frente a `curl` directo;
- SEC-10: ni el valor ni el nombre del secreto aparecen en los logs de
  Rayito, y el uid 1000 no los ve en el entorno de ningún proceso.

`RAYITO_E2E_GATEWAY_UPSTREAM` elige el eco (por defecto
`https://postman-echo.com`, que responde `/headers`, `/get`, `/post` en
JSON). Coste: un secreto durante < 5 min (≈ $0,0005), ~6 llamadas a Secrets
Manager y un sandbox (~$0,03); el eco es externo y gratuito."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import secrets as stdlib_secrets
import time
from typing import Any

import pytest

from rayito import Sandbox, SecretCache, SecretGateway, SecretStore
from rayito._aws import LambdaMicrovmsControlPlane
from rayito.exceptions import SandboxNotFoundException

from .conftest import TEST_SANDBOX_TIMEOUT_SECONDS, E2ESettings

pytestmark = pytest.mark.e2e

UPSTREAM_VAR = "RAYITO_E2E_GATEWAY_UPSTREAM"
DEFAULT_UPSTREAM = "https://postman-echo.com"
ROUTE = "echo"
INJECTED_HEADER = "x-rayito-e2e"
# Capacidad del cubo de la ruta (`refresh()` lo rellena): las peticiones
# permitidas de este test caben en ella; la ráfaga final la supera con
# margen para la recarga (8/min, una cada 7,5 s) que caiga mientras corre.
RATE_PER_MINUTE = 8
BURST_MARGIN = 4
LATENCY_SAMPLES = 3
# SEC-7: la imagen no trae /etc/hostname (aceptación 2026-10-02), así que el
# test escribe su propia carga. 96 KiB supera el búfer de subida de curl
# (64 KiB por defecto), luego viaja en varios trozos, y cabe bajo el límite
# de cuerpo del eco por defecto: postman-echo responde 500 a 128 KiB también
# en directo, sin la pasarela.
UPLOAD_PATH = "/tmp/rayito-e2e-upload"
UPLOAD_BYTES = 96 * 1024
# Secrets Manager es de consistencia eventual: justo después de
# `UpdateSecret`, `GetSecretValue` puede devolver un momento la versión
# anterior, y `refresh()` empuja lo que lee. El test repite `refresh()` +
# petición hasta ver el valor nuevo (cada `refresh()` rellena el cubo de la
# ruta, así que los reintentos no gastan la ráfaga del final).
ROTATION_ATTEMPTS = 6
ROTATION_RETRY_SECONDS = 2.0


def curl(sandbox: Sandbox, args: str) -> tuple[int, str]:
    """`curl` desde dentro del sandbox; devuelve (código HTTP, cuerpo)."""
    result = sandbox.commands.run(
        f"curl -sS -o /tmp/body -w '%{{http_code}}' {args}; echo; cat /tmp/body",
        timeout=60,
    )
    status, _, body = result.stdout.partition("\n")
    return int(status.strip()), body


def echoed_header(body: str) -> str | None:
    headers: dict[str, Any] = json.loads(body).get("headers", {})
    value = headers.get(INJECTED_HEADER)
    return None if value is None else str(value)


def test_gateway_injects_enforces_rotates_and_never_leaks(
    e2e_settings: E2ESettings,
    control_plane: LambdaMicrovmsControlPlane,
    template_arn: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="rayito")
    upstream = os.environ.get(UPSTREAM_VAR) or DEFAULT_UPSTREAM
    region = e2e_settings.region or control_plane.region
    store = SecretStore(region=region)
    name = f"e2e-gw-{stdlib_secrets.token_hex(6)}"
    value = f"sentinel-{stdlib_secrets.token_hex(16)}"
    rotated = f"{value}-rotated"
    store.create(name, value)
    try:
        sandbox = Sandbox.create(
            template_arn,
            template_version=e2e_settings.template_version,
            timeout=TEST_SANDBOX_TIMEOUT_SECONDS,
            idle=None,
            execution_role_arn=e2e_settings.execution_role_arn,
            logging=e2e_settings.logging,
            control_plane=control_plane,
            gateways={
                ROUTE: SecretGateway(
                    upstream=upstream,
                    headers={INJECTED_HEADER: name},
                    allow=[("GET", "/headers"), ("GET", "/get/*"), ("POST", "/post")],
                    rate_per_minute=RATE_PER_MINUTE,
                )
            },
            secret_cache=SecretCache(store=store),
        )
        try:
            url = sandbox.gateways[ROUTE].url
            port = sandbox.gateways[ROUTE].port

            status, body = curl(sandbox, f"-H '{INJECTED_HEADER}: spoofed' {url}/headers")
            assert status == 200, body
            assert echoed_header(body) == value, "la cabecera vaultada no llegó (o llegó la falsa)"

            assert curl(sandbox, f"{url}/status/200")[0] == 403
            assert curl(sandbox, f"--path-as-is {url}/get/../headers")[0] == 403
            assert curl(sandbox, f"--path-as-is {url}/get/%2e%2e/headers")[0] == 403

            sandbox.commands.run(f"head -c {UPLOAD_BYTES} /dev/zero > {UPLOAD_PATH}")
            status, body = curl(
                sandbox,
                "-X POST -H 'Transfer-Encoding: chunked' "
                "-H 'Content-Type: application/octet-stream' "
                f"--data-binary @{UPLOAD_PATH} {url}/post",
            )
            assert status == 200, f"SEC-7: la subida troceada no atravesó la pasarela: {body}"
            report_added_latency(sandbox, url, upstream)

            store.update(name, rotated)
            status, body = refresh_until_rotated(sandbox, url, rotated)
            assert sandbox.gateways[ROUTE].port == port, "refresh() movió el puerto"
            assert status == 200, body
            assert echoed_header(body) == rotated, "refresh() no empujó el valor rotado"

            burst = RATE_PER_MINUTE + BURST_MARGIN
            statuses = [curl(sandbox, f"{url}/headers")[0] for _ in range(burst)]
            assert 429 in statuses, f"el límite de tasa nunca saltó: {statuses}"

            leaked = sandbox.commands.run(
                f"cat /proc/*/environ 2>/dev/null | tr '\\0' '\\n' | grep -c -e {value} || true"
            )
            assert leaked.stdout.strip() in ("", "0"), "SEC-10: el valor está en un environ"
        finally:
            with contextlib.suppress(SandboxNotFoundException):
                sandbox.kill()
    finally:
        store.destroy(name)
    for secret_text in (value, rotated, name):
        assert secret_text not in caplog.text, "SEC-10: el secreto apareció en los logs de Rayito"


def refresh_until_rotated(sandbox: Sandbox, url: str, rotated: str) -> tuple[int, str]:
    """`refresh()` y una petición, repetidos hasta `ROTATION_ATTEMPTS` veces
    mientras el eco no devuelva el valor rotado (ver la constante)."""
    status, body = 0, ""
    for attempt in range(ROTATION_ATTEMPTS):
        if attempt:
            time.sleep(ROTATION_RETRY_SECONDS)
        sandbox.gateways.refresh()
        status, body = curl(sandbox, f"{url}/headers")
        if status == 200 and echoed_header(body) == rotated:
            break
    return status, body


def report_added_latency(sandbox: Sandbox, url: str, upstream: str) -> None:
    """SEC-7: mediana de `time_total` a través de la pasarela frente a
    directo al `upstream`, desde el mismo sandbox (sólo se imprime)."""

    def median_seconds(target: str) -> float:
        samples = []
        for _ in range(LATENCY_SAMPLES):
            result = sandbox.commands.run(
                f"curl -sS -o /dev/null -w '%{{time_total}}' {target}", timeout=60
            )
            samples.append(float(result.stdout.strip()))
        return sorted(samples)[len(samples) // 2]

    through = median_seconds(f"{url}/headers")
    direct = median_seconds(f"{upstream}/headers")
    print(
        f"\nSEC-7: pasarela {through * 1000:.0f} ms vs directo {direct * 1000:.0f} ms "
        f"(añadido {(through - direct) * 1000:.0f} ms)",
        flush=True,
    )

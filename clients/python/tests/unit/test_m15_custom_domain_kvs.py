"""`rayito._custom_domain._kvs.CloudFrontKvsWriter` (m15-custom-domain,
ADR-024): el adaptador boto3 real. Verifica, sin red, que el plano de datos
de `cloudfront-keyvaluestore` firma de verdad con SigV4A — pese a que
`service-2.json` declara `signatureVersion: v4`, es `endpoint-rule-set-1.json`
quien decide el firmante (AWS_API_NOTES.md §29) — y que sin el extra
`awscrt` el fallo se traduce a un `CustomDomainException` legible, nunca al
`MissingDependencyException` crudo de botocore."""

from __future__ import annotations

import importlib.util
from typing import Any

import boto3
import pytest

from rayito._custom_domain._kvs import CloudFrontKvsWriter, _wrap
from rayito.exceptions import CustomDomainException

#: `True` si el extra `rayito[custom-domain]` (`awscrt`) está instalado en
#: este entorno; decide cuál de las dos pruebas "offline contra boto3 real"
#: de abajo puede ejecutarse (son mutuamente excluyentes, nunca las dos a la
#: vez en el mismo entorno).
_AWSCRT_INSTALLED = importlib.util.find_spec("awscrt") is not None

_DUMMY_KVS_ARN = "arn:aws:cloudfront::111122223333:key-value-store/abc123"


def _dummy_session() -> boto3.session.Session:
    # Credenciales ficticias, a propósito: el hook `before-send` de abajo
    # para la petición antes de que salga a red, así que nunca se usan de
    # verdad ni necesitan ser válidas.
    return boto3.session.Session(
        # La access key de ejemplo de la documentación de AWS (termina en
        # EXAMPLE a propósito: `scripts/check_hygiene.py` la reconoce como
        # placeholder, no como una credencial real).
        aws_access_key_id="AKIAIOSFODNN7EXAMPLE",
        aws_secret_access_key="dummy-secret-dummy-secret-dummy-secret",
        region_name="us-east-1",
    )


class _StopBeforeSend(Exception):
    """Corta la petición antes de que `before-send` la mande a red de
    verdad; en ese punto ya está firmada, así que la cabecera
    `Authorization` capturada es la real."""


def test_wrap_maps_missing_crt_dependency_to_a_clear_message() -> None:
    """Puro, no necesita boto3 real ni red: `_wrap` reconoce el
    `MissingDependencyException` de botocore (sin `.response`, por eso
    `aws_code()` no sirve aquí) por su nombre de clase y nombra el extra
    instalable en vez de repetir el mensaje crudo de botocore."""

    class MissingDependencyException(Exception):
        pass

    wrapped = _wrap(
        MissingDependencyException(
            "This operation requires an additional dependency. Use pip install botocore[crt]"
        )
    )
    assert isinstance(wrapped, CustomDomainException)
    assert "rayito[custom-domain]" in str(wrapped)
    assert wrapped.aws_code is None


@pytest.mark.skipif(
    not _AWSCRT_INSTALLED, reason="necesita el extra rayito[custom-domain] (awscrt) instalado"
)
def test_describe_key_value_store_signs_with_sigv4a_when_awscrt_is_installed() -> None:
    """La comprobación offline que de verdad decide D2 del `design.md` del
    cambio: `endpoint-rule-set-1.json` de `cloudfront-keyvaluestore` fija
    `authSchemes: [{"name": "sigv4a", ...}]`, así que con `awscrt` instalado
    boto3 firma con SigV4A (`AWS4-ECDSA-P256-SHA256`), no con SigV4 llano
    (`AWS4-HMAC-SHA256`) pese a `signatureVersion: v4` en `service-2.json`."""
    captured: dict[str, str] = {}

    def _capture(request: Any, **_kwargs: Any) -> None:
        value = request.headers.get("Authorization", "")
        captured["authorization"] = (
            value.decode("utf-8", "replace") if isinstance(value, bytes) else value
        )
        raise _StopBeforeSend

    client = _dummy_session().client("cloudfront-keyvaluestore")
    client.meta.events.register("before-send", _capture)
    with pytest.raises(_StopBeforeSend):
        client.describe_key_value_store(KvsARN=_DUMMY_KVS_ARN)
    assert captured["authorization"].startswith("AWS4-ECDSA-P256-SHA256"), (
        f"se esperaba SigV4A, la petición salió firmada con: {captured['authorization']!r}"
    )


@pytest.mark.skipif(
    _AWSCRT_INSTALLED, reason="sólo reproducible sin el extra rayito[custom-domain] instalado"
)
def test_describe_without_awscrt_raises_a_clear_custom_domain_exception() -> None:
    """La otra mitad de la comprobación offline: en un entorno sin `awscrt`
    (el caso por defecto de `pip install rayito`, sin el extra), la misma
    llamada falla, pero `CloudFrontKvsWriter` la traduce a un mensaje que
    nombra el extra exacto a instalar, no al `MissingDependencyException`
    crudo de botocore."""
    writer = CloudFrontKvsWriter(session=_dummy_session())
    with pytest.raises(CustomDomainException, match=r"rayito\[custom-domain\]"):
        writer.describe(_DUMMY_KVS_ARN)


def test_kvs_module_has_no_static_top_level_awscrt_import() -> None:
    """Hallazgo del review de PR #74: `awscrt` nunca debe ser una
    importación estática de nivel superior de este módulo (eso rompería
    `import rayito._custom_domain._kvs` en el caso por defecto, sin el
    extra `rayito[custom-domain]` instalado) — lo único que hace falta es
    que boto3/botocore lo encuentren *a sí mismos*, perezosamente, al
    firmar de verdad (D2 del cambio). No se comprueba vía `sys.modules`
    tras `import rayito`: boto3 (dependencia obligatoria) ya dispara esa
    detección de `awscrt` por su cuenta en cualquier entorno donde el
    extra esté instalado, para *cualquier* cliente, así que ese síntoma no
    distingue nada que dependa de este módulo."""
    import ast
    from pathlib import Path

    import rayito._custom_domain._kvs as kvs_module

    source = Path(kvs_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    # Sólo el cuerpo de nivel superior del módulo, nunca dentro de una
    # función: una importación perezosa de `awscrt` *ahí dentro* sería
    # legítima (y hoy no hace falta ninguna: boto3/botocore ya lo
    # encuentran solos).
    top_level_imports = {
        alias.name for node in tree.body if isinstance(node, ast.Import) for alias in node.names
    } | {
        node.module
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    offenders = {name for name in top_level_imports if name.split(".")[0] == "awscrt"}
    assert offenders == set()

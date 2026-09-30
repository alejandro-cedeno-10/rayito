"""`require_module`: sólo se importa lo opcional cuando ya se activó una
función (ADR-014), y `import rayito` nunca carga un paquete opcional."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from rayito._optional import require_module
from rayito.exceptions import InvalidArgumentException

MISSING_MODULE = "rayito_test_definitely_not_installed"
KNOWN_OPTIONAL_PACKAGES = ("opentelemetry", "opentelemetry.trace", "opentelemetry.sdk")
FIXTURES_DIR = Path(__file__).parent / "fixtures"


def test_missing_module_raises_invalid_argument_with_extra_hint() -> None:
    with pytest.raises(InvalidArgumentException) as excinfo:
        require_module(MISSING_MODULE, extra="otel", feature="las trazas OTel")
    message = str(excinfo.value)
    assert "pip install rayito[otel]" in message
    assert "las trazas OTel" in message
    assert MISSING_MODULE in message


def test_missing_module_error_chains_the_original_import_error() -> None:
    with pytest.raises(InvalidArgumentException) as excinfo:
        require_module(MISSING_MODULE, extra="otel", feature="las trazas OTel")
    assert isinstance(excinfo.value.__cause__, ImportError)


def test_existing_module_resolves() -> None:
    module = require_module("json", extra="unused", feature="una función existente")
    assert module is sys.modules["json"]


def test_missing_transitive_dependency_of_an_installed_package_propagates_as_is(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Un paquete instalado cuya propia dependencia interna falta no debe
    confundirse con "falta el extra": el `ModuleNotFoundError` real (por
    `optional_broken_pkg.this_nested_dependency_does_not_exist`, no por
    `optional_broken_pkg`) se propaga tal cual."""
    monkeypatch.syspath_prepend(str(FIXTURES_DIR))
    sys.modules.pop("optional_broken_pkg", None)
    with pytest.raises(ModuleNotFoundError) as excinfo:
        require_module("optional_broken_pkg", extra="unused", feature="una función")
    assert not isinstance(excinfo.value, InvalidArgumentException)
    assert excinfo.value.name == "optional_broken_pkg.this_nested_dependency_does_not_exist"


def _probe(import_statement: str) -> str:
    packages = list(KNOWN_OPTIONAL_PACKAGES)
    check = f"any(name in sys.modules for name in {packages!r})"
    return f"import sys; {import_statement}; print({check})"


def test_importing_rayito_never_imports_an_optional_package() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _probe("import rayito")],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "False"


def test_optional_module_itself_imports_nothing_optional_at_module_level() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _probe("import rayito._optional")],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "False"

"""Sin el extra `cdk` nada cambia (ADR-032 D8): importar `rayito`, la CLI o
el servidor MCP no carga `aws_cdk`, `jsii` ni `constructs`, y `rayito.cdk`
sin el extra falla con la instrucción `pip install rayito[cdk]`."""

from __future__ import annotations

import json
import subprocess
import sys

CDK_MODULES = ("aws_cdk", "jsii", "constructs")
LOADED = (
    "import json, sys; print(json.dumps(sorted({m.split('.')[0] for m in sys.modules} & set(%r))))"
)


def loaded_after(statement: str) -> list[str]:
    result = subprocess.run(
        [sys.executable, "-c", f"{statement}; {LOADED % (CDK_MODULES,)}"],
        capture_output=True,
        text=True,
        check=True,
    )
    loaded: list[str] = json.loads(result.stdout.strip().splitlines()[-1])
    return loaded


def test_import_rayito_loads_no_cdk_module() -> None:
    assert loaded_after("import rayito") == []


def test_the_cli_and_mcp_load_no_cdk_module() -> None:
    assert loaded_after("import rayito.cli.app") == []
    assert loaded_after("import rayito.mcp") == []
    assert loaded_after("import rayito.e2b") == []


def test_the_publish_domain_loads_no_cdk_module() -> None:
    assert loaded_after("import rayito._images_publish_domain, rayito.cli._publish") == []


def test_rayito_cdk_without_the_extra_names_it() -> None:
    blocker = (
        "import sys, importlib.abc\n"
        "class Block(importlib.abc.MetaPathFinder):\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name.split('.')[0] in ('aws_cdk',):\n"
        "            raise ModuleNotFoundError(name=name)\n"
        "sys.meta_path.insert(0, Block())\n"
        "try:\n"
        "    import rayito.cdk\n"
        "except Exception as exc:\n"
        "    print(type(exc).__name__, exc)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", blocker], capture_output=True, text=True, check=True
    )
    assert "InvalidArgumentException" in result.stdout
    assert "pip install rayito[cdk]" in result.stdout

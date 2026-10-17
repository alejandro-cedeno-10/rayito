"""Piezas compartidas de las recetas de instalación (ADR-028, design.md D8):
las rutas de la imagen del agente, el instalador de un venv con requisitos
con hash y la lectura de los datos de paquete (requisitos, demonio de
precarga y host del agente). Cada runtime compone sus `install_steps()` con
estas piezas; `AgentTemplate` sólo las ensambla.

Puro salvo la lectura de datos de paquete. Ningún valor de entorno concreto
vive aquí: los pines (versiones y sha256) llegan de `limits.json`.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from importlib import resources
from importlib.resources.abc import Traversable
from typing import Final

from rayito._agent._runtime import DEFAULT_INSTALL_DIR, InstallTarget

#: Directorio raíz de todo lo que la plantilla instala (root, 0755).
AGENT_INSTALL_DIR: Final = DEFAULT_INSTALL_DIR
AGENT_BIN_DIR: Final = f"{AGENT_INSTALL_DIR}/bin"
#: El código del usuario (`AgentTemplate(app=...)`): de root, sin escritura
#: para `user`.
AGENT_APP_DIR: Final = f"{AGENT_INSTALL_DIR}/app"
#: Lo que Rayito instala para ejecutar agentes (runner y host), de root.
AGENT_RAYITO_DIR: Final = f"{AGENT_INSTALL_DIR}/rayito"
#: El host del agente (`agent_host`) que sirve y ejecuta los runtimes.
AGENT_HOST_DIR: Final = f"{AGENT_RAYITO_DIR}/agent_host"
#: Nombres de las carpetas del contexto de build que `AgentTemplate` copia.
AGENT_HOST_CONTEXT_DIR: Final = "agent_host"
AGENT_APP_CONTEXT_DIR: Final = "app"
#: El puente de Node de los agentes de JavaScript y TypeScript (dato de
#: paquete) y dónde lo instala la plantilla.
NODE_BRIDGE_NAME: Final = "node_bridge.mjs"
NODE_BRIDGE_PATH: Final = f"{AGENT_RAYITO_DIR}/{NODE_BRIDGE_NAME}"
PREFETCH_SCRIPT_NAME: Final = "rayito-agent-prefetch"
PREFETCH_SCRIPT_PATH: Final = f"{AGENT_BIN_DIR}/{PREFETCH_SCRIPT_NAME}"
_ASSETS_PACKAGE: Final = "rayito._agent"
_ASSETS_DIR: Final = "_assets"
_RUNNER_DIR: Final = "_runner"
_HOST_PACKAGE_DIR: Final = "agent_host"
_PYCACHE: Final = "__pycache__"
_HOST_SUFFIX: Final = ".py"


def asset_bytes(name: str) -> bytes:
    """Un fichero de `_agent/_assets/` (datos de paquete)."""
    return resources.files(_ASSETS_PACKAGE).joinpath(_ASSETS_DIR, name).read_bytes()


def runner_bytes(name: str) -> bytes:
    """Un fichero de `_agent/_runner/` (datos de paquete)."""
    return resources.files(_ASSETS_PACKAGE).joinpath(_RUNNER_DIR, name).read_bytes()


def node_bridge() -> bytes:
    """El puente de Node (`node_bridge.mjs`)."""
    return asset_bytes(NODE_BRIDGE_NAME)


def prefetch_script() -> bytes:
    """El demonio de precarga (`rayito-agent-prefetch`)."""
    return asset_bytes(PREFETCH_SCRIPT_NAME)


def agent_host_files() -> dict[str, bytes]:
    """Los ficheros del host del agente (`_runner/agent_host/**.py`), con su
    ruta relativa a esa carpeta; vacío mientras el paquete no lo incluya."""
    root = resources.files(_ASSETS_PACKAGE).joinpath(_RUNNER_DIR, _HOST_PACKAGE_DIR)
    if not root.is_dir():
        return {}
    files: dict[str, bytes] = {}
    _collect_host_files(root, "", files)
    return dict(sorted(files.items()))


def _collect_host_files(node: Traversable, prefix: str, files: dict[str, bytes]) -> None:
    for child in node.iterdir():
        if child.name == _PYCACHE:
            continue
        relative = f"{prefix}{child.name}"
        if child.is_dir():
            _collect_host_files(child, f"{relative}/", files)
        elif child.name.endswith(_HOST_SUFFIX):
            files[relative] = child.read_bytes()


def host_sha256(files: Mapping[str, bytes]) -> str | None:
    """sha256 de `ruta NUL contenido NUL` de cada fichero del host, por
    orden de ruta; `None` si el paquete no incluye el host."""
    if not files:
        return None
    digest = hashlib.sha256()
    for relative, data in sorted(files.items()):
        digest.update(relative.encode("utf-8") + b"\0" + data + b"\0")
    return digest.hexdigest()


def hashed_venv_install(
    target: InstallTarget, *, venv_dir: str, requirements_path: str, requirements_sha256: str
) -> str:
    """El `RUN` que comprueba el sha256 del fichero de requisitos, crea el
    venv, instala con `--require-hashes --no-deps --only-binary=:all:` y
    termina con `pip check`."""
    venv_python = f"{venv_dir}/bin/python"
    return (
        f"echo '{requirements_sha256}  {requirements_path}' | sha256sum -c -"
        f" && {target.python} -m venv {venv_dir}"
        f" && {venv_python} -m pip install --no-cache-dir"
        " --require-hashes --no-deps --only-binary=:all:"
        f" -r {requirements_path}"
        f" && {venv_python} -m pip check"
    )

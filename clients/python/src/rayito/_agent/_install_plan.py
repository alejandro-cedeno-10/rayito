"""El plan de instalación de un agente que ya existe (0.14, D1 y D3): cómo
se llevan a la imagen el intérprete (CPython o Node con sha256 fijado), el
gestor de paquetes (uv, pip, npm, pnpm o yarn) y las dependencias del
proyecto, a partir de lo que se sabe de él (el gestor, el fichero de
requisitos o el lockfile y la versión del intérprete).

Puro: sin E/S, sin reloj, sin red; sólo compone `TemplateStep`s. Los pines
(versiones, sha256 y la etiqueta de la release de python-build-standalone)
llegan de `limits.json`. Los vectores compartidos con TypeScript son
`testdata/agent/install-plans/*.json`: el mismo plan produce los mismos pasos
en los dos SDK.

Reglas:

- Python: sin `python_version`, o con la versión del intérprete de la imagen
  base (`AGENT_DEFAULT_PYTHON_MINOR`), no se descarga nada. Con otra, se
  baja el tarball `install_only` de python-build-standalone (linux arm64)
  de `AGENT_PYTHON_BUILD_VERSIONS` y se comprueba su sha256.
- Node: tarball oficial de nodejs.org (linux arm64) de `AGENT_NODE_BUILD_VERSIONS`
  con su sha256; `pnpm` y `yarn` se instalan con `npm install -g
  --ignore-scripts` a la versión fijada.
- Un lockfile ausente no se inventa: `npm ci`, `pnpm install
  --frozen-lockfile` y `yarn install --frozen-lockfile` fallan si el lockfile
  no coincide con `package.json`, que es lo que se quiere.
"""

from __future__ import annotations

import re
from typing import Final, Literal

from rayito._agent._host_runtime import container_packages_step, user_requirements_steps
from rayito._agent._runtime import InstallTarget, TemplateStep, copy_step, run_step
from rayito._limits import (
    AGENT_DEFAULT_NODE_MAJOR,
    AGENT_DEFAULT_PYTHON_MINOR,
    AGENT_NODE_BUILD_SHA256,
    AGENT_NODE_BUILD_VERSIONS,
    AGENT_PNPM_VERSION,
    AGENT_PYTHON_BUILD_SHA256,
    AGENT_PYTHON_BUILD_TAG,
    AGENT_PYTHON_BUILD_VERSIONS,
    AGENT_UV_SHA256,
    AGENT_UV_VERSION,
    AGENT_YARN_CLASSIC_VERSION,
)
from rayito.exceptions import InvalidArgumentException

PythonInstaller = Literal["pip", "uv"]
NodePackageManager = Literal["npm", "pnpm", "yarn"]
#: Cómo se describen las dependencias de Python de un proyecto: un fichero
#: de requisitos, `pyproject.toml` más `uv.lock`, o `pyproject.toml` solo.
PythonManifest = Literal["requirements", "uv-lock", "pyproject"]
PYTHON_INSTALLERS: Final[tuple[str, ...]] = ("pip", "uv")
NODE_PACKAGE_MANAGERS: Final[tuple[str, ...]] = ("npm", "pnpm", "yarn")
PACKAGE_MANAGERS: Final[tuple[str, ...]] = (*PYTHON_INSTALLERS, *NODE_PACKAGE_MANAGERS)
PYTHON_MANIFESTS: Final[tuple[str, ...]] = ("requirements", "uv-lock", "pyproject")
#: Lockfile que cada gestor de Node lee junto a `package.json`.
NODE_LOCKFILES: Final[dict[str, str]] = {
    "npm": "package-lock.json",
    "pnpm": "pnpm-lock.yaml",
    "yarn": "yarn.lock",
}
NODE_MANIFEST: Final = "package.json"
PYPROJECT_MANIFEST: Final = "pyproject.toml"
UV_LOCKFILE: Final = "uv.lock"
#: Carpetas de lo que instala el plan, bajo `install_dir`.
NODE_DIR_NAME: Final = "node"
UV_DIR_NAME: Final = "uv"
PYTHON_DIR_PREFIX: Final = "python-"
#: El directorio que trae el tarball `install_only` de python-build-standalone.
PYTHON_TARBALL_ROOT: Final = "python"
PYTHON_TARBALL_PATH: Final = "/tmp/pinned-python.tar.gz"
NODE_TARBALL_PATH: Final = "/tmp/node.tar.gz"
UV_TARBALL_PATH: Final = "/tmp/uv.tar.gz"
#: Dónde se descarga cada herramienta. Los tres hosts son los oficiales del
#: proyecto y el sha256 de `limits.json` es lo que se comprueba.
PYTHON_BUILD_URL: Final = "https://github.com/astral-sh/python-build-standalone/releases/download"
NODE_DIST_URL: Final = "https://nodejs.org/dist"
UV_RELEASE_URL: Final = "https://github.com/astral-sh/uv/releases/download"
UV_ARCHIVE: Final = "uv-aarch64-unknown-linux-gnu"
#: Herramientas de descarga que una imagen Debian de contenedor no trae.
DOWNLOAD_PACKAGES: Final[tuple[str, ...]] = ("curl", "ca-certificates")
_REQUIREMENTS_PATH_PATTERN: Final = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]*")
#: `--hash=` en un fichero de requisitos activa `--require-hashes`.
UV_PIP_HASH_FLAG: Final = "--require-hashes"


def python_manifest(requirements: str) -> str:
    """`uv-lock`, `pyproject` o `requirements` según el nombre del fichero de
    dependencias de `source`."""
    if requirements == UV_LOCKFILE:
        return "uv-lock"
    return "pyproject" if requirements == PYPROJECT_MANIFEST else "requirements"


def check_requirements_path(label: str, requirements: str, package_manager: str | None) -> None:
    """`requirements` es una ruta relativa dentro de `source`, y
    `pyproject.toml` o `uv.lock` sólo con `uv`; si no,
    `InvalidArgumentException` con el nombre de la opción (`label`)."""
    if not _REQUIREMENTS_PATH_PATTERN.fullmatch(requirements) or ".." in requirements.split("/"):
        raise InvalidArgumentException(
            f"{label}.requirements es una ruta relativa dentro de source"
        )
    if python_manifest(requirements) != "requirements" and package_manager != "uv":
        raise InvalidArgumentException(
            f"{label}.requirements={requirements!r} necesita package_manager='uv'"
        )


def ecosystem(package_manager: str) -> Literal["python", "node"]:
    """El ecosistema del gestor: `python` (pip, uv) o `node` (npm, pnpm,
    yarn); cualquier otro nombre es `InvalidArgumentException`."""
    if package_manager in PYTHON_INSTALLERS:
        return "python"
    if package_manager in NODE_PACKAGE_MANAGERS:
        return "node"
    raise InvalidArgumentException(
        f"package_manager debe ser uno de {list(PACKAGE_MANAGERS)}, recibido {package_manager!r}"
    )


def check_python_version(version: str) -> str:
    """`version` (la menor de CPython, `3.12`) si hay un build fijado."""
    if version not in AGENT_PYTHON_BUILD_VERSIONS:
        raise InvalidArgumentException(
            f"python_version debe ser una de {sorted(AGENT_PYTHON_BUILD_VERSIONS)}, "
            f"recibida {version!r}"
        )
    return version


def check_node_version(version: str) -> str:
    """`version` (la mayor de Node, `22`) si hay un build fijado."""
    if version not in AGENT_NODE_BUILD_VERSIONS:
        raise InvalidArgumentException(
            f"node_version debe ser una de {sorted(AGENT_NODE_BUILD_VERSIONS)}, "
            f"recibida {version!r}"
        )
    return version


def python_download_command(minor: str, parent_dir: str, tarball_path: str) -> str:
    """El `RUN` que baja el CPython fijado de la versión menor `minor` a
    `parent_dir/python`, comprobando su sha256."""
    check_python_version(minor)
    version = AGENT_PYTHON_BUILD_VERSIONS[minor]
    asset = f"cpython-{version}%2B{AGENT_PYTHON_BUILD_TAG}-aarch64-unknown-linux-gnu-install_only"
    url = f"{PYTHON_BUILD_URL}/{AGENT_PYTHON_BUILD_TAG}/{asset}.tar.gz"
    return (
        f"mkdir -p {parent_dir}"
        f" && curl -fsSL --retry 3 -o {tarball_path} {url}"
        f" && echo '{AGENT_PYTHON_BUILD_SHA256[minor]}  {tarball_path}' | sha256sum -c -"
        f" && tar -xzf {tarball_path} -C {parent_dir}"
        f" && rm -f {tarball_path}"
    )


def python_interpreter(target: InstallTarget, python_version: str | None) -> str:
    """El intérprete con el que se crean los venvs: el de `target` si no se
    pide otra versión menor, o el CPython fijado bajo `install_dir`."""
    if python_version is None or python_version == AGENT_DEFAULT_PYTHON_MINOR:
        return target.python
    check_python_version(python_version)
    parent = f"{target.install_dir}/{PYTHON_DIR_PREFIX}{python_version}"
    return f"{parent}/{PYTHON_TARBALL_ROOT}/bin/python3"


def python_runtime_steps(target: InstallTarget, python_version: str | None) -> list[TemplateStep]:
    """Descarga el CPython fijado de `python_version`; ninguno paso si es la
    versión del intérprete de la imagen base."""
    if python_version is None or python_version == AGENT_DEFAULT_PYTHON_MINOR:
        return []
    parent = f"{target.install_dir}/{PYTHON_DIR_PREFIX}{python_version}"
    steps = list(container_packages_step(target, DOWNLOAD_PACKAGES))
    steps.append(run_step(python_download_command(python_version, parent, PYTHON_TARBALL_PATH)))
    return steps


def uv_binary(target: InstallTarget) -> str:
    return f"{target.install_dir}/{UV_DIR_NAME}/uv"


def uv_steps(target: InstallTarget) -> list[TemplateStep]:
    """Instala `uv` (versión y sha256 fijados) en `install_dir/uv/uv`."""
    directory = f"{target.install_dir}/{UV_DIR_NAME}"
    url = f"{UV_RELEASE_URL}/{AGENT_UV_VERSION}/{UV_ARCHIVE}.tar.gz"
    steps = list(container_packages_step(target, DOWNLOAD_PACKAGES))
    steps.append(
        run_step(
            f"curl -fsSL --retry 3 -o {UV_TARBALL_PATH} {url}"
            f" && echo '{AGENT_UV_SHA256}  {UV_TARBALL_PATH}' | sha256sum -c -"
            f" && mkdir -p {directory}"
            f" && tar -xzf {UV_TARBALL_PATH} -C {directory} --strip-components=1"
            f" && rm -f {UV_TARBALL_PATH}"
        )
    )
    return steps


def python_dependency_steps(
    target: InstallTarget,
    *,
    installer: str,
    manifest: str,
    path: str | None,
    python: str,
    venv_dir: str,
    slug: str,
    create_venv: bool = True,
) -> list[TemplateStep]:
    """Crea `venv_dir` con `python` e instala las dependencias del proyecto.
    Con `create_venv=False` el venv ya existe (el de un runtime con sus
    pines, como el de deepagents o el de Strands) y `python` es el suyo: las
    dependencias se añaden sin quitar lo que ya hay.

    `manifest` es `requirements` (`path` es el fichero dentro de `source`),
    `uv-lock` (`pyproject.toml` y `uv.lock` en la raíz de `source`) o
    `pyproject` (sólo `pyproject.toml`). `pip` sólo instala `requirements`;
    `uv` instala los tres (`uv sync --frozen` con el lockfile, `uv pip
    install -r` con los otros dos)."""
    _check_python_inputs(installer, manifest, path)
    if installer == "pip":
        creation = [run_step(f"{python} -m venv {venv_dir}")] if create_venv else []
        return [
            *creation,
            *user_requirements_steps(target, venv_dir=venv_dir, requirements=str(path), slug=slug),
        ]
    uv = uv_binary(target)
    venv_python = f"{venv_dir}/bin/python"
    steps = uv_steps(target)
    if create_venv:
        steps.append(run_step(f"{uv} venv --python {python} {venv_dir}"))
    inexact = "" if create_venv else " --inexact"
    if manifest == "uv-lock":
        source = f"{target.install_dir}/{slug}-project"
        steps += [
            copy_step(f"app/{PYPROJECT_MANIFEST}", f"{source}/{PYPROJECT_MANIFEST}"),
            copy_step(f"app/{UV_LOCKFILE}", f"{source}/{UV_LOCKFILE}"),
            run_step(
                f"cd {source} && UV_PROJECT_ENVIRONMENT={venv_dir} UV_PYTHON_DOWNLOADS=never"
                f" {uv} sync --frozen --no-dev --no-install-project{inexact} --python {python}"
            ),
        ]
        return steps
    if manifest == "requirements":
        requirements = str(path)
        destination = f"{target.install_dir}/{slug}-{requirements.rsplit('/', 1)[-1]}"
    else:
        requirements = PYPROJECT_MANIFEST
        destination = f"{target.install_dir}/{slug}-project/{PYPROJECT_MANIFEST}"
    steps += [
        copy_step(f"app/{requirements}", destination),
        run_step(
            f"hashes=''; if grep -q -- '--hash=' {destination};"
            f" then hashes='{UV_PIP_HASH_FLAG}'; fi"
            f" && {uv} pip install --python {venv_python} --no-cache $hashes -r {destination}"
            f" && {uv} pip check --python {venv_python}"
        ),
    ]
    return steps


def _check_python_inputs(installer: str, manifest: str, path: str | None) -> None:
    if installer not in PYTHON_INSTALLERS:
        raise InvalidArgumentException(
            f"installer debe ser uno de {list(PYTHON_INSTALLERS)}, recibido {installer!r}"
        )
    if manifest not in PYTHON_MANIFESTS:
        raise InvalidArgumentException(
            f"manifest debe ser uno de {list(PYTHON_MANIFESTS)}, recibido {manifest!r}"
        )
    if installer == "pip" and manifest != "requirements":
        raise InvalidArgumentException(
            "pip sólo instala un fichero de requisitos: para pyproject.toml o uv.lock usa uv"
        )
    if manifest == "requirements" and not path:
        raise InvalidArgumentException("requirements necesita la ruta del fichero de requisitos")


def node_directory(target: InstallTarget) -> str:
    return f"{target.install_dir}/{NODE_DIR_NAME}"


def node_bin(target: InstallTarget) -> str:
    return f"{node_directory(target)}/bin"


def node_runtime_steps(target: InstallTarget, node_version: str | None) -> list[TemplateStep]:
    """Node (tarball oficial linux arm64, sha256 fijado) en
    `install_dir/node`, con `node` enlazado en `/usr/local/bin`."""
    major = check_node_version(node_version or AGENT_DEFAULT_NODE_MAJOR)
    version = AGENT_NODE_BUILD_VERSIONS[major]
    archive = f"node-v{version}-linux-arm64.tar.gz"
    directory = node_directory(target)
    steps = list(container_packages_step(target, DOWNLOAD_PACKAGES))
    steps.append(
        run_step(
            f"curl -fsSL --retry 3 -o {NODE_TARBALL_PATH} {NODE_DIST_URL}/v{version}/{archive}"
            f" && echo '{AGENT_NODE_BUILD_SHA256[major]}  {NODE_TARBALL_PATH}' | sha256sum -c -"
            f" && mkdir -p {directory}"
            f" && tar -xzf {NODE_TARBALL_PATH} -C {directory} --strip-components=1"
            f" && rm -f {NODE_TARBALL_PATH}"
            f" && ln -sf {node_bin(target)}/node /usr/local/bin/node"
        )
    )
    return steps


def node_package_files(manager: str) -> tuple[str, str]:
    """`package.json` y el lockfile del gestor, que el build copia de
    `source`."""
    if ecosystem(manager) != "node":
        raise InvalidArgumentException(f"{manager!r} no es un gestor de paquetes de Node")
    return NODE_MANIFEST, NODE_LOCKFILES[manager]


_NODE_INSTALL_COMMANDS: Final[dict[str, str]] = {
    "npm": "npm ci --ignore-scripts --omit=dev --no-audit --no-fund",
    "pnpm": "pnpm install --frozen-lockfile --prod --ignore-scripts",
    "yarn": "yarn install --frozen-lockfile --production --ignore-scripts",
}
_NODE_MANAGER_VERSIONS: Final[dict[str, str]] = {
    "pnpm": AGENT_PNPM_VERSION,
    "yarn": AGENT_YARN_CLASSIC_VERSION,
}


def node_dependency_steps(target: InstallTarget, manager: str) -> list[TemplateStep]:
    """Copia `package.json` y el lockfile a `install_dir` (de modo que
    `node_modules` quede por encima del código copiado y Node lo resuelva) y
    ejecuta la instalación congelada del gestor."""
    files = node_package_files(manager)
    steps = [copy_step(f"app/{name}", f"{target.install_dir}/{name}") for name in files]
    path = f"PATH={node_bin(target)}:$PATH"
    version = _NODE_MANAGER_VERSIONS.get(manager)
    prepare = (
        ""
        if version is None
        else f"{path} npm install -g --ignore-scripts --no-audit --no-fund {manager}@{version} && "
    )
    steps.append(
        run_step(f"{prepare}cd {target.install_dir} && {path} {_NODE_INSTALL_COMMANDS[manager]}")
    )
    return steps


__all__ = [
    "NODE_LOCKFILES",
    "NODE_PACKAGE_MANAGERS",
    "PACKAGE_MANAGERS",
    "PYTHON_INSTALLERS",
    "PYTHON_MANIFESTS",
    "check_node_version",
    "check_python_version",
    "check_requirements_path",
    "ecosystem",
    "node_bin",
    "node_dependency_steps",
    "node_package_files",
    "node_runtime_steps",
    "python_dependency_steps",
    "python_download_command",
    "python_interpreter",
    "python_manifest",
    "python_runtime_steps",
    "uv_steps",
]

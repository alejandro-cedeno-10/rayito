"""Compara la superficie pública del SDK Python con la del SDK TypeScript.

Cuatro listas se leen sin importar ninguno de los dos paquetes (sólo la
biblioteca estándar, sin red, sin `node` ni `uv`):

- `__all__` de `rayito/__init__.py` y de `rayito/e2b/__init__.py` (con `ast`);
- los `export` de `src/index.ts` y de `src/e2b/index.ts` (con una lectura
  léxica: bloques `export { ... }`, `export type { ... }` y declaraciones
  `export const|function|class|interface|type|enum`).

Los nombres se comparan normalizados: sin guiones bajos, en minúsculas y con
`XException` leído como `XError` (`parse_chart` es `parseChart`;
`SandboxException` es `SandboxError`).

Dos direcciones, con reglas distintas:

- **Python sin TypeScript**: cualquier nombre de `__all__` que TypeScript no
  exporte (como valor o como tipo) es un hallazgo, salvo los `Async*` (TypeScript
  es sólo asíncrono) y los de `PYTHON_ONLY`.
- **TypeScript sin Python**: sólo se miran los exports de *valor* (clases,
  funciones, constantes). Los tipos (`*Options`, callbacks, uniones) son el
  modo idiomático de TypeScript de describir argumentos y no tienen contraparte
  en `__all__`; un valor sin contraparte es un hallazgo salvo los de
  `TYPESCRIPT_ONLY_VALUES`.

Las diferencias intencionales viven en `PYTHON_ONLY` y `TYPESCRIPT_ONLY_VALUES`,
cada una con su motivo. Una entrada que ya no hace falta (el nombre desapareció
o ahora existe en los dos lados) también es un hallazgo, para que la lista no
se pudra. Antes de añadir una entrada, comprueba que la diferencia sea de
verdad intencional (`docs/site/docs/e2b-compat.md`, `referencia/typescript.md`);
si es un hueco, arréglalo.

Uso::

    python3 scripts/check_sdk_parity.py [--root DIR]
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path
from typing import Final

PYTHON_SOURCE: Final = Path("clients/python/src/rayito")
TYPESCRIPT_SOURCE: Final = Path("clients/typescript/src")

#: Cada par es una superficie pública: `(nombre, módulo Python, módulo TypeScript)`.
SURFACES: Final = (
    ("rayito", PYTHON_SOURCE / "__init__.py", TYPESCRIPT_SOURCE / "index.ts"),
    (
        "rayito.e2b",
        PYTHON_SOURCE / "e2b" / "__init__.py",
        TYPESCRIPT_SOURCE / "e2b" / "index.ts",
    ),
)

ASYNC_PREFIX: Final = "Async"
EXCEPTION_SUFFIX: Final = "Exception"
ERROR_SUFFIX: Final = "Error"

#: Nombres de `__all__` sin contraparte en TypeScript, por superficie.
#: Una entrada por diferencia intencional, con su motivo. Las 19 clases
#: `Async*` no se listan: TypeScript es sólo asíncrono (ARCHITECTURE.md) y la
#: regla `ASYNC_PREFIX` las cubre, comprobando que existe la clase síncrona.
PYTHON_ONLY: Final[dict[str, dict[str, str]]] = {
    "rayito": {
        "ClientSettings": "en TypeScript son las opciones de `create()` y de cada clase",
        "NetworkOptions": "en TypeScript es el tipo `NetworkPolicyInput` de la opción `network`",
        "NetworkPolicy": "en TypeScript es el tipo `NetworkPolicyInput` de la opción `network`",
        "EgressProxy": "en TypeScript es el tipo `EgressProxyInput` de la opción `egressProxy`",
        "RayitoCompatWarning": "en TypeScript es `process.emitWarning` con `COMPAT_WARNING_TYPE`",
    },
    "rayito.e2b": {
        "RayitoCompatWarning": "en TypeScript es `process.emitWarning` con `COMPAT_WARNING_TYPE`",
        "VolumeException": "los errores de volumen del shim son una asimetría documentada "
        "en `e2b-compat.md`",
        "VolumeNotFoundException": "ídem: errores de volumen documentados en `e2b-compat.md`",
        "VolumePathNotFoundException": "ídem: errores de volumen documentados en `e2b-compat.md`",
        "Chart2D": "clase base de Python; en TypeScript la unión `Chart` ya cubre los gráficos 2D",
        "DownloadLink": "alias de tipo de Python; en TypeScript el enlace es un `string`",
        "ListedSandbox": "alias de tipo de Python; TypeScript usa `SandboxInfo`",
        "MIMEType": "alias de tipo de Python; en TypeScript es `string`",
        "OutputHandler": "alias de tipo de Python; en TypeScript son los `*Callback`",
        "PtyOutput": "alias de tipo de Python; en TypeScript es `PtyOutputCallback`",
        "RunCodeLanguage": "alias de tipo de Python; en TypeScript es `string`",
        "SandboxQuery": "en TypeScript son los campos de `SandboxListOpts`",
        "Stderr": "alias de tipo de Python; en TypeScript es `string`",
        "Stdout": "alias de tipo de Python; en TypeScript es `string`",
        "UploadTicket": (
            "el shim de TypeScript no re-exporta `UploadTicket` (existe en "
            "`rayito`); `files.uploadUrl` del shim devuelve la URL"
        ),
    },
}

#: Exports de valor de TypeScript sin contraparte en `__all__`, por superficie.
TYPESCRIPT_ONLY_VALUES: Final[dict[str, dict[str, str]]] = {
    "rayito": {
        "Agent": "en Python el agente se usa a través de `Sandbox.agent` y `AgentTemplate`",
        "AGENT_TEMPLATE_RUNTIMES": "constante pública de TypeScript; en Python es privada",
        "DEFAULT_AGENT_TEMPLATE_BASE": "constante pública de TypeScript; en Python es privada",
        "DEFAULT_AGENT_TEMPLATE_NAME": "constante pública de TypeScript; en Python es privada",
        "DEFAULT_INTERVAL_S": "constante pública de TypeScript; en Python es privada",
        "DEFAULT_PERSIST_TIMEOUT_MS": "constante pública de TypeScript; en Python es privada",
        "DEFAULT_SERVICE_NAME": "constante pública de TypeScript; en Python es privada",
        "MAX_INTERVAL_S": "constante pública de TypeScript; en Python es privada",
        "MIN_INTERVAL_S": "constante pública de TypeScript; en Python es privada",
        "CodeClient": "cliente de `runCode`, público en TypeScript; en Python es privado",
        "LambdaMicrovmsControlPlane": "adaptador público en TypeScript; en Python es privado",
        "PortSpec": "clase pública en TypeScript; en Python es un tipo privado",
        "Commands": "clase de `sbx.commands`, pública en TypeScript; en Python es privada",
        "Filesystem": "clase de `sbx.files`, pública en TypeScript; en Python es privada",
        "Pty": "clase de `sbx.pty`, pública en TypeScript; en Python es privada",
        "defaultIdlePolicy": "ayudante público en TypeScript; en Python es privado",
        "parseChart": "ayudante público en TypeScript; en Python es `rayito._charts`",
        "validatePoolConfig": "ayudante público en TypeScript; en Python es privado",
    },
    "rayito.e2b": {
        "DEFAULT_WATCH_TIMEOUT_MS": "constante de TypeScript; en Python es privada",
        "Filesystem": "clase de `sbx.files` exportada por el shim de TypeScript",
        "Pty": "clase de `sbx.pty` exportada por el shim de TypeScript",
        "bindVolume": "ayudante del shim de TypeScript; en Python es un método de `Volume`",
    },
}

BLOCK_COMMENT: Final = re.compile(r"/\*.*?\*/", re.DOTALL)
LINE_COMMENT: Final = re.compile(r"//[^\n]*")
EXPORT_BLOCK: Final = re.compile(r"export\s+(type\s+)?\{([^}]*)\}")
EXPORT_DECLARATION: Final = re.compile(
    r"^export\s+(?:declare\s+)?(?:async\s+)?"
    r"(const|let|function|class|interface|type|enum)\s+(\w+)",
    re.MULTILINE,
)
TYPE_ONLY_DECLARATIONS: Final = frozenset({"interface", "type"})
TYPE_MARKER: Final = re.compile(r"^type\s+")
ALIAS_SEPARATOR: Final = " as "


def python_exports(path: Path) -> set[str]:
    """Los nombres de `__all__`, que debe ser una lista o tupla literal."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if any(
            isinstance(target, ast.Name) and target.id == "__all__"
            for target in node.targets
        ):
            return {
                ast.literal_eval(element)
                for element in ast.iter_child_nodes(node.value)
                if isinstance(element, ast.Constant)
            }
    raise SystemExit(f"{path}: no encuentro un `__all__` literal")


def strip_comments(source: str) -> str:
    return LINE_COMMENT.sub("", BLOCK_COMMENT.sub("", source))


def exported_name(specifier: str) -> tuple[str, bool]:
    """`"type Foo as Bar"` -> `("Bar", True)`: el nombre que ve el consumidor
    y si es sólo un tipo."""
    is_type = TYPE_MARKER.match(specifier) is not None
    name = TYPE_MARKER.sub("", specifier).split(ALIAS_SEPARATOR)[-1].strip()
    return name, is_type


def typescript_exports(path: Path) -> dict[str, bool]:
    """Nombre exportado -> `True` si es sólo un tipo (no existe en tiempo de
    ejecución). `export default` no cuenta."""
    return typescript_exports_from_source(path.read_text(encoding="utf-8"))


def typescript_exports_from_source(text: str) -> dict[str, bool]:
    """Como `typescript_exports`, sobre el texto de un módulo."""
    source = strip_comments(text)
    exports: dict[str, bool] = {}
    for block in EXPORT_BLOCK.finditer(source):
        block_is_type = block.group(1) is not None
        for raw in block.group(2).split(","):
            specifier = raw.strip()
            if not specifier:
                continue
            name, is_type = exported_name(specifier)
            exports[name] = block_is_type or is_type
    for declaration in EXPORT_DECLARATION.finditer(source):
        exports[declaration.group(2)] = declaration.group(1) in TYPE_ONLY_DECLARATIONS
    return exports


def normalise(name: str) -> str:
    if name.endswith(EXCEPTION_SUFFIX):
        name = name.removesuffix(EXCEPTION_SUFFIX) + ERROR_SUFFIX
    return name.replace("_", "").lower()


def stale_entries(
    surface: str,
    table: dict[str, dict[str, str]],
    present: set[str],
    counterpart: set[str],
) -> list[str]:
    """Entradas de la lista de permitidos que ya no se justifican."""
    problems = []
    for name in table.get(surface, {}):
        if name not in present:
            problems.append(f"{surface}: `{name}` está permitido pero ya no existe")
        elif normalise(name) in {normalise(other) for other in counterpart}:
            problems.append(
                f"{surface}: `{name}` está permitido pero ya existe en el otro SDK"
            )
    return problems


def async_without_twin(python: set[str]) -> list[str]:
    return sorted(
        name
        for name in python
        if name.startswith(ASYNC_PREFIX)
        and name.removeprefix(ASYNC_PREFIX) not in python
    )


def surface_problems(
    surface: str, python: set[str], typescript: dict[str, bool]
) -> list[str]:
    allowed_python = PYTHON_ONLY.get(surface, {})
    allowed_typescript = TYPESCRIPT_ONLY_VALUES.get(surface, {})
    typescript_names = {normalise(name) for name in typescript}
    python_names = {normalise(name) for name in python}
    problems = [
        f"{surface}: `{name}` está en `__all__` y TypeScript no lo exporta"
        for name in sorted(python)
        if normalise(name) not in typescript_names
        and name not in allowed_python
        and not name.startswith(ASYNC_PREFIX)
    ]
    problems += [
        f"{surface}: `{name}` es un export de valor de TypeScript sin contraparte en `__all__`"
        for name, is_type in sorted(typescript.items())
        if not is_type
        and normalise(name) not in python_names
        and name not in allowed_typescript
    ]
    problems += [
        f"{surface}: `{name}` empieza por Async pero no existe `{name.removeprefix(ASYNC_PREFIX)}`"
        for name in async_without_twin(python)
    ]
    problems += stale_entries(surface, PYTHON_ONLY, python, set(typescript))
    problems += stale_entries(
        surface,
        TYPESCRIPT_ONLY_VALUES,
        {n for n, t in typescript.items() if not t},
        python,
    )
    return problems


def problems_for(root: Path) -> list[str]:
    problems: list[str] = []
    for surface, python_path, typescript_path in SURFACES:
        problems += surface_problems(
            surface,
            python_exports(root / python_path),
            typescript_exports(root / typescript_path),
        )
    return problems


def parse_root(argv: list[str]) -> Path | None:
    if not argv:
        return Path(__file__).resolve().parent.parent
    if len(argv) == 2 and argv[0] == "--root":
        return Path(argv[1])
    return None


def main(argv: list[str]) -> int:
    root = parse_root(argv)
    if root is None:
        print("uso: check_sdk_parity.py [--root DIR]", file=sys.stderr)
        return 2
    problems = problems_for(root)
    if problems:
        for problem in problems:
            print(f"KO {problem}")
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

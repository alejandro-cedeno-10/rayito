"""Comprueba que todo lo que la automatización de este repo ejecuta está clavado.

Dos puertas, las dos en lista blanca (fallan salvo que la línea demuestre estar
clavada), sin red y sólo con la biblioteca estándar:

1. **Acciones**: cada `uses:` de `.github/workflows/` tiene que nombrar un SHA
   de 40 hex, con un comentario opcional con la etiqueta de la que salió
   (`owner/repo@<sha> # vX.Y.Z`). Una etiqueta, un alias de mayor, una rama o
   un SHA truncado son hallazgos; sólo se saltan las líneas comentadas y las
   acciones locales (`./...`).
2. **uvx**: cada invocación de `uvx` en esos workflows y en el `Makefile` tiene
   que nombrar su herramienta con `==` (`uvx twine==7.0.0 check ...`, también
   en la forma `uvx --from paquete==1.2.3 orden`), porque resuelven y ejecutan
   código de terceros dentro de trabajos que llevan credenciales de publicación.

Las recetas de instalación para usuarios de `docs/site/` quedan fuera: instalan
Rayito publicado, no una herramienta de esta construcción.

    python scripts/check_pins.py              # los caminos por defecto
    python scripts/check_pins.py FICHERO...   # otros caminos (tests)
"""

from __future__ import annotations

import re
import shlex
import sys
from pathlib import Path

PINNED_ACTION = re.compile(r"[^@\s]+@[0-9a-f]{40}( +#.*)?")
USES_KEY = re.compile(r"^\s*(?:-\s+)?uses:\s*(?P<reference>\S.*?)\s*$")
UVX = "uvx"
LOCAL_ACTION_PREFIX = "./"
VERSION_PIN = "=="
FROM_FLAG = "--from"
WORKFLOW_SUFFIXES = frozenset({".yml", ".yaml"})
DEFAULT_PATHS = (
    ".github/workflows/*.yml",
    ".github/workflows/*.yaml",
    "Makefile",
)
ACTION_REASON = "la acción no está clavada a un SHA de 40 hex"
UVX_REASON = "la herramienta de uvx no lleva ==<versión>"

Finding = tuple[int, str, str]


def is_comment(line: str) -> bool:
    return line.lstrip().startswith("#")


def action_reference(line: str) -> str | None:
    """La referencia de una línea cuya clave YAML es `uses:`, o `None`. La
    clave tiene que abrir la línea: `uses:` citado dentro del nombre de un
    paso o de una orden de shell no declara ninguna acción."""
    if is_comment(line):
        return None
    match = USES_KEY.match(line)
    return match.group("reference") if match else None


def unpinned_actions(text: str) -> list[Finding]:
    findings: list[Finding] = []
    for number, line in enumerate(text.splitlines(), start=1):
        reference = action_reference(line)
        if reference is None or reference.startswith(LOCAL_ACTION_PREFIX):
            continue
        if not PINNED_ACTION.fullmatch(reference):
            findings.append((number, line.strip(), ACTION_REASON))
    return findings


def split_words(fragment: str) -> list[str]:
    try:
        return shlex.split(fragment, posix=True)
    except ValueError:
        return fragment.split()


def tool_specification(arguments: list[str]) -> str | None:
    """La herramienta que `uvx` va a ejecutar: la de `--from` si está, si no la
    primera palabra que no sea una opción."""
    if FROM_FLAG in arguments:
        position = arguments.index(FROM_FLAG) + 1
        return arguments[position] if position < len(arguments) else None
    for argument in arguments:
        if not argument.startswith("-"):
            return argument
    return None


def uvx_invocations(line: str) -> list[list[str]]:
    if is_comment(line):
        return []
    words = split_words(line)
    return [words[index + 1 :] for index, word in enumerate(words) if word == UVX]


def unpinned_uvx(text: str) -> list[Finding]:
    findings: list[Finding] = []
    for number, line in enumerate(text.splitlines(), start=1):
        for arguments in uvx_invocations(line):
            specification = tool_specification(arguments)
            if specification is None or VERSION_PIN not in specification:
                findings.append((number, line.strip(), UVX_REASON))
    return findings


def findings_for(path: Path) -> list[Finding]:
    text = path.read_text(encoding="utf-8")
    findings = set(unpinned_uvx(text))
    if path.suffix in WORKFLOW_SUFFIXES:
        findings.update(unpinned_actions(text))
    return sorted(findings)


def resolve_paths(argv: list[str], root: Path) -> list[Path]:
    if argv:
        return [Path(argument) for argument in argv]
    paths: list[Path] = []
    for pattern in DEFAULT_PATHS:
        paths.extend(sorted(root.glob(pattern)))
    return paths


def displayed(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def main(argv: list[str], root: Path | None = None) -> int:
    base = root or Path.cwd()
    paths = [path for path in resolve_paths(argv, base) if path.is_file()]
    total = 0
    for path in paths:
        for number, line, reason in findings_for(path):
            total += 1
            print(f"KO {displayed(path, base)}:{number}: {reason}\n    {line}")
    if total:
        print(f"KO {total} invocación(es) sin clavar")
        return 1
    print(f"OK {len(paths)} fichero(s): toda acción y todo uvx clavados")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

"""Configuración cero de OpenCode (ADR-034, design.md D10): lo que un
proyecto que sólo trae `opencode.json` (u `opencode.jsonc`) y `AGENTS.md`
aporta a su tabla `[agent]` cuando el fichero de proyecto no la escribe.

- `AGENTS.md` de la raíz pasa a ser `agent.instructions_file`.
- El bloque `permission` de la configuración pasa a `agent.permissions`
  (`_opencode.permissions_from_opencode`); lo que no se aplica tal cual se
  avisa con `opencode_permission_ignored`.

Las reglas son datos (`zeroConfig` de `agent-detection.json`). El informe
sólo guarda la evidencia (`file:AGENTS.md`, `config:permission`), nunca el
texto de los ficheros. Espejo de `agent/detect-opencode.ts`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from rayito._agent._detect_report import DetectionEvidence
from rayito._agent._detect_scan import ProjectIndex, WarningSink
from rayito._agent._opencode import permissions_from_opencode

#: La clave de `[agent]` con el fichero de instrucciones y la de los permisos.
INSTRUCTIONS_FILE_KEY: Final = "instructions_file"
PERMISSIONS_KEY: Final = "permissions"
#: El aviso de una entrada de `permission` que no se aplica tal cual.
PERMISSION_WARNING: Final = "opencode_permission_ignored"
#: Una clave de `permission` sólo se nombra en el aviso si tiene esta forma;
#: si no, se nombra con `UNNAMED_PERMISSION_KEY`, así que el aviso nunca
#: copia texto arbitrario del fichero.
_SHOWN_KEY_PATTERN: Final = re.compile(r"[A-Za-z0-9_*.-]{1,64}")
UNNAMED_PERMISSION_KEY: Final = "<clave>"
_STRING_QUOTE: Final = '"'
_ESCAPE: Final = "\\"
_LINE_COMMENT: Final = "//"
_BLOCK_COMMENT_OPEN: Final = "/*"
_BLOCK_COMMENT_CLOSE: Final = "*/"
_CLOSERS: Final = ("}", "]")


@dataclass(frozen=True)
class ZeroConfig:
    """La tabla `[agent]` que aporta la configuración del framework (vacía
    si no aporta nada) y la evidencia de cada clave."""

    agent: Mapping[str, object] = field(default_factory=dict)
    evidence: tuple[DetectionEvidence, ...] = ()


def strip_jsonc(text: str) -> str:
    """`text` sin comentarios (`//` y `/* */`) ni comas finales fuera de las
    cadenas: JSON con comentarios, como lo lee OpenCode. Mismo resultado que
    `stripJsonc` en TypeScript."""
    return _without_trailing_commas(_without_comments(text))


def _without_comments(text: str) -> str:
    out: list[str] = []
    index, in_string = 0, False
    while index < len(text):
        char = text[index]
        if in_string:
            if char == _ESCAPE:
                out.append(text[index : index + 2])
                index += 2
                continue
            in_string = char != _STRING_QUOTE
        elif char == _STRING_QUOTE:
            in_string = True
        elif text.startswith(_LINE_COMMENT, index):
            end = text.find("\n", index)
            index = len(text) if end < 0 else end
            continue
        elif text.startswith(_BLOCK_COMMENT_OPEN, index):
            end = text.find(_BLOCK_COMMENT_CLOSE, index + len(_BLOCK_COMMENT_OPEN))
            index = len(text) if end < 0 else end + len(_BLOCK_COMMENT_CLOSE)
            out.append(" ")
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _without_trailing_commas(text: str) -> str:
    out: list[str] = []
    index, in_string = 0, False
    while index < len(text):
        char = text[index]
        if in_string:
            if char == _ESCAPE:
                out.append(text[index : index + 2])
                index += 2
                continue
            in_string = char != _STRING_QUOTE
        elif char == _STRING_QUOTE:
            in_string = True
        elif char == "," and text[index + 1 :].lstrip().startswith(_CLOSERS):
            index += 1
            continue
        out.append(char)
        index += 1
    return "".join(out)


def parse_jsonc(text: str) -> object | None:
    """El documento de un `opencode.json(c)`, o `None` si no se entiende."""
    try:
        parsed: object = json.loads(strip_jsonc(text))
    except ValueError:
        return None
    return parsed


def zero_config(
    index: ProjectIndex, rules: Mapping[str, Any], framework: str, sink: WarningSink
) -> ZeroConfig:
    """Lo que la configuración de `framework` aporta a `[agent]`, si sus
    reglas (`zeroConfig`) lo describen; nada para el resto."""
    spec = rules["zeroConfig"].get(framework)
    if spec is None:
        return ZeroConfig()
    agent: dict[str, object] = {}
    evidence: list[DetectionEvidence] = []
    name = spec["instructionsFile"]
    text = index.read(name) if index.has_root_file(name) else None
    if text is not None and text.strip():
        agent[INSTRUCTIONS_FILE_KEY] = name
        evidence.append(DetectionEvidence(name, f"file:{name}"))
    permissions = _permissions(index, spec, sink)
    if permissions is not None:
        file, table = permissions
        agent[PERMISSIONS_KEY] = table
        evidence.append(DetectionEvidence(file, f"config:{spec['permissionKey']}"))
    return ZeroConfig(agent, tuple(evidence))


def _permissions(
    index: ProjectIndex, spec: Mapping[str, Any], sink: WarningSink
) -> tuple[str, Mapping[str, object]] | None:
    """El fichero de configuración (el primero de `configFiles` que hay en
    la raíz) y su tabla de permisos, o `None` si no aporta ninguna."""
    file = next((name for name in spec["configFiles"] if index.has_root_file(name)), None)
    text = None if file is None else index.read(file)
    if file is None or text is None:
        return None
    document = parse_jsonc(text)
    if not isinstance(document, Mapping):
        sink.add("file_unreadable", file=file)
        return None
    block = document.get(spec["permissionKey"])
    if block is None:
        return None
    found = permissions_from_opencode(block, ask_action=spec["askAction"])
    for key, reason in found.notes:
        shown = key if _SHOWN_KEY_PATTERN.fullmatch(key) else UNNAMED_PERMISSION_KEY
        sink.add(PERMISSION_WARNING, file=file, key=shown, reason=spec["reasons"][reason])
    return (file, found.table) if found.table else None


__all__ = ["ZeroConfig", "parse_jsonc", "strip_jsonc", "zero_config"]

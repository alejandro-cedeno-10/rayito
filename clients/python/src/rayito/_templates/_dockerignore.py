"""`.dockerignore` con la semántica de Docker (dominio puro, sin E/S).

Docker (`moby/patternmatcher`, el `.dockerignore` de `docker build`) y esta
implementación, igual que `templates/dockerignore.ts` del SDK de TypeScript
(los dos pasan los vectores de `testdata/templates/dockerignore-vectors.json`):

- Cada línea se recorta; las vacías y las que empiezan por `#` se ignoran.
  `!` delante niega el patrón; un `!` solo no es un patrón.
- El patrón se limpia como `filepath.Clean` (sin `/` repetidas, sin `.`,
  `..` resuelto, sin `/` inicial ni final) y queda **anclado en la raíz del
  contexto**: `README.md` no excluye `docs/README.md`.
- `*` y `?` nunca cruzan `/`; `[...]` es una clase (`!` o `^` la niegan,
  `a-z` es un rango); `\\` escapa el carácter siguiente.
- Un segmento `**` equivale a cero o más segmentos completos, así que
  `**/.env` excluye también el `.env` de la raíz (el idioma que genera
  `docker init`). Un `**` final exige al menos un segmento (`logs/**` excluye
  lo de dentro de `logs`, no un fichero llamado `logs`). Un `**` pegado a
  otros caracteres dentro de un segmento cuenta como `*`, como en
  `.gitignore`.
- Un patrón que coincide con un directorio padre excluye todo lo que hay
  debajo, y el último patrón que coincide (con la ruta o con un padre) gana:
  `node_modules` seguido de `!node_modules/keep.js` deja pasar `keep.js`.

Sin expresiones regulares: el emparejado es por segmentos, con un coste
acotado por (segmentos del patrón por segmentos de la ruta) y, dentro de un
segmento, por (longitud del patrón por longitud del nombre), así que un
`.dockerignore` hostil no puede disparar un retroceso exponencial.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Final

#: Nombre del fichero de exclusión.
DOCKERIGNORE_FILENAME: Final = ".dockerignore"
PATH_SEPARATOR: Final = "/"
COMMENT_PREFIX: Final = "#"
NEGATION_PREFIX: Final = "!"
#: El segmento que equivale a cero o más segmentos completos.
GLOBSTAR: Final = "**"
CURRENT_DIR: Final = "."
PARENT_DIR: Final = ".."
STAR: Final = "*"
ANY_CHAR: Final = "?"
ESCAPE: Final = "\\"
CLASS_OPEN: Final = "["
CLASS_CLOSE: Final = "]"
CLASS_NEGATIONS: Final = ("!", "^")
CLASS_RANGE: Final = "-"
LINE_SEPARATOR: Final = "\n"
#: Docker quita un BOM UTF-8 al principio del fichero (`buildkit` `ReadAll`).
UTF8_BOM: Final = "\ufeff"


class _Wildcard(Enum):
    """`*` (cualquier tira dentro del segmento) y `?` (un carácter)."""

    STAR = STAR
    ANY = ANY_CHAR


@dataclass(frozen=True)
class _CharClass:
    """`[...]`: rangos `(desde, hasta)` inclusivos, quizá negados."""

    ranges: tuple[tuple[str, str], ...]
    negated: bool

    def admits(self, char: str) -> bool:
        inside = any(low <= char <= high for low, high in self.ranges)
        return inside != self.negated


#: Un token de un segmento: un literal de un carácter, un comodín o una clase.
_Token = str | _Wildcard | _CharClass


def clean_pattern(pattern: str) -> str:
    """`filepath.Clean` + quitar la `/` inicial, como hace Docker antes de
    compilar un patrón. `""` si no queda nada (no es un patrón)."""
    segments: list[str] = []
    for segment in pattern.split(PATH_SEPARATOR):
        if not segment or segment == CURRENT_DIR:
            continue
        if segment == PARENT_DIR and segments and segments[-1] != PARENT_DIR:
            segments.pop()
            continue
        segments.append(segment)
    return PATH_SEPARATOR.join(segments)


def _escaped_char(segment: str, index: int) -> tuple[str, int]:
    """El carácter en `index` (el siguiente si es un `\\` que escapa) y el
    índice tras él."""
    if segment[index] == ESCAPE and index + 1 < len(segment):
        return segment[index + 1], index + 2
    return segment[index], index + 1


def _read_class(segment: str, start: int) -> tuple[_CharClass, int] | None:
    """La clase que abre `segment[start] == "["` y el índice tras su `]`, o
    `None` si no se cierra (entonces el `[` es un literal). Un `]` justo
    tras la apertura es un literal, como en `filepath.Match`."""
    index = start + 1
    negated = index < len(segment) and segment[index] in CLASS_NEGATIONS
    if negated:
        index += 1
    ranges: list[tuple[str, str]] = []
    while index < len(segment):
        if segment[index] == CLASS_CLOSE and ranges:
            return _CharClass(tuple(ranges), negated), index + 1
        low, index = _escaped_char(segment, index)
        high = low
        is_range = (
            index + 1 < len(segment)
            and segment[index] == CLASS_RANGE
            and segment[index + 1] != CLASS_CLOSE
        )
        if is_range:
            high, index = _escaped_char(segment, index + 1)
        ranges.append((low, high))
    return None


def _tokenize(segment: str) -> tuple[_Token, ...]:
    tokens: list[_Token] = []
    index = 0
    while index < len(segment):
        char = segment[index]
        if char == CLASS_OPEN:
            parsed = _read_class(segment, index)
            if parsed is not None:
                tokens.append(parsed[0])
                index = parsed[1]
                continue
        if char == STAR:
            if not tokens or tokens[-1] is not _Wildcard.STAR:
                tokens.append(_Wildcard.STAR)
            index += 1
        elif char == ANY_CHAR:
            tokens.append(_Wildcard.ANY)
            index += 1
        else:
            literal, index = _escaped_char(segment, index)
            tokens.append(literal)
    return tuple(tokens)


def _token_admits(token: _Token, char: str) -> bool:
    if isinstance(token, _CharClass):
        return token.admits(char)
    return token is _Wildcard.ANY or token == char


def _segment_matches(tokens: Sequence[_Token], name: str) -> bool:
    """Glob de un solo segmento (`*` = cualquier tira, que aquí nunca lleva
    `/`): el algoritmo voraz con vuelta al último `*`, O(tokens por nombre)."""
    token_index = name_index = 0
    star_token = star_name = -1
    while name_index < len(name):
        if token_index < len(tokens) and tokens[token_index] is _Wildcard.STAR:
            star_token, star_name = token_index, name_index
            token_index += 1
        elif token_index < len(tokens) and _token_admits(tokens[token_index], name[name_index]):
            token_index += 1
            name_index += 1
        elif star_token >= 0:
            token_index = star_token + 1
            star_name += 1
            name_index = star_name
        else:
            return False
    while token_index < len(tokens) and tokens[token_index] is _Wildcard.STAR:
        token_index += 1
    return token_index == len(tokens)


def _collapse_globstars(segments: Sequence[str]) -> tuple[str, ...]:
    """`**/**` equivale a `**` (y al final sigue exigiendo un segmento):
    colapsarlos acota el coste de un patrón hostil con miles de `**`."""
    collapsed: list[str] = []
    for segment in segments:
        if segment == GLOBSTAR and collapsed and collapsed[-1] == GLOBSTAR:
            continue
        collapsed.append(segment)
    return tuple(collapsed)


@dataclass(frozen=True)
class _Pattern:
    #: `None` en el lugar de un segmento `**`.
    segments: tuple[tuple[_Token, ...] | None, ...]
    negated: bool

    @classmethod
    def compile(cls, cleaned: str, *, negated: bool) -> _Pattern:
        return cls(
            tuple(
                None if segment == GLOBSTAR else _tokenize(segment)
                for segment in _collapse_globstars(cleaned.split(PATH_SEPARATOR))
            ),
            negated,
        )

    def _ends(self, path: Sequence[str]) -> set[int]:
        """Las longitudes `n` tales que el patrón casa con `path[:n]`.
        Recorre los segmentos del patrón guardando las posiciones de la ruta
        alcanzables; un `**` abre todas desde la menor (cero o más; uno o
        más si es el último). Una posición `n` sólo depende de `path[:n]`,
        así que la misma pasada responde por la ruta y por cada padre."""
        reachable = {0}
        last = len(self.segments) - 1
        for index, tokens in enumerate(self.segments):
            if tokens is None:
                start = min(reachable) + (1 if index == last else 0)
                reachable = set(range(start, len(path) + 1))
            else:
                reachable = {
                    position + 1
                    for position in reachable
                    if position < len(path) and _segment_matches(tokens, path[position])
                }
            if not reachable:
                break
        return reachable

    def matches_or_parent(self, path: Sequence[str]) -> bool:
        """¿Casa con la ruta o con alguno de sus directorios padre?"""
        return any(end > 0 for end in self._ends(path))


class DockerIgnore:
    """Patrones de `.dockerignore`: `matches(relpath)` dice si excluir esa
    ruta relativa al contexto (con `/` como separador, igual que Docker)."""

    __slots__ = ("_patterns",)

    def __init__(self, patterns: Sequence[_Pattern]) -> None:
        self._patterns = tuple(patterns)

    @classmethod
    def from_text(cls, text: str) -> DockerIgnore:
        patterns: list[_Pattern] = []
        for raw_line in text.removeprefix(UTF8_BOM).split(LINE_SEPARATOR):
            line = raw_line.strip()
            if not line or line.startswith(COMMENT_PREFIX):
                continue
            negated = line.startswith(NEGATION_PREFIX)
            cleaned = clean_pattern((line[1:] if negated else line).strip())
            if cleaned:
                patterns.append(_Pattern.compile(cleaned, negated=negated))
        return cls(patterns)

    @classmethod
    def from_file(cls, path: Path) -> DockerIgnore:
        if not path.is_file():
            return cls(())
        return cls.from_text(path.read_text(encoding="utf-8"))

    def matches(self, relpath: str) -> bool:
        """El último patrón que coincide con `relpath` o con uno de sus
        directorios padre decide; sin ninguno, la ruta entra."""
        path = tuple(segment for segment in relpath.split(PATH_SEPARATOR) if segment)
        excluded = False
        for pattern in self._patterns:
            if pattern.negated != excluded:
                continue
            if pattern.matches_or_parent(path):
                excluded = not pattern.negated
        return excluded

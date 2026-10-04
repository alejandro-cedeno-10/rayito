"""Comprueba los ejemplos de código de la documentación (`docs/site/docs`).

Extrae cada bloque de código vallado de las páginas Markdown, también los que
van indentados dentro de pestañas (`=== "Python"`) o de admonitions, y los
comprueba sin AWS ni red:

- **Python** (`python`, `py`): `compile()` de cada bloque (sintaxis; un
  `await` fuera de una función es un error, como al ejecutarlo). Con
  `--ruff`, además `ruff check --select F821,F822,F823` (nombres sin definir:
  un import que falta); con `--mypy`, `mypy --check-untyped-defs
  --ignore-missing-imports` contra el paquete `rayito` instalado.
- **TypeScript** (`ts`, `typescript`), con `--typescript`: un proyecto
  temporal dentro de `clients/typescript/` (para resolver sus
  `node_modules`) con `rayito` y `rayito/e2b` apuntando a `src/`, un fichero
  por bloque con `export {}` (módulo ESM con top-level await) y `tsc -p`.
- **JSON** y **YAML**: `json.loads` / `yaml.safe_load` (YAML sólo si PyYAML
  está instalado).
- **CLI** (`--cli`): cada orden `rayito ...` de un bloque de shell (`bash`,
  `sh`, `shell`, `console`) y de un fragmento de código en línea del texto
  (`` `rayito stack deploy ...` ``) usa subcomandos y opciones que existen en
  la CLI instalada (el árbol de `typer` de `rayito.cli.app`). Las sinopsis
  (`[--param K=V]...`, `<componente>`, `status | destroy`) se aceptan: sólo
  se comprueban los subcomandos y las opciones `--x`/`-x`.
- **Estilo** (`--style`): las pestañas usan una etiqueta del conjunto
  cerrado (`Python`, `Python (async)`, `TypeScript`, `CLI`, `Shim E2B`), y
  un bloque Python o TypeScript que crea un sandbox lo libera (`with`,
  `await using`, `kill(`) o lo marca con una excepción.

Un bloque que no se puede comprobar se marca con un comentario HTML en la
línea anterior a la valla, **siempre con el motivo**:

    <!-- noqa: example: pseudocódigo, falta el contexto del agente -->

Un `noqa` sin motivo es un error. Un bloque cuyo contenido es sólo una
inclusión de `pymdownx.snippets` (`--8<-- "docs/examples/x.py"`) se
comprueba con el contenido del fichero incluido.

    python scripts/check_docs_examples.py                    # Python (compile) + JSON/YAML + estilo
    python scripts/check_docs_examples.py --ruff --mypy      # además ruff y mypy
    python scripts/check_docs_examples.py --typescript       # sólo TypeScript (tsc)
    python scripts/check_docs_examples.py --cli              # además, las órdenes `rayito ...`
    python scripts/check_docs_examples.py --list             # inventario por lenguaje

Sale con 1 si algún bloque falla, imprimiendo `página:línea [pestaña]` y el
error; con 2 si falta una herramienta pedida (ruff, mypy, tsc).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCS_DIR = REPO_ROOT / "docs" / "site" / "docs"
TS_CLIENT = REPO_ROOT / "clients" / "typescript"
TS_WORKDIR_NAME = ".docs-examples"

LANGUAGES = {
    "python": "python",
    "py": "python",
    "python3": "python",
    "ts": "ts",
    "typescript": "ts",
    "json": "json",
    "yaml": "yaml",
    "yml": "yaml",
    "bash": "shell",
    "sh": "shell",
    "shell": "shell",
    "console": "shell",
    "zsh": "shell",
}

TAB_LABELS = frozenset({"Python", "Python (async)", "TypeScript", "CLI", "Shim E2B"})

FENCE = re.compile(r"^(?P<indent>[ \t]*)(?P<fence>`{3,}|~{3,})[ \t]*(?P<info>[^`]*)$")
TAB = re.compile(r'^(?P<indent>[ \t]*)===\+?[ \t]+"(?P<label>[^"]*)"')
NOQA = re.compile(r"<!--\s*noqa:\s*example(?P<rest>[^>]*)-->")
SNIPPET_INCLUDE = re.compile(r'^\s*-{2}8<-{2}\s+"(?P<path>[^"]+)"\s*$')
CREATES_SANDBOX = re.compile(r"\bSandbox\.create\(|\bSandbox\.create\(\{")
RELEASES_SANDBOX = re.compile(r"\bwith\b|await using|\.kill\(|kill_all|\.close\(")


@dataclass
class Block:
    page: str
    line: int  # línea (1-based) de la primera línea de código, en la página
    language: str
    info: str
    code: str
    tab: str | None = None
    noqa: str | None = None  # motivo del noqa, o None
    included_from: str | None = None

    @property
    def where(self) -> str:
        tab = f" [{self.tab}]" if self.tab else ""
        source = f" (incluye {self.included_from})" if self.included_from else ""
        return f"{self.page}:{self.line}{tab}{source}"

    def at(self, line: int) -> str:
        """`página:línea` de la línea `line` (1-based) del bloque."""
        tab = f" [{self.tab}]" if self.tab else ""
        if self.included_from:
            return f"{self.included_from}:{line} (incluido en {self.page}:{self.line}{tab})"
        return f"{self.page}:{self.line + line - 1}{tab}"

    @property
    def stem(self) -> str:
        safe = re.sub(r"[^A-Za-z0-9]+", "_", self.page.removesuffix(".md"))
        return f"{safe}_L{self.line}"


@dataclass
class Report:
    blocks: list[Block] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    checked: Counter[str] = field(default_factory=Counter)
    skipped: Counter[str] = field(default_factory=Counter)

    def fail(self, where: str, message: str) -> None:
        self.problems.append(f"{where}: {message}")


def _dedent(lines: list[str], indent: str) -> list[str]:
    out = []
    for line in lines:
        if line.startswith(indent):
            out.append(line[len(indent) :])
        else:
            out.append(line.lstrip(" \t"))
    return out


def _resolve_include(path: str, page: Path) -> Path | None:
    for base in (REPO_ROOT, REPO_ROOT / "docs", DOCS_DIR, page.parent):
        candidate = base / path
        if candidate.is_file():
            return candidate
    return None


def extract_blocks(
    text: str, page: str, page_path: Path | None = None
) -> tuple[list[Block], list[str]]:
    """Bloques vallados de `text` (con su pestaña y su `noqa`) y los problemas
    de formato encontrados (un `noqa` sin motivo, una inclusión rota)."""
    lines = text.splitlines()
    blocks: list[Block] = []
    problems: list[str] = []
    tabs: list[tuple[int, str]] = []  # (ancho de indentación, etiqueta)
    index = 0
    while index < len(lines):
        line = lines[index]
        tab = TAB.match(line)
        if tab:
            width = len(tab.group("indent").expandtabs(4))
            tabs = [(w, label) for w, label in tabs if w < width]
            tabs.append((width, tab.group("label")))
            index += 1
            continue
        match = FENCE.match(line)
        if not match:
            if line.strip():
                width = len(line) - len(line.lstrip(" \t"))
                tabs = [(w, label) for w, label in tabs if w < width]
            index += 1
            continue
        indent = match.group("indent")
        fence = match.group("fence")
        info = match.group("info").strip()
        width = len(indent.expandtabs(4))
        tabs = [(w, label) for w, label in tabs if w < width]
        language_word = info.split()[0].lstrip(".").lower() if info else ""
        language_word = language_word.split("{")[0]
        body: list[str] = []
        end = index + 1
        while end < len(lines):
            closing = lines[end].strip()
            if closing.startswith(fence[0] * len(fence)) and set(closing) <= {fence[0]}:
                break
            body.append(lines[end])
            end += 1
        noqa_reason: str | None = None
        previous = index - 1
        while previous >= 0 and not lines[previous].strip():
            previous -= 1
        if previous >= 0:
            marker = NOQA.search(lines[previous])
            if marker:
                reason = marker.group("rest").strip().lstrip(":—-–").strip()
                if not reason:
                    problems.append(
                        f"{page}:{index + 1}: `noqa: example` sin motivo "
                        "(escribe `<!-- noqa: example: <motivo> -->`)"
                    )
                    reason = "(sin motivo)"
                noqa_reason = reason
        code_lines = _dedent(body, indent)
        block = Block(
            page=page,
            line=index + 2,
            language=LANGUAGES.get(language_word, language_word or "text"),
            info=info,
            code="\n".join(code_lines) + "\n",
            tab=tabs[-1][1] if tabs and tabs[-1][0] < width + 1 else None,
            noqa=noqa_reason,
        )
        includes = [
            SNIPPET_INCLUDE.match(code_line)
            for code_line in code_lines
            if code_line.strip()
        ]
        if includes and all(includes):
            parts = []
            for include in includes:
                assert include is not None
                target = _resolve_include(
                    include.group("path"), page_path or DOCS_DIR / page
                )
                if target is None:
                    problems.append(
                        f"{page}:{index + 1}: inclusión rota {include.group('path')!r}"
                    )
                    continue
                parts.append(target.read_text(encoding="utf-8"))
                block.included_from = include.group("path")
            block.code = "\n".join(parts)
        blocks.append(block)
        index = end + 1
    return blocks, problems


def collect(docs_dir: Path = DOCS_DIR) -> Report:
    report = Report()
    for path in sorted(docs_dir.rglob("*.md")):
        page = path.relative_to(docs_dir).as_posix()
        blocks, problems = extract_blocks(path.read_text(encoding="utf-8"), page, path)
        report.blocks.extend(blocks)
        report.problems.extend(problems)
    return report


LANGUAGE_WORDS = re.compile(
    r"python|typescript|javascript|\bts\b|\bjs\b|e2b|\bcli\b|curl|shell|bash",
    re.IGNORECASE,
)


def _is_language_label(label: str) -> bool:
    """Una pestaña de lenguaje (las que `content.tabs.link` sincroniza por su
    texto exacto). Las de otro tipo («Desde la release») no se restringen."""
    return bool(LANGUAGE_WORDS.search(label))


def check_style(report: Report, docs_dir: Path = DOCS_DIR) -> None:
    for path in sorted(docs_dir.rglob("*.md")):
        page = path.relative_to(docs_dir).as_posix()
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            tab = TAB.match(line)
            if (
                tab
                and _is_language_label(tab.group("label"))
                and tab.group("label") not in TAB_LABELS
            ):
                report.fail(
                    f"{page}:{number}",
                    f"pestaña {tab.group('label')!r} fuera del conjunto cerrado "
                    f"({', '.join(sorted(TAB_LABELS))})",
                )
    for block in report.blocks:
        if block.language not in {"python", "ts"} or block.noqa or block.included_from:
            continue
        if CREATES_SANDBOX.search(block.code) and not RELEASES_SANDBOX.search(
            block.code
        ):
            report.fail(
                block.where,
                "crea un sandbox y no lo libera (usa `with`, `await using` o `kill()`)",
            )


CLI_PROGRAM = "rayito"
#: Separadores de órdenes en una línea de shell; `|` también separa las
#: alternativas de una sinopsis (`status | destroy`, `desc|asc`).
SHELL_SEPARATORS = re.compile(r"&&|\|\||;|\|")
INLINE_CLI = re.compile(r"`(?P<command>rayito [^`]+)`")
ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
#: Lo que envuelve un argumento en una sinopsis: `[--x V]...`, `(a|b)`.
SYNOPSIS_PUNCTUATION = "[]()"
HELP_OPTIONS = frozenset({"--help"})
#: Tras `--`, todo es la orden que se pasa (`rayito sandbox exec ID -- ls -la`).
END_OF_OPTIONS = "--"
#: `rayito template build/status/logs`: alternativas de un mismo nivel.
SUBCOMMAND_ALTERNATIVES = "/"


@dataclass(frozen=True)
class CliNode:
    """Un nodo del árbol de la CLI: sus opciones y, si es un grupo, sus
    subcomandos."""

    options: frozenset[str]
    children: dict[str, CliNode] = field(default_factory=dict)


def load_cli_tree() -> CliNode | None:
    """El árbol de `rayito.cli.app` (typer), o `None` si la CLI no está
    instalada (`rayito[cli]`, o el grupo `dev` de `clients/python`)."""
    try:
        import typer.main
        from rayito.cli.app import app
    except ImportError:
        return None

    def node(command: object) -> CliNode:
        options = {
            option
            for param in getattr(command, "params", [])
            for option in [*getattr(param, "opts", []), *getattr(param, "secondary_opts", [])]
            if option.startswith("-")
        }
        children = {
            name: node(sub) for name, sub in (getattr(command, "commands", None) or {}).items()
        }
        return CliNode(frozenset(options), children)

    return node(typer.main.get_command(app))


def _cli_tokens(segment: str) -> list[str]:
    try:
        tokens = shlex.split(segment, comments=True)
    except ValueError:
        tokens = segment.split()
    while tokens and ENV_ASSIGNMENT.match(tokens[0]):
        tokens.pop(0)
    cleaned = []
    for token in tokens:
        token = token.strip(SYNOPSIS_PUNCTUATION).removesuffix("...").strip(SYNOPSIS_PUNCTUATION)
        if token:
            cleaned.append(token)
    return cleaned


def cli_problems(command_line: str, tree: CliNode) -> list[str]:
    """Los subcomandos y opciones de `command_line` que la CLI no tiene. Una
    palabra que acaba en `:` (`rayito doctor: 9 OK`) es salida, no una orden,
    y un `<marcador>` en lugar de un subcomando corta la comprobación."""
    problems = []
    for segment in SHELL_SEPARATORS.split(command_line):
        tokens = _cli_tokens(segment)
        if not tokens or tokens[0] != CLI_PROGRAM:
            continue
        node, path = tree, [CLI_PROGRAM]
        for token in tokens[1:]:
            if token == END_OF_OPTIONS:
                break
            if token.startswith("-") and token != "-":
                option = token.split("=", 1)[0]
                if option not in node.options | HELP_OPTIONS:
                    problems.append(f"`{' '.join(path)}` no tiene la opción {option}")
                continue
            if not node.children:
                continue
            if token.endswith(":") or token.startswith("<"):
                break
            if token in node.children:
                node = node.children[token]
                path.append(token)
                continue
            alternatives = token.split(SUBCOMMAND_ALTERNATIVES)
            missing = [name for name in alternatives if name not in node.children]
            for name in missing:
                problems.append(f"`{' '.join(path)}` no tiene el subcomando {name!r}")
            break
    return problems


def _shell_lines(code: str) -> list[tuple[int, str]]:
    """Las líneas lógicas (con las continuaciones `\\` unidas) y su número
    de línea (1-based) dentro del bloque."""
    lines: list[tuple[int, str]] = []
    pending: list[str] = []
    start = 1
    for number, raw in enumerate(code.splitlines(), start=1):
        line = raw.strip().removeprefix("$ ")
        if not pending:
            start = number
        if line.endswith("\\"):
            pending.append(line[:-1])
            continue
        pending.append(line)
        lines.append((start, " ".join(pending)))
        pending = []
    if pending:
        lines.append((start, " ".join(pending)))
    return lines


def _prose_lines(text: str) -> list[tuple[int, str]]:
    """Las líneas de `text` fuera de los bloques vallados."""
    fence: str | None = None
    lines = []
    for number, line in enumerate(text.splitlines(), start=1):
        match = FENCE.match(line)
        if match:
            marker = match.group("fence")
            if fence is None:
                fence = marker
            elif line.strip().startswith(fence[0] * len(fence)) and not match.group("info"):
                fence = None
            continue
        if fence is None:
            lines.append((number, line))
    return lines


def check_cli(report: Report, docs_dir: Path = DOCS_DIR, tree: CliNode | None = None) -> int:
    tree = tree or load_cli_tree()
    if tree is None:
        print(
            "check_docs_examples: falta la CLI de rayito (uv run --group dev ...)",
            file=sys.stderr,
        )
        return 2
    for block in report.blocks:
        if block.language != "shell":
            continue
        if block.noqa:
            report.skipped["cli"] += 1
            continue
        for line, command in _shell_lines(block.code):
            for problem in cli_problems(command, tree):
                report.fail(block.at(line), f"cli: {problem}")
        report.checked["cli"] += 1
    for path in sorted(docs_dir.rglob("*.md")):
        page = path.relative_to(docs_dir).as_posix()
        for number, line in _prose_lines(path.read_text(encoding="utf-8")):
            for match in INLINE_CLI.finditer(line):
                for problem in cli_problems(match.group("command"), tree):
                    report.fail(f"{page}:{number}", f"cli: {problem}")
                report.checked["cli"] += 1
    return 0


def check_python_syntax(report: Report) -> list[Block]:
    checked = []
    for block in report.blocks:
        if block.language != "python":
            continue
        if block.noqa:
            report.skipped["python"] += 1
            continue
        try:
            compile(block.code, block.where, "exec", dont_inherit=True)
        except SyntaxError as error:
            report.fail(block.at(error.lineno or 1), f"SyntaxError: {error.msg}")
        report.checked["python"] += 1
        checked.append(block)
    return checked


def check_data(report: Report) -> None:
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:  # pragma: no cover - PyYAML está en el grupo dev
        yaml = None
    for block in report.blocks:
        if block.language not in {"json", "yaml"}:
            continue
        if block.noqa:
            report.skipped[block.language] += 1
            continue
        if block.language == "json":
            try:
                json.loads(block.code)
            except json.JSONDecodeError as error:
                report.fail(
                    block.where, f"JSON inválido: {error.msg} (línea {error.lineno})"
                )
            report.checked["json"] += 1
        elif yaml is not None:
            try:
                yaml.safe_load(block.code)
            except yaml.YAMLError as error:
                report.fail(block.where, f"YAML inválido: {error}")
            report.checked["yaml"] += 1


def _write_python(blocks: list[Block], directory: Path) -> dict[str, Block]:
    mapping = {}
    for block in blocks:
        name = f"{block.stem}.py"
        (directory / name).write_text(block.code, encoding="utf-8")
        mapping[name] = block
    return mapping


DIAGNOSTIC = re.compile(
    r"^(?P<file>[^:\s]+\.(?:py|ts))[:(](?P<line>\d+)[:,]?(?P<rest>.*)$"
)


def _report_tool_output(
    report: Report, output: str, mapping: dict[str, Block], tool: str
) -> None:
    for raw in output.splitlines():
        match = DIAGNOSTIC.match(raw.strip())
        if not match:
            continue
        block = mapping.get(Path(match.group("file")).name)
        if block is None:
            continue
        line = int(match.group("line"))
        rest = match.group("rest").strip().lstrip("):0123456789,").strip()
        if tool == "mypy" and rest.startswith("note:"):
            continue
        report.fail(block.at(line), f"{tool}: {rest}")


def check_python_tools(
    report: Report, blocks: list[Block], *, ruff: bool, mypy: bool
) -> int:
    if not blocks or not (ruff or mypy):
        return 0
    with tempfile.TemporaryDirectory(prefix="docs-examples-") as temp:
        directory = Path(temp)
        mapping = _write_python(blocks, directory)
        if ruff:
            executable = shutil.which("ruff")
            if executable is None:
                print(
                    "check_docs_examples: falta ruff (uv run --group dev ...)",
                    file=sys.stderr,
                )
                return 2
            result = subprocess.run(
                [
                    executable,
                    "check",
                    "--isolated",
                    "--no-cache",
                    "--output-format",
                    "concise",
                    "--select",
                    "F821,F822,F823",
                    str(directory),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            _report_tool_output(report, result.stdout, mapping, "ruff")
        if mypy:
            if importlib.util.find_spec("mypy") is None:
                print(
                    "check_docs_examples: falta mypy (uv run --group dev ...)",
                    file=sys.stderr,
                )
                return 2
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "mypy",
                    "--check-untyped-defs",
                    "--ignore-missing-imports",
                    "--no-error-summary",
                    "--hide-error-context",
                    "--no-color-output",
                    "--show-error-codes",
                    "--cache-dir",
                    str(directory / ".mypy_cache"),
                    "--python-version",
                    "3.11",
                    *sorted(str(directory / name) for name in mapping),
                ],
                capture_output=True,
                text=True,
                check=False,
                cwd=directory,
            )
            _report_tool_output(report, result.stdout, mapping, "mypy")
    return 0


TS_CONFIG = {
    "extends": "../tsconfig.json",
    "compilerOptions": {
        "module": "ESNext",
        "moduleResolution": "Bundler",
        "noEmit": True,
        "paths": {
            "rayito": ["../src/index.ts"],
            "rayito/e2b": ["../src/e2b/index.ts"],
        },
    },
    "include": ["*.ts"],
}


def check_typescript(report: Report, client: Path = TS_CLIENT) -> int:
    blocks = [block for block in report.blocks if block.language == "ts"]
    tsc = (
        client
        / "node_modules"
        / ".bin"
        / ("tsc.cmd" if sys.platform == "win32" else "tsc")
    )
    if not tsc.exists():
        print(
            f"check_docs_examples: falta {tsc.relative_to(REPO_ROOT)} "
            "(pnpm install --frozen-lockfile en clients/typescript)",
            file=sys.stderr,
        )
        return 2
    workdir = client / TS_WORKDIR_NAME
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir()
    try:
        mapping: dict[str, Block] = {}
        for block in blocks:
            if block.noqa:
                report.skipped["ts"] += 1
                continue
            name = f"{block.stem}.ts"
            (workdir / name).write_text(block.code + "\nexport {};\n", encoding="utf-8")
            mapping[name] = block
            report.checked["ts"] += 1
        if not mapping:
            return 0
        (workdir / "tsconfig.json").write_text(
            json.dumps(TS_CONFIG, indent=2), encoding="utf-8"
        )
        result = subprocess.run(
            [str(tsc), "-p", str(workdir / "tsconfig.json"), "--pretty", "false"],
            capture_output=True,
            text=True,
            check=False,
            cwd=client,
        )
        output = result.stdout + result.stderr
        _report_tool_output(report, output, mapping, "tsc")
        if result.returncode != 0 and not report.problems:
            report.problems.append(f"tsc falló sin diagnósticos de ejemplos:\n{output}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--docs", type=Path, default=DOCS_DIR, help="directorio de páginas"
    )
    parser.add_argument(
        "--ruff", action="store_true", help="nombres sin definir con ruff"
    )
    parser.add_argument(
        "--mypy", action="store_true", help="tipos con mypy contra rayito"
    )
    parser.add_argument(
        "--typescript", action="store_true", help="sólo TypeScript, con tsc"
    )
    parser.add_argument(
        "--no-style", action="store_true", help="sin las reglas de estilo"
    )
    parser.add_argument(
        "--cli", action="store_true", help="órdenes `rayito ...` contra la CLI"
    )
    parser.add_argument("--list", action="store_true", help="inventario por lenguaje")
    args = parser.parse_args(argv)

    report = collect(args.docs)
    if args.list:
        counts = Counter(block.language for block in report.blocks)
        for language, count in sorted(counts.items()):
            print(f"{language}\t{count}")
        return 0
    status = 0
    if args.typescript:
        status = check_typescript(report)
    else:
        if not args.no_style:
            check_style(report, args.docs)
        python_blocks = check_python_syntax(report)
        check_data(report)
        status = check_python_tools(
            report, python_blocks, ruff=args.ruff, mypy=args.mypy
        )
        if not status and args.cli:
            status = check_cli(report, args.docs)
    if status:
        return status
    for problem in report.problems:
        print(problem)
    summary = ", ".join(
        f"{language} {report.checked[language]} comprobados"
        + (
            f" / {report.skipped[language]} con noqa"
            if report.skipped[language]
            else ""
        )
        for language in sorted(set(report.checked) | set(report.skipped))
    )
    print(
        f"check_docs_examples: {summary or 'nada que comprobar'}; {len(report.problems)} problemas"
    )
    return 1 if report.problems else 0


if __name__ == "__main__":
    sys.exit(main())

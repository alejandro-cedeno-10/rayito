"""``check_docs_examples.py``: la extracción de bloques (también indentados
dentro de pestañas), los `noqa` con y sin motivo, las inclusiones de
`pymdownx.snippets`, la compilación de Python, las reglas de estilo y, sobre
el sitio real, que todos los ejemplos de Python, JSON y YAML compilan y que
ninguna regla de estilo falla. El chequeo de TypeScript (tsc) corre en el job
de TypeScript de CI; aquí sólo si `clients/typescript/node_modules` existe."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import check_docs_examples as checker

TABBED_PAGE = """# Página

=== "Python"

    ```python
    from rayito import Sandbox

    with Sandbox.create() as sbx:
        print(sbx.sandbox_id)
    ```

=== "TypeScript"

    ```ts
    import { Sandbox } from "rayito";

    await using sbx = await Sandbox.create();
    ```

Texto fuera de las pestañas.

```python
x = 1
```
"""


def write_page(tmp_path: Path, name: str, text: str) -> Path:
    page = tmp_path / name
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(text, encoding="utf-8")
    return page


def test_extracts_indented_blocks_with_their_tab_and_line() -> None:
    blocks, problems = checker.extract_blocks(TABBED_PAGE, "p.md")
    assert problems == []
    assert [(b.language, b.tab, b.line) for b in blocks] == [
        ("python", "Python", 6),
        ("ts", "TypeScript", 15),
        ("python", None, 23),
    ]
    assert blocks[0].code.startswith(
        "from rayito import Sandbox\n\nwith Sandbox.create()"
    )
    assert blocks[0].at(4) == "p.md:9 [Python]"


def test_noqa_needs_a_reason() -> None:
    text = (
        "<!-- noqa: example: pseudocódigo -->\n```python\nesto no compila\n```\n\n"
        "<!-- noqa: example -->\n```python\ntampoco\n```\n"
    )
    blocks, problems = checker.extract_blocks(text, "p.md")
    assert blocks[0].noqa == "pseudocódigo"
    assert blocks[1].noqa == "(sin motivo)"
    assert len(problems) == 1 and "sin motivo" in problems[0]


def test_python_syntax_errors_point_at_the_page_line(tmp_path: Path) -> None:
    write_page(
        tmp_path,
        "p.md",
        "# x\n\n```python\nimport asyncio\nawait asyncio.sleep(1)\n```\n"
        "\n<!-- noqa: example: fragmento -->\n```python\nawait x\n```\n",
    )
    report = checker.collect(tmp_path)
    checker.check_python_syntax(report)
    assert report.checked["python"] == 1
    assert report.skipped["python"] == 1
    assert len(report.problems) == 1
    assert report.problems[0].startswith("p.md:5: SyntaxError")


def test_snippet_includes_are_checked_with_the_included_file(tmp_path: Path) -> None:
    write_page(
        tmp_path, "p.md", '```python\n--8<-- "docs/examples/langchain_tool.py"\n```\n'
    )
    report = checker.collect(tmp_path)
    assert report.problems == []
    (block,) = report.blocks
    assert block.included_from == "docs/examples/langchain_tool.py"
    assert "def run_python" in block.code


def test_a_broken_include_is_a_problem(tmp_path: Path) -> None:
    write_page(tmp_path, "p.md", '```python\n--8<-- "no/existe.py"\n```\n')
    report = checker.collect(tmp_path)
    assert any("inclusión rota" in problem for problem in report.problems)


def test_json_and_yaml_blocks_are_parsed(tmp_path: Path) -> None:
    write_page(tmp_path, "p.md", '```json\n{"a": 1,}\n```\n\n```yaml\na: [1, 2\n```\n')
    report = checker.collect(tmp_path)
    checker.check_data(report)
    assert len(report.problems) == 2


def test_style_rejects_language_tabs_outside_the_closed_set(tmp_path: Path) -> None:
    write_page(
        tmp_path,
        "p.md",
        '=== "Python (sync)"\n\n    ```python\n    x = 1\n    ```\n\n'
        '=== "Desde la release (recomendado)"\n\n    texto\n',
    )
    report = checker.collect(tmp_path)
    checker.check_style(report, tmp_path)
    assert len(report.problems) == 1
    assert "Python (sync)" in report.problems[0]


def test_style_requires_releasing_a_created_sandbox(tmp_path: Path) -> None:
    write_page(
        tmp_path,
        "p.md",
        "```python\nfrom rayito import Sandbox\n\nsbx = Sandbox.create()\n```\n\n"
        "```python\nfrom rayito import Sandbox\n\nsbx = Sandbox.create()\nsbx.kill()\n```\n",
    )
    report = checker.collect(tmp_path)
    checker.check_style(report, tmp_path)
    assert len(report.problems) == 1
    assert "no lo libera" in report.problems[0]


def test_the_real_site_examples_compile_and_follow_the_style() -> None:
    report = checker.collect()
    checker.check_style(report)
    checker.check_python_syntax(report)
    checker.check_data(report)
    assert report.problems == []
    assert report.checked["python"] >= 60
    assert sum(1 for block in report.blocks if block.language == "ts") >= 40


def test_the_real_site_typescript_examples_typecheck() -> None:
    if not (checker.TS_CLIENT / "node_modules" / ".bin" / "tsc").exists():
        pytest.skip("clients/typescript/node_modules no está instalado")
    report = checker.collect()
    assert checker.check_typescript(report) == 0
    assert report.problems == []


CLI_TREE = checker.CliNode(
    frozenset({"--json", "--region"}),
    {
        "stack": checker.CliNode(
            frozenset(),
            {
                "deploy": checker.CliNode(frozenset({"--param", "--yes"})),
                "status": checker.CliNode(frozenset({"--stack-name"})),
                "destroy": checker.CliNode(frozenset({"--yes"})),
            },
        ),
        "sandbox": checker.CliNode(
            frozenset(), {"exec": checker.CliNode(frozenset({"--cwd", "-c"}))}
        ),
    },
)


@pytest.mark.parametrize(
    "command",
    [
        "rayito stack deploy s3-mounts --param BucketName=b --yes",
        "rayito --json stack status s3-mounts",
        "rayito stack deploy <componente> [--param K=V]... [--yes]",
        "rayito stack status | destroy [--yes]",
        "rayito stack deploy/status/destroy",
        "rayito sandbox exec microvm-1 -c /tmp -- ls --all",
        "AWS_REGION=us-east-1 rayito stack deploy x && rayito stack destroy x --yes",
        "rayito stack <componente>",
        "rayito doctor: 9 OK",
        "echo rayito",
    ],
)
def test_cli_accepts_real_commands_and_synopses(command: str) -> None:
    assert checker.cli_problems(command, CLI_TREE) == []


@pytest.mark.parametrize(
    ("command", "fragment"),
    [
        ("rayito stack deploy x --params A=1", "--params"),
        ("rayito stack deploy x --json", "--json"),
        ("rayito stack apply x", "'apply'"),
        ("rayito stack deploy/apply", "'apply'"),
        ("rayito stacks list", "'stacks'"),
    ],
)
def test_cli_rejects_unknown_subcommands_and_options(command: str, fragment: str) -> None:
    problems = checker.cli_problems(command, CLI_TREE)
    assert len(problems) == 1 and fragment in problems[0]


def test_cli_checks_shell_blocks_and_inline_code(tmp_path: Path) -> None:
    write_page(
        tmp_path,
        "p.md",
        "Usa `rayito stack apply x`.\n\n```bash\nrayito stack deploy x \\\n  --nope 1\n```\n",
    )
    report = checker.collect(tmp_path)
    assert checker.check_cli(report, tmp_path, CLI_TREE) == 0
    assert report.problems == [
        "p.md:4: cli: `rayito stack deploy` no tiene la opción --nope",
        "p.md:1: cli: `rayito stack` no tiene el subcomando 'apply'",
    ]


def test_the_real_site_cli_commands_exist() -> None:
    if checker.load_cli_tree() is None:
        pytest.skip("la CLI de rayito no está instalada")
    report = checker.collect()
    assert checker.check_cli(report) == 0
    assert report.problems == []
    assert report.checked["cli"] >= 100

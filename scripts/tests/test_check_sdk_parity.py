"""``check_sdk_parity.py`` sobre un árbol mínimo en ``tmp_path``: el árbol
alineado pasa, y cada deriva (un nombre sólo en Python, un valor sólo en
TypeScript, una entrada de la lista de permitidos que ya no se justifica, una
clase ``Async*`` sin gemela síncrona) falla nombrando el símbolo. Además, el
árbol real del repositorio tiene que pasar."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import check_sdk_parity

REPO_ROOT = SCRIPTS.parent


def write(root: Path, relative: Path, text: str) -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


@pytest.fixture
def no_allowlists(monkeypatch: pytest.MonkeyPatch) -> None:
    """Los árboles de estos tests son sintéticos: sin las listas del repo."""
    monkeypatch.setattr(check_sdk_parity, "PYTHON_ONLY", {})
    monkeypatch.setattr(check_sdk_parity, "TYPESCRIPT_ONLY_VALUES", {})


def build_tree(
    root: Path,
    *,
    python_all: tuple[str, ...] = ("Sandbox", "SandboxException", "parse_chart"),
    typescript: str = (
        "export { Sandbox } from './sandbox.js';\n"
        "export { SandboxError, parseChart } from './errors.js';\n"
    ),
) -> None:
    listed = ", ".join(f'"{name}"' for name in python_all)
    for _surface, python_path, typescript_path in check_sdk_parity.SURFACES:
        write(root, python_path, f"__all__ = [{listed}]\n")
        write(root, typescript_path, typescript)


@pytest.mark.usefixtures("no_allowlists")
def test_an_aligned_tree_passes(tmp_path: Path) -> None:
    build_tree(tmp_path)
    assert check_sdk_parity.problems_for(tmp_path) == []


@pytest.mark.usefixtures("no_allowlists")
def test_exception_and_snake_case_names_are_compared_normalised(tmp_path: Path) -> None:
    build_tree(tmp_path)
    python = check_sdk_parity.python_exports(tmp_path / check_sdk_parity.SURFACES[0][1])
    typescript = check_sdk_parity.typescript_exports(
        tmp_path / check_sdk_parity.SURFACES[0][2]
    )
    assert check_sdk_parity.normalise("SandboxException") == check_sdk_parity.normalise(
        "SandboxError"
    )
    assert check_sdk_parity.normalise("parse_chart") == check_sdk_parity.normalise(
        "parseChart"
    )
    assert python == {"Sandbox", "SandboxException", "parse_chart"}
    assert set(typescript) == {"Sandbox", "SandboxError", "parseChart"}


@pytest.mark.usefixtures("no_allowlists")
def test_a_python_name_without_a_typescript_export_is_reported(tmp_path: Path) -> None:
    build_tree(
        tmp_path, python_all=("Sandbox", "SandboxException", "parse_chart", "Extra")
    )
    problems = check_sdk_parity.problems_for(tmp_path)
    assert any(
        "`Extra`" in problem and "TypeScript no lo exporta" in problem
        for problem in problems
    )


@pytest.mark.usefixtures("no_allowlists")
def test_a_typescript_value_without_a_python_name_is_reported(tmp_path: Path) -> None:
    build_tree(
        tmp_path,
        typescript=(
            "export { Sandbox } from './sandbox.js';\n"
            "export { SandboxError, parseChart, extraHelper } from './errors.js';\n"
        ),
    )
    problems = check_sdk_parity.problems_for(tmp_path)
    assert any("`extraHelper`" in problem for problem in problems)


@pytest.mark.usefixtures("no_allowlists")
def test_typescript_types_do_not_need_a_python_counterpart(tmp_path: Path) -> None:
    build_tree(
        tmp_path,
        typescript=(
            "export { Sandbox } from './sandbox.js';\n"
            "export { SandboxError, parseChart, type CreateOptions } from './errors.js';\n"
            "export type { OtherOptions } from './x.js';\n"
            "export interface Declared { readonly a: string }\n"
            "export type Alias = string;\n"
        ),
    )
    assert check_sdk_parity.problems_for(tmp_path) == []


@pytest.mark.usefixtures("no_allowlists")
def test_async_classes_need_their_sync_twin(tmp_path: Path) -> None:
    build_tree(
        tmp_path,
        python_all=(
            "Sandbox",
            "SandboxException",
            "parse_chart",
            "AsyncSandbox",
            "AsyncOrphan",
        ),
    )
    problems = check_sdk_parity.problems_for(tmp_path)
    assert any("`AsyncOrphan`" in problem for problem in problems)
    assert not any("`AsyncSandbox`" in problem for problem in problems)


def test_comments_and_aliases_in_typescript_exports_are_read_correctly() -> None:
    parsed = check_sdk_parity.typescript_exports_from_source(
        "// export { Hidden } from './a.js';\n"
        "/* export { AlsoHidden } from './b.js'; */\n"
        "export {\n  CapacityError as ServiceBusyError, // alias\n  type Logger as Log,\n} from './c.js';\n"
        "export default Sandbox;\n"
    )
    assert parsed == {"ServiceBusyError": False, "Log": True}


def test_a_stale_allowlist_entry_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    build_tree(tmp_path)
    monkeypatch.setattr(check_sdk_parity, "TYPESCRIPT_ONLY_VALUES", {})
    monkeypatch.setattr(
        check_sdk_parity,
        "PYTHON_ONLY",
        {"rayito": {"Gone": "ya no existe", "Sandbox": "ya coincide"}},
    )
    problems = check_sdk_parity.problems_for(tmp_path)
    assert any(
        "`Gone`" in problem and "ya no existe" in problem for problem in problems
    )
    assert any(
        "`Sandbox`" in problem and "ya existe en el otro SDK" in problem
        for problem in problems
    )


def test_the_real_tree_is_aligned() -> None:
    assert check_sdk_parity.problems_for(REPO_ROOT) == []


@pytest.mark.usefixtures("no_allowlists")
def test_main_reports_ok_and_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    build_tree(tmp_path)
    assert check_sdk_parity.main(["--root", str(tmp_path)]) == 0
    assert capsys.readouterr().out.strip() == "OK"
    build_tree(
        tmp_path, python_all=("Sandbox", "SandboxException", "parse_chart", "Extra")
    )
    assert check_sdk_parity.main(["--root", str(tmp_path)]) == 1
    assert "KO" in capsys.readouterr().out
    assert check_sdk_parity.main(["--bogus"]) == 2


def write_cli_surface(
    root: Path,
    commands: list[tuple[list[str], bool]],
    runnable_groups: tuple[str, ...] = (),
) -> None:
    entries = [
        {
            "path": path,
            "group": group,
            "invoke_without_command": " ".join(path) in runnable_groups,
            "parameters": [],
        }
        for path, group in commands
    ]
    write(root, check_sdk_parity.CLI_SURFACE, json.dumps({"commands": entries}))


def test_cli_report_marks_planned_commands_as_pending_with_their_phase(
    tmp_path: Path,
) -> None:
    write_cli_surface(
        tmp_path,
        [
            ([], True),
            (["sandbox"], True),
            (["sandbox", "list"], False),
            (["doctor"], False),
        ],
    )
    assert check_sdk_parity.cli_report(tmp_path) == [
        "PENDIENTE fase 1 rayito sandbox list",
        "PENDIENTE fase 2 rayito doctor",
    ]


def test_cli_report_counts_a_group_that_runs_without_subcommand_as_a_command(
    tmp_path: Path,
) -> None:
    write_cli_surface(
        tmp_path,
        [(["sandbox"], True), (["sandbox", "list"], False)],
        runnable_groups=("sandbox",),
    )
    assert check_sdk_parity.cli_report(tmp_path) == [
        "SIN FASE rayito sandbox",
        "PENDIENTE fase 1 rayito sandbox list",
    ]


def test_cli_phases_missing_from_golden_names_the_stale_entries(tmp_path: Path) -> None:
    write_cli_surface(tmp_path, [(["sandbox", "list"], False)])
    missing = check_sdk_parity.cli_phases_missing_from_golden(tmp_path)
    assert "sandbox list" not in missing
    assert "doctor" in missing


def test_every_cli_phase_command_exists_in_the_real_golden() -> None:
    assert check_sdk_parity.cli_phases_missing_from_golden(REPO_ROOT) == []


def test_cli_report_flags_a_command_without_an_assigned_phase(tmp_path: Path) -> None:
    write_cli_surface(tmp_path, [(["brand-new"], False)])
    assert check_sdk_parity.cli_report(tmp_path) == ["SIN FASE rayito brand-new"]


def write_typescript_command(root: Path, name: str, *paths: list[str]) -> None:
    definitions = "\n".join(
        "export const command = defineCommand({\n  path: ["
        + ", ".join(f'"{word}"' for word in path)
        + '],\n  help: "una ayuda",\n});'
        for path in paths
    )
    write(root, check_sdk_parity.TYPESCRIPT_CLI_COMMANDS / name, definitions)


def test_cli_report_marks_a_command_typescript_defines_as_ok(tmp_path: Path) -> None:
    write_cli_surface(
        tmp_path, [(["sandbox", "list"], False), (["sandbox", "info"], False)]
    )
    write_typescript_command(tmp_path, "sandbox.ts", ["sandbox", "list"])
    assert check_sdk_parity.cli_report(tmp_path) == [
        "OK rayito sandbox list",
        "PENDIENTE fase 1 rayito sandbox info",
    ]


def test_typescript_cli_commands_reads_every_definition_of_every_file(
    tmp_path: Path,
) -> None:
    write_typescript_command(tmp_path, "a.ts", ["sandbox", "list"], ["doctor"])
    write_typescript_command(tmp_path, "nested/b.ts", ["stack", "status"])
    assert check_sdk_parity.typescript_cli_commands(tmp_path) == {
        "sandbox list",
        "doctor",
        "stack status",
    }


def test_typescript_cli_commands_ignores_a_commented_out_definition(
    tmp_path: Path,
) -> None:
    write(
        tmp_path,
        check_sdk_parity.TYPESCRIPT_CLI_COMMANDS / "a.ts",
        '// defineCommand({ path: ["doctor"] })\n',
    )
    assert check_sdk_parity.typescript_cli_commands(tmp_path) == set()


def test_typescript_cli_commands_without_a_cli_directory_is_empty(
    tmp_path: Path,
) -> None:
    assert check_sdk_parity.typescript_cli_commands(tmp_path) == set()


def test_no_command_of_the_real_golden_lacks_a_phase() -> None:
    lines = check_sdk_parity.cli_report(REPO_ROOT)
    assert lines
    assert not [
        line for line in lines if line.startswith(check_sdk_parity.UNPLANNED_LABEL)
    ]


def test_every_typescript_command_exists_in_the_real_golden() -> None:
    golden = set(check_sdk_parity.cli_leaf_commands(REPO_ROOT))
    assert check_sdk_parity.typescript_cli_commands(REPO_ROOT) <= golden


def test_cli_phases_do_not_repeat_a_command() -> None:
    commands = [
        c for commands in check_sdk_parity.CLI_PHASES.values() for c in commands
    ]
    assert len(commands) == len(set(commands))


@pytest.mark.usefixtures("no_allowlists")
def test_main_with_cli_fails_while_a_command_is_pending(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    build_tree(tmp_path)
    write_cli_surface(tmp_path, [(["sandbox", "list"], False)])
    assert check_sdk_parity.main(["--root", str(tmp_path), "--cli"]) == 1
    assert capsys.readouterr().out.splitlines() == [
        "PENDIENTE fase 1 rayito sandbox list",
        "KO la CLI de TypeScript no cubre: PENDIENTE fase 1 rayito sandbox list",
    ]


@pytest.mark.usefixtures("no_allowlists")
def test_main_with_cli_passes_once_typescript_defines_every_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    build_tree(tmp_path)
    write_cli_surface(tmp_path, [(["sandbox", "list"], False)])
    write_typescript_command(tmp_path, "sandbox.ts", ["sandbox", "list"])
    assert check_sdk_parity.main(["--root", str(tmp_path), "--cli"]) == 0
    assert capsys.readouterr().out.splitlines() == ["OK rayito sandbox list", "OK"]

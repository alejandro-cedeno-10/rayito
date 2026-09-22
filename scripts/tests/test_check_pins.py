"""``check_pins.py`` over crafted text and a temporary tree: the spellings the
old denylist grep missed (``@1.2.3``, ``@latest``, ``@release-v2`` and a
truncated ``@ab12cd34``) are findings, a full SHA with or without its version
comment and a local ``./`` action are not, every ``uvx`` without ``==`` is a
finding in both the bare and the ``--from`` form, ``main`` reports every file
and exits 1 — and the real repository is clean."""

from __future__ import annotations

import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import check_pins

REPO_ROOT = SCRIPTS.parent
FULL_SHA = "3d3c42e5aac5ba805825da76410c181273ba90b1"


def test_tag_alias_branch_and_short_sha_are_unpinned() -> None:
    text = """      - uses: actions/checkout@v7
      - uses: owner/action@main
      - uses: owner/action@master
      - uses: owner/action@release/1.0
      - uses: owner/action@1.2.3
      - uses: owner/action@latest
      - uses: owner/action@release-v2
      - uses: owner/action@ab12cd34"""

    findings = check_pins.unpinned_actions(text)

    assert [number for number, _, _ in findings] == [1, 2, 3, 4, 5, 6, 7, 8]
    assert all(reason == check_pins.ACTION_REASON for _, _, reason in findings)


def test_full_sha_local_action_and_quoted_uses_are_pinned() -> None:
    text = f"""      - uses: actions/checkout@{FULL_SHA} # v7.0.1
      - uses: actions/checkout@{FULL_SHA}
      - uses: github/codeql-action/upload-sarif@{FULL_SHA} # v4.31.2
      - uses: ./.github/actions/local
      # - uses: foo@v1
      - name: no unpinned actions (every `uses:` is a commit SHA)
        run: grep -nE 'uses: [^@]+@v[0-9]' .github/workflows/*.yml"""

    assert check_pins.unpinned_actions(text) == []


def test_uvx_without_a_version_is_a_finding() -> None:
    text = """\tuvx twine check dist/*
\tuvx cfn-lint --version
          uvx ruff check .
          uvx --from "rayito[mcp]" rayito-mcp"""

    findings = check_pins.unpinned_uvx(text)

    assert [number for number, _, _ in findings] == [1, 2, 3, 4]
    assert all(reason == check_pins.UVX_REASON for _, _, reason in findings)


def test_pinned_uvx_invocations_pass() -> None:
    text = """        run: uvx pip-audit==2.10.1 -r req.txt --no-deps --strict
\tuvx twine==7.0.0 check dist/*
\tuvx ruff==0.16.7 format --check .
\tuvx cfn-lint==1.56.3 -- $(EGRESS_TEMPLATE) $(IAM_TEMPLATE)
          uvx --from pkg==1.0 tool
# uvx twine check dist/*"""

    assert check_pins.unpinned_uvx(text) == []


def test_main_reports_every_finding_and_exits_one(tmp_path: Path) -> None:
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(
        "jobs:\n  check:\n    steps:\n      - uses: actions/checkout@v7\n",
        encoding="utf-8",
    )
    (tmp_path / "Makefile").write_text(
        "lint:\n\tuvx ruff check scripts\n", encoding="utf-8"
    )

    stdout = io.StringIO()
    with redirect_stdout(stdout):
        code = check_pins.main([], root=tmp_path)

    printed = stdout.getvalue()
    assert code == 1
    assert ".github/workflows/ci.yml:4" in printed
    assert "Makefile:2" in printed


def test_main_accepts_a_clean_tree(tmp_path: Path) -> None:
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(
        f"jobs:\n  check:\n    steps:\n      - uses: actions/checkout@{FULL_SHA} # v7.0.1\n",
        encoding="utf-8",
    )
    (tmp_path / "Makefile").write_text(
        "lint:\n\tuvx ruff==0.16.7 check scripts\n", encoding="utf-8"
    )

    stdout = io.StringIO()
    with redirect_stdout(stdout):
        code = check_pins.main([], root=tmp_path)

    assert code == 0
    assert "OK 2 fichero(s)" in stdout.getvalue()


def test_the_repository_itself_is_clean() -> None:
    stdout = io.StringIO()
    with redirect_stdout(stdout):
        code = check_pins.main([], root=REPO_ROOT)

    assert code == 0, stdout.getvalue()

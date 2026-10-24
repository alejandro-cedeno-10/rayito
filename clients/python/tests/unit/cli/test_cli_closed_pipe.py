"""Una salida cerrada por el lector (`rayito ... | head`): la CLI sale en
silencio con `CLOSED_PIPE_EXIT_CODE`, como la de TypeScript
(`closed-pipe.test.ts`), sin traza ni `Exception ignored` al vaciar
`stdout`."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from rayito.cli import __main__ as entry

CLOSED_STDOUT_PROGRAM = (
    "import sys\n"
    "from rayito.cli.__main__ import main\n"
    "sys.argv = ['rayito', '--help']\n"
    "sys.exit(main())\n"
)


def test_a_broken_pipe_inside_the_app_exits_quietly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def closed(**_: object) -> None:
        raise BrokenPipeError

    monkeypatch.setattr("rayito.cli.app.app", closed)
    redirected: list[int] = []
    monkeypatch.setattr(os, "dup2", lambda _source, target: redirected.append(target))
    monkeypatch.setattr(sys.stdout, "fileno", lambda: 1, raising=False)

    assert entry.main() == entry.CLOSED_PIPE_EXIT_CODE
    assert redirected == [1]
    assert capsys.readouterr().err == ""


def test_a_closed_stdout_leaves_no_traceback() -> None:
    process = subprocess.Popen(
        [sys.executable, "-c", CLOSED_STDOUT_PROGRAM],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None
    process.stdout.close()
    _, stderr = process.communicate(timeout=60)

    assert process.returncode == entry.CLOSED_PIPE_EXIT_CODE
    assert b"Traceback" not in stderr
    assert b"BrokenPipeError" not in stderr

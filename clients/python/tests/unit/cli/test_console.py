"""`rayito.cli._console.echo`: el texto que llega de fuera (logs de
CloudWatch, logs de build, mensajes de AWS) nunca mete secuencias de escape
en una terminal."""

from __future__ import annotations

import io

import pytest

from rayito.cli import _console

OSC52_CLIPBOARD = "\x1b]52;c;ZXZpbA==\x07"
CURSOR_UP_AND_ERASE = "\x1b[1A\x1b[2K"


class _Terminal(io.StringIO):
    encoding = "utf-8"

    def isatty(self) -> bool:
        return True


def test_visible_controls_escapes_c0_del_and_c1_but_keeps_tab_and_newline() -> None:
    text = f"a\tb\nc{OSC52_CLIPBOARD}\r\x7f\x9b"
    assert _console.visible_controls(text) == "a\tb\nc\\x1b]52;c;ZXZpbA==\\x07\\x0d\\x7f\\x9b"


def test_echo_to_a_terminal_neutralises_escape_sequences(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Una línea de log con OSC 52 (escribe en el portapapeles) y un CSI que
    borra la línea anterior llega visible, sin efecto."""
    terminal = _Terminal()
    monkeypatch.setattr("sys.stdout", terminal)
    _console.echo(f"2026-10-04T00:00:00Z ok{OSC52_CLIPBOARD}{CURSOR_UP_AND_ERASE}terminated")
    written = terminal.getvalue()
    assert "\x1b" not in written and "\x07" not in written
    assert "\\x1b]52;c;ZXZpbA==\\x07" in written
    assert written.endswith("terminated\n")


def test_echo_to_stderr_terminal_is_also_sanitised(monkeypatch: pytest.MonkeyPatch) -> None:
    terminal = _Terminal()
    monkeypatch.setattr("sys.stderr", terminal)
    _console.echo(f"rayito: {CURSOR_UP_AND_ERASE}", err=True)
    assert "\x1b" not in terminal.getvalue()


def test_echo_to_a_pipe_keeps_the_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirigido a un fichero o a otra orden, el texto sale tal cual."""
    pipe = io.StringIO()
    monkeypatch.setattr("sys.stdout", pipe)
    _console.echo(f"x{CURSOR_UP_AND_ERASE}")
    assert pipe.getvalue() == f"x{CURSOR_UP_AND_ERASE}\n"

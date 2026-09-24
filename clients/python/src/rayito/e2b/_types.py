"""Alias de tipos con los nombres de E2B 2.51 (`e2b.sandbox.commands`,
`e2b_code_interpreter.models`). Son alias, no clases: `Username` es `str`."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal, TypeAlias, TypeVar

T = TypeVar("T")

Username: TypeAlias = str
Stdout: TypeAlias = str
Stderr: TypeAlias = str
PtyOutput: TypeAlias = bytes
MIMEType: TypeAlias = str
RunCodeLanguage: TypeAlias = (
    Literal["python", "javascript", "typescript", "r", "java", "bash"] | str
)
OutputHandler: TypeAlias = Callable[[T], Any]

__all__ = [
    "MIMEType",
    "OutputHandler",
    "PtyOutput",
    "RunCodeLanguage",
    "Stderr",
    "Stdout",
    "Username",
]

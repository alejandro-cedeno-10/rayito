"""Lo que `rayito agent deploy` hace con `--create-secrets-from` antes de
llamar al SDK (ADR-033 D4): enseñar qué secreto se creará o actualizará y con
qué variables (sólo nombres, nunca valores), y pedir confirmación porque
cuesta. Sin AWS: lee sólo el `.env` local.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Final

import typer

from rayito._agent._project_domain import AgentProject
from rayito._agent._secret_env_deploy import EnvFileSecret, read_env_file_secret

CREATE_SECRETS_LINE: Final = (
    "secreto {name}: se crea o se actualiza con {variables} (cuesta $0,40 al mes hasta que lo "
    "borres)"
)
CREATE_SECRETS_QUESTION: Final = "¿Crear o actualizar el secreto con esas variables?"
ENV_WARNING_LINE: Final = "aviso: {warning}"
CREATE_SECRETS_NEEDS_CONFIRMATION: Final = (
    "--create-secrets-from crea un secreto que cuesta dinero: confírmalo con --yes (o desde una "
    "terminal)"
)


def env_file_secret_line(secret: EnvFileSecret) -> str:
    return CREATE_SECRETS_LINE.format(name=secret.name, variables=", ".join(secret.variables))


def env_warning_line(warning: str) -> str:
    return ENV_WARNING_LINE.format(warning=warning)


def notices_of(warnings: tuple[str, ...], secret: EnvFileSecret | None) -> list[str]:
    """Lo que el plan avisa de `secret_env`: los `env_missing` y el secreto que
    `--create-secrets-from` creará (sólo nombres)."""
    lines = [env_warning_line(warning) for warning in warnings]
    if secret is not None:
        lines.append(env_file_secret_line(secret))
    return lines


def env_file_secret_of(project: AgentProject, path: Path | None) -> EnvFileSecret | None:
    """El secreto que crearía `--create-secrets-from` (sin crearlo), o `None`."""
    return None if path is None else read_env_file_secret(project.name, path)


def confirmed(*, yes: bool, json_output: bool) -> bool:
    """Con `--yes`, sí; en una terminal (sin `--json`), lo que se responda;
    si no, no."""
    if yes:
        return True
    if json_output or not sys.stdin.isatty():
        return False
    return typer.confirm(CREATE_SECRETS_QUESTION)

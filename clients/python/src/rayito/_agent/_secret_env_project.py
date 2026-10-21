"""`secret_env` y `secret_env_from` sobre un `AgentProject` (ADR-033 D4):
las banderas `--secret-env NOMBRE=<id>[#clave]` y `--secret-env-from
<id>[,prefix=P][,keys=A|B]`, leídas con las mismas reglas que el fichero de
proyecto (`testdata/agent-secret-env/parse.json`), y la comprobación de que
ninguna variable choca con las que el runtime ya fija. Un error es
`AgentProjectException` con `code` y `path`; su mensaje nunca repite el valor
de la bandera, que podría ser un secreto mal pegado.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from typing import Final

from rayito._agent._project_domain import (
    AgentProject,
    _error,
    _guard,
    check_secret_env_name,
    check_secret_env_source,
    check_secret_id,
    secret_env_from_path,
    secret_env_path,
)
from rayito._agent._secret_env_domain import (
    KEY_SEPARATOR,
    SecretEnvRef,
    SecretEnvSource,
    SecretEnvValue,
    secret_env_names,
)

NAME_SEPARATOR: Final = "="
OPTION_SEPARATOR: Final = ","
KEYS_SEPARATOR: Final = "|"
PREFIX_OPTION: Final = "prefix"
KEYS_OPTION: Final = "keys"


def parse_secret_env_flag(value: str) -> tuple[str, SecretEnvValue]:
    """`NOMBRE=<id>[#clave]` -> el nombre y su secreto (un `SecretEnvRef` si
    lleva clave)."""
    name, separator, reference = value.partition(NAME_SEPARATOR)
    path = secret_env_path(name)
    if not separator:
        raise _error(path, "invalid_value", "se esperaba NOMBRE=<secreto>[#clave]")
    check_secret_env_name(name, path)
    secret, marker, key = reference.partition(KEY_SEPARATOR)
    if marker and not key:
        raise _error(path, "invalid_value", "falta la clave JSON tras '#'")
    check_secret_id(secret, path)
    return name, SecretEnvRef(secret, key=key) if key else secret


def _options(path: str, options: Sequence[str]) -> tuple[str, tuple[str, ...] | None]:
    prefix, keys = "", None
    for option in options:
        option_name, separator, option_value = option.partition(NAME_SEPARATOR)
        if separator and option_name == PREFIX_OPTION:
            prefix = option_value
        elif separator and option_name == KEYS_OPTION and option_value:
            keys = tuple(option_value.split(KEYS_SEPARATOR))
        else:
            raise _error(path, "invalid_value", "opción desconocida; admitidas: prefix=, keys=")
    return prefix, keys


def parse_secret_env_from_flag(value: str, index: int) -> SecretEnvSource:
    """`<id>[,prefix=P][,keys=A|B]` -> un `SecretEnvSource`; `index` es su
    posición entre las `--secret-env-from` (la ruta del error)."""
    path = secret_env_from_path(index)
    secret, *options = value.split(OPTION_SEPARATOR)
    prefix, keys = _options(path, options)
    check_secret_id(secret, f"{path}.secret")
    source = _guard(path, lambda: SecretEnvSource(secret, prefix=prefix, keys=keys))
    return check_secret_env_source(source, path)


def parse_secret_env_flags(values: Sequence[str]) -> dict[str, SecretEnvValue]:
    """Las `--secret-env` repetidas; un nombre repetido gana el último."""
    return dict(parse_secret_env_flag(value) for value in values)


def parse_secret_env_from_flags(values: Sequence[str]) -> tuple[SecretEnvSource, ...]:
    return tuple(parse_secret_env_from_flag(value, index) for index, value in enumerate(values))


def with_secret_env(
    project: AgentProject,
    secret_env: Mapping[str, SecretEnvValue],
    secret_env_from: Sequence[SecretEnvSource],
) -> AgentProject:
    """El proyecto con las banderas ya leídas sumadas a su `secret_env` (la
    bandera gana) y a su `secret_env_from` (detrás de las del fichero)."""
    if not secret_env and not secret_env_from:
        return project
    return dataclasses.replace(
        project,
        secret_env={**project.secret_env, **secret_env},
        secret_env_from=(*project.secret_env_from, *secret_env_from),
    )


def check_runtime_collisions(project: AgentProject, envs: Mapping[str, str]) -> None:
    """Una variable de `secret_env` (o una clave de una lista `keys`) que el
    runtime ya fija en su entorno es `invalid_value`: el host la rechazaría al
    arrancar."""
    for name in secret_env_names(project.secret_env, project.secret_env_from):
        if name in envs:
            raise _error(secret_env_path(name), "invalid_value", "el runtime ya fija esa variable")

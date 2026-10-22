"""Lo que `deploy_agent()` y `rayito agent deploy` hacen con `secret_env`
además de pasarlo al runtime (ADR-033 D4): crear o actualizar, con
`create_secrets_from` / `--create-secrets-from`, un secreto JSON con los
valores de un `.env` y usarlo como `secret_env_from`, y avisar
(`check_env` / `--check-env`, o siempre que el proyecto use `secret_env`) de
las variables de `.env.example` que no reciben valor.

Crear el secreto es la única operación con coste y la única que lee los
valores de un `.env`; es explícita y nunca ocurre en la detección (ADR-029).
Ningún log, paso ni error nombra un valor.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Protocol

from rayito._agent._aws_resolve import GATEWAY_SECRET_ENV_VARS
from rayito._agent._deploy_plan import with_excluded_file
from rayito._agent._detect_env_names import (
    env_missing_warnings,
    missing_env_names,
    required_env_names,
)
from rayito._agent._detect_fs import LocalProjectTree
from rayito._agent._project_domain import AgentProject
from rayito._agent._secret_env_domain import (
    SecretEnvSource,
    is_reserved_name,
    parse_dotenv,
    secret_env_names,
    unlisted_prefixes,
    uses_secret_env,
)
from rayito._limits import AGENT_SECRET_NAME_TEMPLATE
from rayito.exceptions import InvalidArgumentException

logger = logging.getLogger("rayito.agent")

#: El hueco de `AGENT_SECRET_NAME_TEMPLATE` del secreto de un `.env`: el
#: secreto queda en `rayito/agents/<proyecto>/env`.
ENV_FILE_SECRET_SLOT: Final = "env"
#: El paso de `on_step` al crear o actualizar ese secreto; `detail` es
#: `created` o `updated`, nunca el nombre ni un valor.
STEP_SECRET_ENV_CREATED: Final = "secret_env_created"
#: El paso de `on_step` de cada aviso `env_missing`; `detail` es el aviso (sólo
#: el nombre de la variable).
STEP_ENV_MISSING: Final = "env_missing"
CREATED: Final = "created"
UPDATED: Final = "updated"


class EnvSecretStore(Protocol):
    """Lo que el despliegue usa de `SecretStore(prefix="")`."""

    def exists(self, name: str) -> bool: ...

    def create(self, name: str, value: str) -> Any: ...

    def update(self, name: str, value: str) -> Any: ...


@dataclass(frozen=True)
class EnvFileSecret:
    """El secreto JSON de un `.env`: su nombre, los nombres de sus variables
    y su valor (que nunca se muestra)."""

    name: str
    variables: tuple[str, ...]
    value: str = field(repr=False)


def env_file_secret_name(project: str) -> str:
    return AGENT_SECRET_NAME_TEMPLATE.format(project=project, provider=ENV_FILE_SECRET_SLOT)


def read_env_file_secret(project: str, path: str | os.PathLike[str]) -> EnvFileSecret:
    """Lee el `.env` de `path` (`testdata/agent-secret-env/dotenv.json`). Un
    fichero que no existe, sin variables o con un nombre reservado del
    runtime (`AWS_REGION`, `AWS_PROFILE`... de un `.env` local) es
    `InvalidArgumentException`, que nombra la variable y nunca un valor."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        raise InvalidArgumentException("create_secrets_from: no se pudo leer el fichero") from None
    variables = parse_dotenv(text)
    if not variables:
        raise InvalidArgumentException("create_secrets_from: el fichero no tiene variables")
    reserved = sorted(name for name in variables if is_reserved_name(name))
    if reserved:
        raise InvalidArgumentException(
            f"create_secrets_from: {reserved[0]} es un nombre reservado del runtime; "
            "quítalo del fichero"
        )
    return EnvFileSecret(
        name=env_file_secret_name(project),
        variables=tuple(sorted(variables)),
        value=json.dumps(variables, sort_keys=True, separators=(",", ":")),
    )


def with_env_file_source(project: AgentProject, secret: EnvFileSecret) -> AgentProject:
    """El proyecto que además lee ese secreto como `secret_env_from`, con sus
    variables como lista `keys` (así `check_env` las da por cubiertas)."""
    source = SecretEnvSource(secret.name, keys=secret.variables)
    if source in project.secret_env_from:
        return project
    return dataclasses.replace(project, secret_env_from=(*project.secret_env_from, source))


def publish_env_file_secret(store: EnvSecretStore, secret: EnvFileSecret) -> str:
    """Crea el secreto o, si ya existe, escribe una versión nueva; devuelve
    `CREATED` o `UPDATED`."""
    if store.exists(secret.name):
        store.update(secret.name, secret.value)
        return UPDATED
    store.create(secret.name, secret.value)
    return CREATED


def covered_env_names(project: AgentProject) -> frozenset[str]:
    """Lo que recibe valor sin leer ningún secreto: `[env]`, `secret_env`,
    las listas `keys` de `secret_env_from` y las variables de las pasarelas."""
    return frozenset(
        {
            *project.env,
            *secret_env_names(project.secret_env, project.secret_env_from),
            *GATEWAY_SECRET_ENV_VARS.values(),
        }
    )


def env_missing_of(project: AgentProject, *, check_env: bool) -> tuple[str, ...]:
    """Los avisos `env_missing` del proyecto: sólo con `check_env` o si usa
    `secret_env`, y sólo si tiene código. Los nombres reservados (las
    variables de región y de credenciales de AWS) los pone la plataforma."""
    if not check_env and not uses_secret_env(project.secret_env, project.secret_env_from):
        return ()
    if project.source is None:
        return ()
    required = [
        name
        for name in required_env_names(LocalProjectTree(project.source))
        if not is_reserved_name(name)
    ]
    missing = missing_env_names(
        required, covered_env_names(project), unlisted_prefixes(project.secret_env_from)
    )
    return env_missing_warnings(missing)


@dataclass(frozen=True)
class PreparedSecretEnv:
    """El proyecto con el secreto de `create_secrets_from` ya conectado (aún
    sin crear), ese secreto y los avisos de las variables sin valor."""

    project: AgentProject
    secret: EnvFileSecret | None
    warnings: tuple[str, ...]


def prepare_secret_env(
    project: AgentProject, create_secrets_from: str | None, *, check_env: bool
) -> PreparedSecretEnv:
    """Conecta el secreto de `create_secrets_from` y calcula los avisos
    `env_missing`, que además registra en el logger `rayito.agent`. Si el
    fichero está dentro del código, se excluye de él (`with_excluded_file`):
    sus valores van al secreto, nunca a la imagen ni a la plantilla."""
    secret = (
        None
        if create_secrets_from is None
        else read_env_file_secret(project.name, create_secrets_from)
    )
    prepared = (
        project
        if secret is None
        else with_excluded_file(with_env_file_source(project, secret), create_secrets_from)
    )
    warnings = env_missing_of(prepared, check_env=check_env)
    for warning in warnings:
        logger.warning(warning)
    return PreparedSecretEnv(prepared, secret, warnings)


def env_missing_steps(warnings: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    """Cada aviso como `(STEP_ENV_MISSING, aviso)` para `on_step`."""
    return tuple((STEP_ENV_MISSING, warning) for warning in warnings)


def env_secret_store(region: str | None, session: Any) -> EnvSecretStore:
    """`SecretStore` con nombres completos; no llama a AWS al construirse."""
    from rayito._secrets import SecretStore

    return SecretStore(region=region, session=session, prefix="")


__all__ = [
    "STEP_ENV_MISSING",
    "STEP_SECRET_ENV_CREATED",
    "EnvFileSecret",
    "EnvSecretStore",
    "PreparedSecretEnv",
    "env_missing_of",
    "env_missing_steps",
    "prepare_secret_env",
    "publish_env_file_secret",
]

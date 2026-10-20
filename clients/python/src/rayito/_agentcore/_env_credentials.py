"""Los adaptadores que sólo leen las credenciales de variables de entorno
(ADR-031, D9): en AgentCore no hay pasarela de secretos y el cliente de Bedrock
de esos frameworks no usa la cadena de credenciales de AWS, así que el host
las lee del rol del runtime y se las pone en cada invocación. Puro: lo
importan el export del contexto de build y el plan de despliegue.
"""

from __future__ import annotations

from typing import Any, Final

from rayito._limits import (
    AGENTCORE_CREDENTIAL_PROCESS_RUNTIMES,
    AGENTCORE_ENV_CREDENTIAL_ADAPTERS,
)

#: Los ficheros del contexto de build y de la imagen (`contract.json`,
#: `constants.export.awsCredentials`).
REQUIREMENTS_NAME: Final = "requirements-aws-credentials.txt"
SCRIPT_NAME: Final = "aws_env_credentials.py"
#: Clave de `host.json` con el intérprete y el script que lee las credenciales.
HOST_KEY: Final = "aws_env_credentials"


def uses_env_credentials(runtime: Any) -> bool:
    """Si el runtime del proyecto es un `CustomRuntime` cuyo adaptador está en
    `AGENTCORE_ENV_CREDENTIAL_ADAPTERS`."""
    return getattr(runtime, "adapter", None) in AGENTCORE_ENV_CREDENTIAL_ADAPTERS


#: El perfil de `credential_process` (`contract.json`,
#: `constants.export.awsCredentials.credentialProcess`): OpenCode sólo arranca
#: su proveedor de Bedrock con un perfil o una variable de AWS, y en AgentCore
#: el rol sólo lo sirve el servicio de metadatos de la microVM.
CONFIG_NAME: Final = "aws-config"
PROFILE: Final = "rayito-agentcore"
HELPER_ARGUMENT: Final = "--credential-process"
CONFIG_FILE_ENV: Final = "AWS_CONFIG_FILE"
PROFILE_ENV: Final = "AWS_PROFILE"


def uses_credential_process(runtime: Any) -> bool:
    """Si el runtime del proyecto (un nombre del registro o un `AgentRuntime`)
    es uno de `AGENTCORE_CREDENTIAL_PROCESS_RUNTIMES`, por su `name`."""
    name = runtime if isinstance(runtime, str) else getattr(runtime, "name", None)
    return name in AGENTCORE_CREDENTIAL_PROCESS_RUNTIMES


def needs_helper(runtime: Any) -> bool:
    """Si la imagen necesita la herramienta que lee las credenciales del rol."""
    return uses_env_credentials(runtime) or uses_credential_process(runtime)


def config_text(python: str, script: str) -> str:
    """El `AWS_CONFIG_FILE`: un perfil cuyas credenciales salen de la
    herramienta (`contract.json`, `credentialProcess.configTemplate`)."""
    return f"[profile {PROFILE}]\ncredential_process = {python} {script} {HELPER_ARGUMENT}\n"


def profile_envs(config_path: str) -> dict[str, str]:
    """Las variables que apuntan al proceso de OpenCode a ese perfil."""
    return {CONFIG_FILE_ENV: config_path, PROFILE_ENV: PROFILE}

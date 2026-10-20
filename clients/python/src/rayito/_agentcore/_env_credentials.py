"""Los adaptadores que sólo leen las credenciales de variables de entorno
(ADR-031, D9): en AgentCore no hay pasarela de secretos y el cliente de Bedrock
de esos frameworks no usa la cadena de credenciales de AWS, así que el host
las lee del rol del runtime y se las pone en cada invocación. Puro: lo
importan el export del contexto de build y el plan de despliegue.
"""

from __future__ import annotations

from typing import Any, Final

from rayito._limits import AGENTCORE_ENV_CREDENTIAL_ADAPTERS

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

"""`cfn_image_properties` (ADR-032 D9.3): la configuración que `rayito image
publish` manda a `create`/`update-microvm-image`
(`_images_publish_domain.desired_configuration`) con los nombres de
`AWS::Lambda::MicrovmImage` (AWS_API_NOTES.md, «Constructos CDK»). Puro: sólo
la biblioteca estándar, así que se prueba sin `aws-cdk-lib`.

El esquema exige las 13 propiedades de configuración: lo que la API deja
fuera cuando está vacío (`AdditionalOsCapabilities`, `EnvironmentVariables`,
`EgressNetworkConnectors`) va aquí como lista vacía. `EnvironmentVariables` es
una lista de `Key`/`Value` ordenada por clave.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

ENVIRONMENT_KEY: Final = "environmentVariables"
OS_CAPABILITIES_KEY: Final = "additionalOsCapabilities"
NAME_PROPERTY: Final = "Name"
DESCRIPTION_PROPERTY: Final = "Description"
OS_CAPABILITIES_PROPERTY: Final = "AdditionalOsCapabilities"
ENVIRONMENT_PROPERTY: Final = "EnvironmentVariables"
EGRESS_PROPERTY: Final = "EgressNetworkConnectors"
TAG_KEY: Final = "Key"
TAG_VALUE: Final = "Value"


def pascal_case(value: Any) -> Any:
    """Cada clave de un mapa (y de los mapas que contiene) con la inicial en
    mayúscula: `minimumMemoryInMiB` es `MinimumMemoryInMiB` y `cloudWatch` es
    `CloudWatch`, que es como los nombra el esquema."""
    if isinstance(value, Mapping):
        return {key[:1].upper() + key[1:]: pascal_case(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [pascal_case(item) for item in value]
    return value


def camel_case(value: Any) -> Any:
    """La inversa de `pascal_case`: los nombres de las structs de
    `aws-cdk-lib` (`CfnMicrovmImage.HooksProperty` y compañía), que el L1
    valida antes de escribirlos con los nombres del esquema."""
    if isinstance(value, Mapping):
        return {key[:1].lower() + key[1:]: camel_case(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [camel_case(item) for item in value]
    return value


def environment_entries(environment: Mapping[str, str]) -> list[dict[str, str]]:
    """`EnvironmentVariables`: `{Key, Value}` ordenados por clave."""
    return [{TAG_KEY: key, TAG_VALUE: environment[key]} for key in sorted(environment)]


def cfn_image_properties(
    configuration: Mapping[str, Any], *, name: str, description: str
) -> dict[str, Any]:
    """Las propiedades de `AWS::Lambda::MicrovmImage` de una configuración
    de `desired_configuration`, con su nombre y su descripción."""
    rest = {
        key: value
        for key, value in configuration.items()
        if key not in (ENVIRONMENT_KEY, OS_CAPABILITIES_KEY)
    }
    properties: dict[str, Any] = {
        NAME_PROPERTY: name,
        DESCRIPTION_PROPERTY: description,
        **pascal_case(rest),
        OS_CAPABILITIES_PROPERTY: list(configuration.get(OS_CAPABILITIES_KEY, [])),
        EGRESS_PROPERTY: [],
        ENVIRONMENT_PROPERTY: environment_entries(configuration.get(ENVIRONMENT_KEY, {})),
    }
    return properties


__all__ = ["camel_case", "cfn_image_properties", "environment_entries", "pascal_case"]

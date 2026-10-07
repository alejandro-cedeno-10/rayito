"""Resolución de la región de AWS del SDK: un único sitio para todo cliente boto3.

botocore sólo lee `AWS_DEFAULT_REGION` y la región del perfil; el SDK de
TypeScript (AWS SDK v3) y la CLI leen además `AWS_REGION`. Para que los tres
coincidan, toda sesión que crea el SDK pasa por aquí con la misma prioridad:
`region=` explícita > sesión del llamante > `AWS_REGION` > `AWS_DEFAULT_REGION`
> perfil (los dos últimos, por la cadena de boto3).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Final

import boto3

REGION_ENV_VAR: Final = "AWS_REGION"
"""Variable de región del AWS SDK v3 y de la CLI que botocore no lee."""


def resolve_region(
    region: str | None,
    session: boto3.session.Session | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    """La región que usará el SDK, o `None` para delegar en la cadena de boto3
    (`AWS_DEFAULT_REGION` y el perfil). Una sesión del llamante es
    configuración explícita: su región gana a `AWS_REGION`."""
    if region:
        return region
    if session is not None:
        return str(session.region_name) if session.region_name else None
    return (os.environ if environ is None else environ).get(REGION_ENV_VAR) or None


def aws_session(session: boto3.session.Session | None, region: str | None) -> boto3.session.Session:
    """La sesión del llamante, o una nueva en la región resuelta."""
    return session or boto3.session.Session(region_name=resolve_region(region))

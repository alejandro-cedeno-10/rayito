"""La receta de la flota y las plazas calientes con pasarelas (ADR-034,
openspec `fleet-recipe`) contra AWS real, para Q191, Q194 y Q193 de
`AWS_API_NOTES.md`:

- **Q191 y Q194**: `deploy_agent()` de un ejemplo con `warm_vms=1`, sesiones
  dedicadas y plazas persistentes; otro proceso se reconecta sólo con el nombre
  y el token (`AgentFleet.connect(nombre)`, que lee la receta), invoca dos
  sesiones nuevas, sin id propio porque el host sólo acepta los suyos (la
  segunda cae en la plaza aparcada y necesita la pasarela del modelo),
  `status()` da el escalado de la receta y `stop()` borra la receta.
- **Q193** (sólo con `RAYITO_E2E_RENEWAL=1`): un despliegue con
  `renew_gateway_keys=True` sobre un secreto propio de la prueba, recién
  creado con un valor marcador: el despliegue escribe siempre una clave de
  corta duración (aunque el secreto sea más nuevo que
  `gatewayKeyRenewAfterSeconds`), el secreto deja de tener el marcador y la
  invocación funciona a través de la pasarela.

Variables de entorno, además de las de `agent_zero_config_support.py`
(`RAYITO_E2E=1`, `RAYITO_E2E_RUN_ID`, `RAYITO_E2E_ZC_BUCKET`,
`RAYITO_E2E_ZC_BASE`, `RAYITO_E2E_BEDROCK_SECRET`): `RAYITO_E2E_RECIPE=1`
activa el módulo. Coste: dos o tres VMs de 4 GB durante unos minutos y una
versión de imagen (menos de 0,30 USD). Todo se para en `finally`; el secreto
de Q193 se borra sin ventana de recuperación.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import uuid
from pathlib import Path
from typing import Final

import boto3
import pytest
from botocore.exceptions import ClientError

from rayito import AgentDeployOptions, AgentFleet, ScalingOptions, deploy_agent
from rayito._pool_backends import JsonFilePoolBackend

from .agent_project_support import new_access_token
from .agent_zero_config_support import (
    BUCKET_VAR,
    GATEWAY_NAME,
    MANIFEST,
    RUN_ID_VAR,
    SECRET_VAR,
    region,
    require,
    staged_sample,
)

pytestmark = pytest.mark.e2e

RECIPE_FLAG_VAR: Final = "RAYITO_E2E_RECIPE"
RENEWAL_FLAG_VAR: Final = "RAYITO_E2E_RENEWAL"
RECIPE_VARIANT: Final = "r"
RENEWAL_VARIANT: Final = "k"
SCALING: Final = ScalingOptions(min_vms=1, max_vms=2, warm_vms=1, sessions="dedicated")
SCALE_IN_SECONDS: Final = 300
PLACEHOLDER_KEY: Final = "Bearer marcador-sin-valor"
SECOND_PROCESS: Final = textwrap.dedent(
    """
    import json, os, sys
    from rayito import AgentFleet

    fleet = AgentFleet.connect(sys.argv[1], access_token=os.environ["RAYITO_ACCESS_TOKEN"],
                               region=sys.argv[2])
    first = fleet.invoke("Responde sólo: uno")
    second = fleet.invoke("Responde sólo: dos")
    status = fleet.status()
    fleet.stop()
    print(json.dumps({
        "texts": [bool(first.text), bool(second.text)],
        "members": len(status.members),
        "min": status.min_vms,
        "max": status.max_vms,
        "source": status.scaling_source,
    }))
    """
)


def require_recipe() -> None:
    if os.environ.get(RECIPE_FLAG_VAR) != "1":
        pytest.skip(f"exporta {RECIPE_FLAG_VAR}=1 (cabecera de este módulo)")


def recipe_exists(name: str) -> bool:
    ssm = boto3.session.Session(region_name=region()).client("ssm")
    try:
        ssm.get_parameter(Name=f"/rayito/agents/{name}/recipe", WithDecryption=False)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ParameterNotFound":
            return False
        raise
    return True


def test_a_reconnected_fleet_uses_the_recipe_and_the_warm_slot(tmp_path: Path) -> None:
    require_recipe()
    require(RUN_ID_VAR)
    sample = MANIFEST["samples"][0]
    path = staged_sample(sample, tmp_path, variant=RECIPE_VARIANT)
    token = new_access_token()
    fleet = deploy_agent(
        path,
        options=AgentDeployOptions(
            bucket=require(BUCKET_VAR),
            secret_names={GATEWAY_NAME: require(SECRET_VAR)},
            access_token=token,
            scaling=SCALING,
            scale_in_after_seconds=SCALE_IN_SECONDS,
            pool_backend=JsonFilePoolBackend(tmp_path / "pool.json"),
        ),
        region=region(),
    )
    name = fleet.name
    try:
        fleet.detach()
        assert recipe_exists(name)
        outcome = subprocess.run(
            [sys.executable, "-c", SECOND_PROCESS, name, region()],
            env={**os.environ, "RAYITO_ACCESS_TOKEN": token},
            capture_output=True,
            text=True,
            check=True,
        )
        report = json.loads(outcome.stdout.strip().splitlines()[-1])
        assert report["texts"] == [True, True]
        assert (report["min"], report["max"], report["source"]) == (1, 2, "recipe")
        assert not recipe_exists(name)
    finally:
        AgentFleet.connect(name, region=region(), use_recipe=False).stop()


def test_the_renewed_short_term_key_reaches_the_gateway(tmp_path: Path) -> None:
    require_recipe()
    if os.environ.get(RENEWAL_FLAG_VAR) != "1":
        pytest.skip(f"exporta {RENEWAL_FLAG_VAR}=1 (cabecera de este módulo)")
    run = require(RUN_ID_VAR)
    secrets = boto3.session.Session(region_name=region()).client("secretsmanager")
    secret_name = f"rayito-e2e/{run}/renewal-{uuid.uuid4().hex[:8]}"
    secret_arn = str(secrets.create_secret(Name=secret_name, SecretString=PLACEHOLDER_KEY)["ARN"])
    path = staged_sample(MANIFEST["samples"][0], tmp_path, variant=RENEWAL_VARIANT)
    fleet = None
    try:
        fleet = deploy_agent(
            path,
            options=AgentDeployOptions(
                bucket=require(BUCKET_VAR),
                secret_names={GATEWAY_NAME: secret_arn},
                access_token=new_access_token(),
                renew_gateway_keys=True,
                save_recipe=False,
            ),
            region=region(),
        )
        renewed = secrets.get_secret_value(SecretId=secret_name)["SecretString"]
        assert renewed != PLACEHOLDER_KEY and renewed.startswith("Bearer ")
        assert fleet.invoke("Responde sólo: hola").text
    finally:
        if fleet is not None:
            fleet.stop()
        secrets.delete_secret(SecretId=secret_name, ForceDeleteWithoutRecovery=True)

"""Cablea la aplicación con clientes de AWS reales a partir del entorno de la
función. Se ejecuta una vez por entorno de ejecución (`handler.py`); los
tests construyen `StackApp` con fakes y no pasan por aquí.

Lectura: el rol de ejecución (credenciales por defecto de la Lambda).
Acciones: el rol de acciones, por `AssumedRoleActions`, sólo si la pila lo
tiene. El plano de control de `lambda-microvms` lo conoce el SDK de AWS del
runtime sólo gracias al modelo que el zip lleva en `models/` y `AWS_DATA_PATH`
(`scripts/gen_stack_assets.py`).
"""

from __future__ import annotations

from collections.abc import Mapping

import boto3

from dashboard_stack.actions import AssumedRoleActions
from dashboard_stack.app import StackApp
from dashboard_stack.appearance import LiveAppearance, load_appearance, read_parameter
from dashboard_stack.audit import AuditLog
from dashboard_stack.config import (
    ENV_SURFACE,
    SURFACE_FULL,
    SURFACE_IAM_API,
    SURFACE_IAM_SHELL,
    ShellConfig,
    StackConfig,
)
from dashboard_stack.cost_cache import DynamoCostCache
from dashboard_stack.cost_guard import BudgetedCostSource, CostWindowGuard, DynamoMissBudget
from dashboard_stack.directory import CognitoDirectory
from dashboard_stack.identity import IamEndpointCache, parse_iam_endpoint
from dashboard_stack.jobs import InlineJobs, JobLog
from dashboard_stack.jwks import (
    ClientIdCache,
    JwksCache,
    fetch_jwks,
    issuer_url,
    list_user_pool_client_ids,
)
from dashboard_stack.shell import ShellApp
from dashboard_stack.tokens import TokenVerifier
from rayito._aws import LambdaMicrovmsControlPlane
from rayito._dashboard._api._router import DashboardApi
from rayito._dashboard._aws import CostExplorerSource
from rayito._dashboard._cost import UsageCost
from rayito._dashboard._fleet import Fleet
from rayito._dashboard._ports import CostSource
from rayito._dashboard._quotas import Quotas
from rayito._limits import COST_EXPLORER_REGION

JWKS_PATH = "/.well-known/jwks.json"


def build_app(environ: Mapping[str, str]) -> StackApp | ShellApp:
    """La aplicación de esta función según su `surface` (por defecto `full`)."""
    surface = environ.get(ENV_SURFACE, SURFACE_FULL)
    if surface == SURFACE_IAM_SHELL:
        return build_shell_app(ShellConfig.from_environ(environ))
    return build_stack_app(
        StackConfig.from_environ(environ),
        surface=SURFACE_IAM_API if surface == SURFACE_IAM_API else SURFACE_FULL,
    )


def build_shell_app(config: ShellConfig) -> ShellApp:
    session = boto3.session.Session(region_name=config.region)
    ssm = session.client("ssm", region_name=config.region)
    cognito = session.client("cognito-idp", region_name=config.region)
    return ShellApp(
        config=config,
        clients=ClientIdCache(
            lambda: list_user_pool_client_ids(cognito, config.user_pool_id, config.client_name)
        ),
        endpoints=IamEndpointCache(
            lambda: parse_iam_endpoint(
                read_parameter(ssm, config.identity_parameter), config.region
            )
        ),
        appearance=LiveAppearance(
            lambda: load_appearance(ssm, config.theme_parameter, config.logo_parameter)
        ),
    )


def build_stack_app(config: StackConfig, *, surface: str = SURFACE_FULL) -> StackApp:
    session = boto3.session.Session(region_name=config.region)
    log = JobLog()
    ssm = session.client("ssm", region_name=config.region)
    appearance = LiveAppearance(
        lambda: load_appearance(ssm, config.theme_parameter, config.logo_parameter)
    )
    read_plane = LambdaMicrovmsControlPlane.from_session(session, region=config.region)
    quotas = Quotas(region=config.region, session=session, control_plane=read_plane)
    cost = UsageCost(
        region=config.region,
        session=session,
        control_plane=read_plane,
        source=_cost_source(config, session),
    )
    read_fleet = Fleet(region=config.region, session=session, control_plane=read_plane)
    read_api = DashboardApi(
        fleet=read_fleet,
        quotas=quotas,
        cost=cost,
        region=config.region,
        jobs=InlineJobs(read_fleet, log),
        cost_explorer=config.cost_explorer,
    )

    def build_actions_api(actions_session: boto3.session.Session) -> DashboardApi:
        plane = LambdaMicrovmsControlPlane.from_session(actions_session, region=config.region)
        fleet = Fleet(
            region=config.region, session=actions_session, control_plane=plane, allow_actions=True
        )
        return DashboardApi(
            fleet=fleet,
            quotas=quotas,
            cost=cost,
            region=config.region,
            jobs=InlineJobs(fleet, log),
            cost_explorer=config.cost_explorer,
        )

    issuer = issuer_url(config.region, config.user_pool_id)
    cognito = session.client("cognito-idp", region_name=config.region)
    clients = ClientIdCache(
        lambda: list_user_pool_client_ids(cognito, config.user_pool_id, config.client_name)
    )
    verifier = TokenVerifier(
        issuer=issuer,
        keys=JwksCache(lambda: fetch_jwks(issuer + JWKS_PATH)),
        clients=clients,
    )
    actions = (
        None
        if config.actions_role_arn is None
        else AssumedRoleActions(
            sts=session.client("sts", region_name=config.region),
            role_arn=config.actions_role_arn,
            region=config.region,
            build_api=build_actions_api,
        )
    )
    return StackApp(
        config=config,
        verifier=verifier,
        clients=clients,
        read_api=read_api,
        actions=actions,
        audit=AuditLog(session.client("dynamodb", region_name=config.region), config.audit_table),
        appearance=appearance,
        directory=CognitoDirectory(cognito, config.user_pool_id),
        surface=surface,
    )


def _cost_source(config: StackConfig, session: boto3.session.Session) -> CostSource | None:
    """De fuera a dentro: ventana y región acotadas, caché en DynamoDB y, sólo
    en un fallo de caché, el presupuesto diario y Cost Explorer."""
    if not config.cost_explorer:
        return None
    client = session.client("ce", region_name=COST_EXPLORER_REGION)
    dynamodb = session.client("dynamodb", region_name=config.region)
    paid = BudgetedCostSource(
        CostExplorerSource(lambda: client), DynamoMissBudget(dynamodb, config.cache_table)
    )
    return CostWindowGuard(DynamoCostCache(paid, dynamodb, config.cache_table))

"""`rayito dashboard`: el panel de gestión local (ADR-026, fase 1). Sirve en
`127.0.0.1` una SPA y la API `/api/v1` sobre tus credenciales de AWS, hasta
que pulses Ctrl+C. De sólo lectura salvo `--allow-actions`. Con `--account`
(fase 3, opt-in) añade otras cuentas, de sólo lectura, por roles que asume.
`rayito dashboard cloudwatch` genera el dashboard de CloudWatch
(`dashboard_cloudwatch.py`).

Códigos de salida: 0 al cerrar con Ctrl+C; 2 por uso o entorno (puerto
ocupado o inválido, sin región o credenciales).
"""

from __future__ import annotations

import webbrowser
from pathlib import Path
from typing import Annotated, Final

import typer

from rayito._aws import LambdaMicrovmsControlPlane
from rayito._dashboard._accounts import AccountsApi, AccountSpec, parse_account_specs
from rayito._dashboard._api._contract import ApiBackend
from rayito._dashboard._api._router import DashboardApi
from rayito._dashboard._assume import AssumedRole, assumed_session
from rayito._dashboard._cost import UsageCost
from rayito._dashboard._fleet import Fleet
from rayito._dashboard._logs_aws import CloudWatchLogsSource
from rayito._dashboard._quotas import Quotas
from rayito._dashboard._theme import Appearance, Branding, parse_logo, parse_theme
from rayito._lifecycle_events._service import LifecycleEvents
from rayito._limits import (
    COST_CACHE_TTL_SECONDS,
    COST_EXPLORER_REQUEST_PRICE_USD,
    DASHBOARD_LOGO_MAX_BYTES,
    DASHBOARD_THEME_MAX_BYTES,
    PORT_MAX,
    PORT_MIN,
    SNAPSHOT_READ_PRICE_USD_PER_GB,
    SNAPSHOT_STORAGE_PRICE_USD_PER_GB_MONTH,
    SNAPSHOT_WRITE_PRICE_USD_PER_GB,
)
from rayito.cli._console import EXIT_USAGE, echo, fail
from rayito.cli._dashboard_server import LocalDashboard
from rayito.cli._session import Clients, clients_of
from rayito.cli.dashboard_cloudwatch import cloudwatch_command
from rayito.exceptions import InvalidArgumentException

ACCOUNTS_ENV: Final = "RAYITO_DASHBOARD_ACCOUNTS"
EXTERNAL_ID_ENV: Final = "RAYITO_DASHBOARD_EXTERNAL_ID"
THEME_ENV: Final = "RAYITO_DASHBOARD_THEME"
LOGO_ENV: Final = "RAYITO_DASHBOARD_LOGO"
THEME_FORMATS: Final = {".json": "json", ".css": "css"}
RANDOM_PORT = 0
BYTES_PER_KIB: Final = 1024
SECONDS_PER_HOUR: Final = 3600


def count_accounts(count: int) -> str:
    """«1 cuenta» / «3 cuentas»: la concordancia de número del aviso de arranque."""
    return f"{count} {'cuenta' if count == 1 else 'cuentas'}"


def usd_es(value: float) -> str:
    """Un precio en dólares con coma decimal, como en la documentación."""
    return f"${value:g}".replace(".", ",")


COST_EXPLORER_HELP = (
    "Permite consultar el coste facturado en Cost Explorer. DE PAGO: cada consulta se factura "
    f"({COST_EXPLORER_REQUEST_PRICE_USD:.2f} USD, también «Ignorar la caché») y la caché "
    f"local, en memoria, la conserva {COST_CACHE_TTL_SECONDS / SECONDS_PER_HOUR:g} h."
)
ALLOW_ACTIONS_HELP = (
    "Habilita matar, suspender y reanudar desde el panel (con confirmación). Sin esta opción "
    "el panel es de sólo lectura. Las acciones no facturan por la llamada, pero cambian la "
    "factura (AWS_API_NOTES.md §12): kill deja de facturar cómputo; suspend escribe un "
    f"snapshot ({usd_es(SNAPSHOT_WRITE_PRICE_USD_PER_GB)}/GB) y paga almacenamiento "
    f"({usd_es(SNAPSHOT_STORAGE_PRICE_USD_PER_GB_MONTH)}/GB-mes); resume lee el snapshot "
    f"({usd_es(SNAPSHOT_READ_PRICE_USD_PER_GB)}/GB) y vuelve a facturar cómputo al precio de "
    "su tamaño."
)
EVENTS_STACK_HELP = (
    "Nombre de la pila events-webhooks desplegada: activa la vista de eventos. DE PAGO, "
    "aunque mínimo: lecturas bajo demanda de DynamoDB sobre su tabla, sin sondeo."
)
LOG_GROUP_HELP = (
    "Grupo de CloudWatch de los logs (por defecto, por imagen). Leer los logs usa "
    "DescribeLogStreams y GetLogEvents, sin tarifa propia publicada por CloudWatch "
    "(https://aws.amazon.com/cloudwatch/pricing/): sólo cuentan la ingesta, el almacenamiento "
    "y Logs Insights."
)
COST_EXPLORER_BANNER = (
    f", coste facturado activado ({COST_EXPLORER_REQUEST_PRICE_USD:.2f} USD por consulta)"
)


dashboard_app = typer.Typer(
    help="Panel de gestión de MicroVMs: sin subcomando, el panel local (cuotas, coste, "
    "flota y acciones); `cloudwatch` genera un dashboard de CloudWatch. Apagado y de sólo "
    "lectura por defecto: las lecturas son gratuitas; lo que cuesta es opt-in "
    "(--cost-explorer y --events-stack, de pago; --allow-actions, donde resume vuelve a "
    "facturar cómputo).",
    no_args_is_help=False,
    add_completion=False,
)

dashboard_app.command("cloudwatch")(cloudwatch_command)


def build_api(
    clients: Clients,
    *,
    allow_actions: bool,
    cost_explorer: bool,
    events_stack: str | None,
    log_group: str | None,
    branding: Branding | None = None,
) -> DashboardApi:
    """Los servicios del SDK sobre la sesión de la CLI; ninguno llama a AWS
    hasta que la SPA pide su vista."""
    plane = clients.control_plane
    events = (
        None
        if events_stack is None
        else LifecycleEvents(
            stack_name=events_stack, region=clients.region, session=clients.session
        )
    )
    fleet = Fleet(
        region=clients.region,
        session=clients.session,
        control_plane=plane,
        events=events,
        allow_actions=allow_actions,
    )
    return DashboardApi(
        fleet=fleet,
        quotas=Quotas(region=clients.region, session=clients.session, control_plane=plane),
        cost=UsageCost(region=clients.region, session=clients.session, control_plane=plane),
        region=clients.region,
        logs=CloudWatchLogsSource(clients.logs, plane, log_group=log_group),
        events_configured=events is not None,
        cost_explorer=cost_explorer,
        branding=branding,
    )


def build_account_api(
    clients: Clients,
    spec: AccountSpec,
    external_id: str | None,
    branding: Branding | None = None,
) -> DashboardApi:
    """La API de una cuenta adicional: sólo lectura, sin coste facturado, logs
    ni eventos. La sesión asume el rol al firmar su primera petición, no
    ahora. Comparte la marca del panel principal para que cambiar de cuenta
    no cambie el nombre ni el logo."""
    role = AssumedRole(clients.sts, role_arn=spec.role_arn, external_id=external_id)
    session = assumed_session(role, region=clients.region)
    plane = LambdaMicrovmsControlPlane.from_session(session, region=clients.region)
    return DashboardApi(
        fleet=Fleet(region=clients.region, session=session, control_plane=plane),
        quotas=Quotas(region=clients.region, session=session, control_plane=plane),
        cost=UsageCost(region=clients.region, session=session, control_plane=plane),
        region=clients.region,
        branding=branding,
    )


def with_accounts(
    clients: Clients,
    primary: DashboardApi,
    specs: tuple[AccountSpec, ...],
    external_id: str | None,
    branding: Branding | None = None,
) -> ApiBackend:
    if not specs:
        return primary
    others = {spec.label: build_account_api(clients, spec, external_id, branding) for spec in specs}
    return AccountsApi(primary, others)


def read_bounded(path: Path, limit: int, flag: str) -> bytes:
    """Lee un fichero de aspecto sin cargar más de `limit` bytes: un fichero
    enorme se rechaza antes de leerlo entero. El mensaje nombra la opción,
    no la ruta."""
    try:
        with path.open("rb") as handle:
            data = handle.read(limit + 1)
    except OSError:
        fail(f"{flag}: no se pudo leer el fichero", code=EXIT_USAGE)
    return data


def load_appearance(theme: Path | None, logo: Path | None) -> Appearance:
    """El tema y el logo de usuario, validados antes de abrir el puerto.
    Cualquier fallo sale con código 2 y un mensaje que no repite valores."""
    try:
        parsed_theme = None
        if theme is not None:
            fmt = THEME_FORMATS.get(theme.suffix.lower())
            if fmt is None:
                fail("--theme: la extensión debe ser .json o .css", code=EXIT_USAGE)
            data = read_bounded(theme, DASHBOARD_THEME_MAX_BYTES, "--theme")
            try:
                parsed_theme = parse_theme(data.decode(), fmt=fmt)
            except UnicodeDecodeError:
                fail("--theme: el fichero no es texto UTF-8", code=EXIT_USAGE)
        parsed_logo = None
        if logo is not None:
            parsed_logo = parse_logo(read_bounded(logo, DASHBOARD_LOGO_MAX_BYTES, "--logo"))
    except InvalidArgumentException as exc:
        fail(str(exc), code=EXIT_USAGE)
    return Appearance(theme=parsed_theme, logo=parsed_logo)


@dashboard_app.callback(invoke_without_command=True)
def dashboard_callback(
    ctx: typer.Context,
    port: Annotated[
        int,
        typer.Option(
            "--port",
            help="Puerto local; por defecto uno libre al azar. Sólo escucha en 127.0.0.1.",
        ),
    ] = RANDOM_PORT,
    allow_actions: Annotated[
        bool,
        typer.Option(
            "--allow-actions",
            help=ALLOW_ACTIONS_HELP,
        ),
    ] = False,
    cost_explorer: Annotated[
        bool,
        typer.Option(
            "--cost-explorer",
            help=COST_EXPLORER_HELP,
        ),
    ] = False,
    events_stack: Annotated[
        str | None,
        typer.Option(
            "--events-stack",
            help=EVENTS_STACK_HELP,
        ),
    ] = None,
    log_group: Annotated[
        str | None,
        typer.Option("--log-group", help=LOG_GROUP_HELP),
    ] = None,
    theme: Annotated[
        Path | None,
        typer.Option(
            "--theme",
            envvar=THEME_ENV,
            help="Tema propio: un .json o .css con los tokens de diseño (colores, radios, "
            "fuentes). Se valida antes de arrancar; ver «Personalizar el tema» en la guía.",
        ),
    ] = None,
    logo: Annotated[
        Path | None,
        typer.Option(
            "--logo",
            envvar=LOGO_ENV,
            help="Logo propio: un PNG o SVG de hasta "
            f"{DASHBOARD_LOGO_MAX_BYTES // BYTES_PER_KIB} KiB, servido desde el propio panel.",
        ),
    ] = None,
    account: Annotated[
        list[str] | None,
        typer.Option(
            "--account",
            envvar=ACCOUNTS_ENV,
            help="Vista multicuenta (opt-in), repetible: NOMBRE=ARN_DEL_ROL de un rol de sólo "
            "lectura que se asume (plantilla: rayito stack deploy dashboard-read-role en la "
            "cuenta destino). También se lee de la variable "
            f"{ACCOUNTS_ENV}, con las entradas separadas por espacios, para armar el ARN "
            "en tiempo de ejecución sin escribirlo en la orden. Las cuentas adicionales son "
            "de sólo lectura y sin coste facturado.",
        ),
    ] = None,
    external_id: Annotated[
        str | None,
        typer.Option(
            "--external-id",
            envvar=EXTERNAL_ID_ENV,
            help="ExternalId que exigen los roles de --account (el mismo para todos).",
        ),
    ] = None,
    open_browser: Annotated[
        bool, typer.Option("--open", help="Abre la URL de arranque en el navegador.")
    ] = False,
) -> None:
    """Panel de gestión local de MicroVMs (cuotas, coste, flota y acciones).

    Apagado y de sólo lectura por defecto: las lecturas son gratuitas. Lo que
    cuesta es opt-in: --cost-explorer (Cost Explorer, de pago por consulta),
    --events-stack (lecturas de DynamoDB) y --allow-actions (resume vuelve a
    facturar cómputo)."""
    if ctx.invoked_subcommand is not None:
        return
    if port != RANDOM_PORT and not PORT_MIN <= port <= PORT_MAX:
        fail(f"--port: debe estar entre {PORT_MIN} y {PORT_MAX}", code=EXIT_USAGE)
    appearance = load_appearance(theme, logo)
    try:
        specs = parse_account_specs(account or [])
    except InvalidArgumentException as exc:
        fail(str(exc), code=EXIT_USAGE)
    clients = clients_of(ctx)
    primary = build_api(
        clients,
        allow_actions=allow_actions,
        cost_explorer=cost_explorer,
        events_stack=events_stack,
        log_group=log_group,
        branding=appearance.branding,
    )
    api = with_accounts(clients, primary, specs, external_id, appearance.branding)
    try:
        server = LocalDashboard(api, port=port, appearance=appearance)
    except OSError:
        api.close()
        fail(f"no se pudo abrir el puerto {port} en 127.0.0.1 (¿ocupado?)", code=EXIT_USAGE)
    mode = "con acciones (kill, suspend, resume)" if allow_actions else "sólo lectura"
    extra = f", más {count_accounts(len(specs))} de sólo lectura" if specs else ""
    paid = COST_EXPLORER_BANNER if cost_explorer else ""
    echo(f"Panel de Rayito ({clients.region}, {mode}{paid}{extra}). Ábrelo en tu navegador:")
    echo(server.url)
    echo("Ctrl+C para cerrar. El enlace sirve una sola vez.")
    if open_browser:
        webbrowser.open(server.url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        echo("Panel cerrado.")
    finally:
        server.shutdown()

"""Una imagen propia sobre rayito-base: dependencia fijada, app/ copiada y un servidor arrancado.

Ejecútalo desde la carpeta examples/csv-tickets-guide/ (`copy("app/")` es relativo a
`context_dir`). `RAYITO_BUCKET` es el bucket de artefactos que imprimió `rayito setup`.
Cada versión nueva factura almacenamiento de snapshot (mínimo una semana)."""

import os
import sys

from rayito import Sandbox, Template, wait_for_port

from _common import LOCAL_CSV, METADATA, ROOT, SANDBOX_CSV

BUCKET = os.environ.get("RAYITO_BUCKET")
NAME = os.environ.get("TEMPLATE_NAME", "tickets-demo")
SANDBOX_TIMEOUT_SECONDS = 300
COMMAND_TIMEOUT_SECONDS = 30
APP_TIMEOUT_SECONDS = 60
SERVER_PORT = 8000
APP_FILE = "summary.py"

template = (
    Template()
    .from_base_image("rayito-base")
    .pip_install(["tabulate==0.9.0"])
    .copy("app/", "/srv/app/")
    .set_envs({"MODE": "prod"})
    .set_start_cmd(f"python3 -m http.server {SERVER_PORT} -d /srv/app", wait_for_port(SERVER_PORT))
)


def check_image(sbx: Sandbox) -> None:
    """Comprueba la dependencia, el servidor y la app dentro del sandbox nuevo."""
    print(f"imagen {NAME}, versión {sbx.get_info().template_version}")
    version = sbx.commands.run(
        "python3 -c 'import tabulate; print(tabulate.__version__)'", timeout=COMMAND_TIMEOUT_SECONDS
    )
    print(f"tabulate {version.stdout.strip()}")
    listing = sbx.commands.run(f"curl -s localhost:{SERVER_PORT}/", timeout=COMMAND_TIMEOUT_SECONDS).stdout
    print(f"servidor en :{SERVER_PORT} sirve {APP_FILE}:", APP_FILE in listing)
    sbx.files.write(SANDBOX_CSV, LOCAL_CSV.read_text(encoding="utf-8"))
    print(sbx.commands.run(f"python3 /srv/app/{APP_FILE} {SANDBOX_CSV}", timeout=APP_TIMEOUT_SECONDS).stdout, end="")


def main() -> int:
    """Construye la imagen y la prueba en un sandbox."""
    if not BUCKET:
        print("falta RAYITO_BUCKET (lo imprime rayito setup)", file=sys.stderr)
        return 2
    if not (ROOT / "app" / APP_FILE).is_file():
        print(f"no encuentro app/{APP_FILE} bajo {ROOT.name}/", file=sys.stderr)
        return 2
    info = Template.build(template, NAME, bucket=BUCKET, context_dir=ROOT)
    with Sandbox.create(info.template_id, timeout=SANDBOX_TIMEOUT_SECONDS, metadata=METADATA) as sbx:
        check_image(sbx)
    return 0


if __name__ == "__main__":
    sys.exit(main())

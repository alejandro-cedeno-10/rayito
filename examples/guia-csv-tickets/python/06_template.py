"""Una imagen propia sobre rayito-base: dependencia fijada, app/ copiada y un servidor arrancado.

Ejecútalo desde la carpeta examples/guia-csv-tickets/ (copy("app/") es relativo a context_dir).
Cada versión nueva factura almacenamiento de snapshot (mínimo una semana)."""

import os
import sys

from rayito import Sandbox, Template, wait_for_port

from _comun import CSV_LOCAL, CSV_SANDBOX, METADATA, ROOT

BUCKET = os.environ.get("RAYITO_BUCKET")  # el bucket de artefactos que imprimió rayito setup
NAME = os.environ.get("TEMPLATE_NAME", "tickets-demo")

template = (
    Template()
    .from_base_image("rayito-base")
    .pip_install(["tabulate==0.9.0"])
    .copy("app/", "/srv/app/")
    .set_envs({"MODE": "prod"})
    .set_start_cmd("python3 -m http.server 8000 -d /srv/app", wait_for_port(8000))
)


def main() -> int:
    if not BUCKET:
        print("falta RAYITO_BUCKET (lo imprime rayito setup)", file=sys.stderr)
        return 2
    if not (ROOT / "app" / "resumen.py").is_file():
        print(f"no encuentro app/resumen.py bajo {ROOT.name}/", file=sys.stderr)
        return 2
    info = Template.build(template, NAME, bucket=BUCKET, context_dir=ROOT)
    with Sandbox.create(info.template_id, timeout=300, metadata=METADATA) as sbx:
        print(f"imagen {NAME}, versión {sbx.get_info().template_version}")
        version = sbx.commands.run("python3 -c 'import tabulate; print(tabulate.__version__)'", timeout=30)
        print(f"tabulate {version.stdout.strip()}")
        listing = sbx.commands.run("curl -s localhost:8000/", timeout=30).stdout
        print("servidor en :8000 sirve resumen.py:", "resumen.py" in listing)
        sbx.files.write(CSV_SANDBOX, CSV_LOCAL.read_text(encoding="utf-8"))
        print(sbx.commands.run(f"python3 /srv/app/resumen.py {CSV_SANDBOX}", timeout=60).stdout, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())

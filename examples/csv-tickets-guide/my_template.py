"""El template de 06_template, sólo como variable `template`, para construirlo con la CLI.

Desde esta carpeta (`copy("app/")` es relativo a ella):

    uv run rayito template build my_template.py --name tickets-demo --bucket "$RAYITO_BUCKET"

Cada versión nueva factura almacenamiento de snapshot (mínimo una semana).
Construye cada imagen desde un solo sitio: dos SDK no reutilizan la misma versión."""

from rayito import Template, wait_for_port

SERVER_PORT = 8000

template = (
    Template()
    .from_base_image("rayito-base")
    .pip_install(["tabulate==0.9.0"])
    .copy("app/", "/srv/app/")
    .set_envs({"MODE": "prod"})
    .set_start_cmd(f"python3 -m http.server {SERVER_PORT} -d /srv/app", wait_for_port(SERVER_PORT))
)

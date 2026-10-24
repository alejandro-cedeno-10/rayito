"""Volver a un sandbox con su id y su access token.

El token es un secreto: nunca lo imprimas. Desde otro proceso se hace igual,
con el id y el token guardados donde guardarías una contraseña."""

from rayito import Sandbox

from _common import METADATA, TEMPLATE

SANDBOX_TIMEOUT_SECONDS = 600
STATE_PATH = "/home/user/estado.txt"


def main() -> None:
    """Escribe con un handle y lee con otro conectado por id y token."""
    sbx = Sandbox.create(TEMPLATE, timeout=SANDBOX_TIMEOUT_SECONDS, metadata=METADATA)
    try:
        sbx.files.write(STATE_PATH, "paso 1 hecho")
        sandbox_id, token = sbx.sandbox_id, sbx.access_token
        again = Sandbox.connect(sandbox_id, access_token=token)
        print(again.files.read(STATE_PATH))
    finally:
        sbx.kill()


if __name__ == "__main__":
    main()

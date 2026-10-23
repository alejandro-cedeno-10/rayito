"""Volver a un sandbox con su id y su access token (el token es un secreto)."""

from rayito import Sandbox

from _comun import METADATA, TEMPLATE


def main() -> None:
    sbx = Sandbox.create(TEMPLATE, timeout=600, metadata=METADATA)
    try:
        sbx.files.write("/home/user/estado.txt", "paso 1 hecho")
        sandbox_id, token = sbx.sandbox_id, sbx.access_token  # nunca lo imprimas
        again = Sandbox.connect(sandbox_id, access_token=token)  # desde otro proceso, igual
        print(again.files.read("/home/user/estado.txt"))
    finally:
        sbx.kill()


if __name__ == "__main__":
    main()

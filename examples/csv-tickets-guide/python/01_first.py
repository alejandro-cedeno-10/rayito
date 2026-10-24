"""Un comando, un fichero de ida y vuelta y una celda con estado.

`with` llama a `kill()` al salir, también si algo lanza una excepción."""

from rayito import Sandbox

from _common import METADATA, TEMPLATE, run_cell

SANDBOX_TIMEOUT_SECONDS = 600
COMMAND_TIMEOUT_SECONDS = 30
NOTE_PATH = "/home/user/a.txt"


def main() -> None:
    """Ejecuta un comando, escribe y lee un fichero y evalúa una celda con estado."""
    with Sandbox.create(TEMPLATE, timeout=SANDBOX_TIMEOUT_SECONDS, metadata=METADATA) as sbx:
        print(sbx.commands.run("echo hola", timeout=COMMAND_TIMEOUT_SECONDS).stdout, end="")
        sbx.files.write(NOTE_PATH, "contenido")
        print(sbx.files.read(NOTE_PATH))
        print(run_cell(sbx, "x = 40; x + 2", "celda con estado").text)


if __name__ == "__main__":
    main()

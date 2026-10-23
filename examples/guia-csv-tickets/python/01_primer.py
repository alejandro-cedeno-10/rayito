"""Un comando, un fichero de ida y vuelta y una celda con estado."""

from rayito import Sandbox

from _comun import METADATA, TEMPLATE, run_cell


def main() -> None:
    # with llama a kill() al salir, también si algo lanza una excepción
    with Sandbox.create(TEMPLATE, timeout=600, metadata=METADATA) as sbx:
        print(sbx.commands.run("echo hola", timeout=30).stdout, end="")
        sbx.files.write("/home/user/a.txt", "contenido")
        print(sbx.files.read("/home/user/a.txt"))
        print(run_cell(sbx, "x = 40; x + 2", "celda con estado").text)


if __name__ == "__main__":
    main()

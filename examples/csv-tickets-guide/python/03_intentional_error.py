"""Un error a propósito: el fallo del código es un dato del resultado, no una excepción.

Lo que imprime es lo que le devolverías al modelo para que corrija su código."""

from rayito import Sandbox

from _common import CELL_TIMEOUT_SECONDS, LOCAL_CSV, METADATA, SANDBOX_CSV, TEMPLATE, run_cell

SANDBOX_TIMEOUT_SECONDS = 300


def main() -> None:
    """Carga el CSV y pide una columna que no existe."""
    with Sandbox.create(TEMPLATE, timeout=SANDBOX_TIMEOUT_SECONDS, metadata=METADATA) as sbx:
        sbx.files.write(SANDBOX_CSV, LOCAL_CSV.read_text(encoding="utf-8"))
        run_cell(sbx, f"import pandas as pd\ndf = pd.read_csv('{SANDBOX_CSV}')", "cargar el CSV")

        failed = sbx.run_code("df['no_existe']", timeout=CELL_TIMEOUT_SECONDS)
        if not failed.error:
            raise SystemExit("se esperaba un KeyError y la celda no falló")
        print(f"error esperado -> {failed.error.name}: {failed.error.value}")
        print(f"traceback: {len(failed.error.traceback.splitlines())} líneas")


if __name__ == "__main__":
    main()

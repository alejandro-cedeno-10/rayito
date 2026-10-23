"""Un error a propósito: el fallo del código es un dato del resultado, no una excepción."""

from rayito import Sandbox

from _comun import CSV_LOCAL, CSV_SANDBOX, METADATA, TEMPLATE, run_cell


def main() -> None:
    with Sandbox.create(TEMPLATE, timeout=300, metadata=METADATA) as sbx:
        sbx.files.write(CSV_SANDBOX, CSV_LOCAL.read_text(encoding="utf-8"))
        run_cell(sbx, f"import pandas as pd\ndf = pd.read_csv('{CSV_SANDBOX}')", "cargar el CSV")

        failed = sbx.run_code("df['no_existe']", timeout=60)
        if failed.error:
            # Esto es lo que le devolverías al modelo para que corrija su código.
            print(f"error esperado -> {failed.error.name}: {failed.error.value}")
            print(f"traceback: {len(failed.error.traceback.splitlines())} líneas")
        else:
            raise SystemExit("se esperaba un KeyError y la celda no falló")


if __name__ == "__main__":
    main()

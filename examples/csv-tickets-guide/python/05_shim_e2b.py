"""Un programa escrito para el SDK de E2B 2.x, con sólo el import cambiado.

Antes: `from e2b_code_interpreter import Sandbox`."""

from rayito.e2b import Sandbox

from _common import METADATA

SANDBOX_TIMEOUT_SECONDS = 300


def main() -> None:
    """Evalúa una celda con el shim de E2B."""
    with Sandbox.create(timeout=SANDBOX_TIMEOUT_SECONDS, metadata=METADATA) as sbx:
        execution = sbx.run_code("x = 40; x + 2")
        if execution.error:
            raise SystemExit(f"{execution.error.name}: {execution.error.value}")
        print(execution.text)


if __name__ == "__main__":
    main()

"""Un programa escrito para el SDK de E2B 2.x, con sólo el import cambiado."""

from rayito.e2b import Sandbox  # antes: from e2b_code_interpreter import Sandbox

with Sandbox.create(timeout=300, metadata={"app": "tickets-demo"}) as sbx:
    execution = sbx.run_code("x = 40; x + 2")
    if execution.error:
        raise SystemExit(f"{execution.error.name}: {execution.error.value}")
    print(execution.text)

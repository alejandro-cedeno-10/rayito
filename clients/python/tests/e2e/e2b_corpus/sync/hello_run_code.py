# Sigue https://docs.e2b.dev/quickstart ("Running your first Sandbox").
from rayito.e2b import Sandbox

sbx = Sandbox.create()
try:
    execution = sbx.run_code("x = 1; x + 1")
    assert execution.text == "2", execution
finally:
    sbx.kill()
print("hello_run_code ok")

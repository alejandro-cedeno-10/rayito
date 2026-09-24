# Sigue https://docs.e2b.dev/sandbox/persistence y https://docs.e2b.dev/sandbox/connect.
from rayito.e2b import Sandbox

sbx = Sandbox.create()
try:
    sbx.run_code("counter = 41")
    assert sbx.pause() is True
    assert Sandbox.pause(sbx.sandbox_id) is False

    resumed = Sandbox.connect(sbx.sandbox_id)
    assert resumed.run_code("counter + 1").text == "42"

    assert sbx.connect() is sbx
    assert sbx.run_code("counter").text == "41"
finally:
    sbx.kill()
print("pause_connect ok")

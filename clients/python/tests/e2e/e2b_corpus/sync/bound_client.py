# Sigue https://docs.e2b.dev/client ("SDK client").
from rayito.e2b import E2B

client = E2B()
sbx = client.Sandbox.create()
try:
    assert sbx.run_code("1 + 1").text == "2"
finally:
    sbx.kill()
print("bound_client ok")

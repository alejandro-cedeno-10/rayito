# Sigue https://docs.e2b.dev/code-interpreting/contexts.
from rayito.e2b import Sandbox

sbx = Sandbox.create()
try:
    context = sbx.create_code_context()
    assert sbx.run_code("value = 7", context=context).error is None
    assert sbx.run_code("value * 6", context=context).text == "42"
    assert context.id in [item.id for item in sbx.list_code_contexts()]

    sbx.restart_code_context(context)
    after_restart = sbx.run_code("value", context=context)
    assert after_restart.error is not None, after_restart
    assert after_restart.error.name == "NameError", after_restart.error

    sbx.remove_code_context(context)
    assert context.id not in [item.id for item in sbx.list_code_contexts()]
finally:
    sbx.kill()
print("contexts ok")

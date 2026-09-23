# Sigue https://docs.e2b.dev/code-interpreting/contexts, versión async.
import asyncio

from rayito.e2b import AsyncSandbox


async def main() -> None:
    sbx = await AsyncSandbox.create()
    try:
        context = await sbx.create_code_context()
        assert (await sbx.run_code("value = 7", context=context)).error is None
        assert (await sbx.run_code("value * 6", context=context)).text == "42"
        assert context.id in [item.id for item in await sbx.list_code_contexts()]

        await sbx.restart_code_context(context)
        after_restart = await sbx.run_code("value", context=context)
        assert after_restart.error is not None, after_restart
        assert after_restart.error.name == "NameError", after_restart.error

        await sbx.remove_code_context(context)
        assert context.id not in [item.id for item in await sbx.list_code_contexts()]
    finally:
        await sbx.kill()


asyncio.run(main())
print("contexts ok")

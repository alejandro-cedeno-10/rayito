# Sigue https://docs.e2b.dev/quickstart ("Running your first Sandbox"), versión async.
import asyncio

from rayito.e2b import AsyncSandbox


async def main() -> None:
    sbx = await AsyncSandbox.create()
    try:
        execution = await sbx.run_code("x = 1; x + 1")
        assert execution.text == "2", execution
    finally:
        await sbx.kill()


asyncio.run(main())
print("hello_run_code ok")

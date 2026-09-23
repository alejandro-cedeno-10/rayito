# Sigue https://docs.e2b.dev/sandbox/persistence y
# https://docs.e2b.dev/sandbox/connect, versión async.
import asyncio

from rayito.e2b import AsyncSandbox


async def main() -> None:
    sbx = await AsyncSandbox.create()
    try:
        await sbx.run_code("counter = 41")
        assert await sbx.pause() is True
        assert await AsyncSandbox.pause(sbx.sandbox_id) is False

        resumed = await AsyncSandbox.connect(sbx.sandbox_id)
        assert (await resumed.run_code("counter + 1")).text == "42"

        assert await sbx.connect() is sbx
        assert (await sbx.run_code("counter")).text == "41"
    finally:
        await sbx.kill()


asyncio.run(main())
print("pause_connect ok")

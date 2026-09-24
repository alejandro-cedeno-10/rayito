# Sigue https://docs.e2b.dev/client ("SDK client"), versión async.
import asyncio

from rayito.e2b import E2B


async def main() -> None:
    client = E2B()
    sbx = await client.AsyncSandbox.create()
    try:
        assert (await sbx.run_code("1 + 1")).text == "2"
    finally:
        await sbx.kill()


asyncio.run(main())
print("bound_client ok")

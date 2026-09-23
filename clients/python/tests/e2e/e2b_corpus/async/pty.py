# Sigue https://docs.e2b.dev/sandbox/pty ("Interactive terminal (PTY)"), versión async.
import asyncio

from rayito.e2b import AsyncSandbox, PtySize

MARKER = b"hola-2"


async def main() -> None:
    sbx = await AsyncSandbox.create()
    try:
        terminal = await sbx.pty.create(PtySize(24, 80), None)
        pid = terminal.pid
        terminal.disconnect()

        output = bytearray()
        seen = asyncio.Event()

        def on_data(data: bytes) -> None:
            output.extend(data)
            if MARKER in output:
                seen.set()

        reattached = await sbx.pty.connect(pid, on_data)
        waiter = asyncio.create_task(reattached.wait())
        await sbx.pty.send_stdin(pid, b"echo hola-$((1+1))\n")
        await asyncio.wait_for(seen.wait(), timeout=30)
        reattached.disconnect()
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)

        assert b"hola" in output
        assert await sbx.pty.kill(pid) is True
    finally:
        await sbx.kill()


asyncio.run(main())
print("pty ok")

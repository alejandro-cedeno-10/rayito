# Sigue https://docs.e2b.dev/commands y https://docs.e2b.dev/commands/background, versión async.
import asyncio

from rayito.e2b import AsyncSandbox


async def main() -> None:
    sbx = await AsyncSandbox.create()
    try:
        result = await sbx.commands.run("echo hi")
        assert result.stdout == "hi\n", result
        assert result.exit_code == 0

        command = await sbx.commands.run("sleep 1; echo done", True)
        running = [process.pid for process in await sbx.commands.list()]
        assert command.pid in running, running

        lines: list[str] = []
        finished = await command.wait(on_stdout=lines.append)
        assert finished.exit_code == 0, finished
        assert "".join(lines) == "done\n", lines
    finally:
        await sbx.kill()


asyncio.run(main())
print("commands ok")

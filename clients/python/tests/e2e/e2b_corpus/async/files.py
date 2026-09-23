# Sigue https://docs.e2b.dev/filesystem/read-write, versión async.
import asyncio

from rayito.e2b import AsyncSandbox, WriteEntry


async def main() -> None:
    sbx = await AsyncSandbox.create()
    try:
        written = await sbx.files.write_files(
            [
                WriteEntry(path="/home/user/corpus/a.txt", data="alpha"),
                WriteEntry(path="/home/user/corpus/b.txt", data=b"beta"),
            ]
        )
        assert [info.name for info in written] == ["a.txt", "b.txt"], written

        assert await sbx.files.read("/home/user/corpus/a.txt") == "alpha"
        assert await sbx.files.read("/home/user/corpus/b.txt", format="bytes") == b"beta"

        names = sorted(entry.name for entry in await sbx.files.list("/home/user/corpus"))
        assert names == ["a.txt", "b.txt"], names

        assert await sbx.files.exists("/home/user/corpus/a.txt") is True
        await sbx.files.remove("/home/user/corpus/a.txt")
        assert await sbx.files.exists("/home/user/corpus/a.txt") is False
    finally:
        await sbx.kill()


asyncio.run(main())
print("files ok")

# Sigue https://docs.e2b.dev/filesystem/watch, versión async.
import asyncio

from rayito.e2b import AsyncSandbox, FilesystemEvent, FilesystemEventType

WATCHED = "/home/user/watched"


async def main() -> None:
    sbx = await AsyncSandbox.create()
    try:
        await sbx.files.make_dir(WATCHED)
        events: list[FilesystemEvent] = []
        arrived = asyncio.Event()

        def on_event(event: FilesystemEvent) -> None:
            events.append(event)
            if event.entry is not None:
                arrived.set()

        handle = await sbx.files.watch_dir(WATCHED, on_event, include_entry=True)
        await sbx.files.write(f"{WATCHED}/note.txt", "hola")
        await asyncio.wait_for(arrived.wait(), timeout=15)
        await handle.stop()

        with_entry = [event for event in events if event.entry is not None]
        assert with_entry[0].name == "note.txt", with_entry
        assert with_entry[0].type in (FilesystemEventType.CREATE, FilesystemEventType.WRITE)
    finally:
        await sbx.kill()


asyncio.run(main())
print("watch ok")

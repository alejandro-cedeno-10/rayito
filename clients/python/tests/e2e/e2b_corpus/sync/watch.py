# Sigue https://docs.e2b.dev/filesystem/watch.
import time

from rayito.e2b import FilesystemEventType, Sandbox

WATCHED = "/home/user/watched"

sbx = Sandbox.create()
try:
    sbx.files.make_dir(WATCHED)
    handle = sbx.files.watch_dir(WATCHED, include_entry=True)
    sbx.files.write(f"{WATCHED}/note.txt", "hola")

    events = []
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        events.extend(handle.get_new_events())
        if any(event.entry is not None for event in events):
            break
        time.sleep(0.5)
    handle.stop()

    with_entry = [event for event in events if event.entry is not None]
    assert with_entry, events
    assert with_entry[0].name == "note.txt", with_entry
    assert with_entry[0].type in (FilesystemEventType.CREATE, FilesystemEventType.WRITE)
finally:
    sbx.kill()
print("watch ok")

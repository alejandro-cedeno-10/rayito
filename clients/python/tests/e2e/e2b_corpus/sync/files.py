# Sigue https://docs.e2b.dev/filesystem/read-write.
from rayito.e2b import Sandbox, WriteEntry

sbx = Sandbox.create()
try:
    written = sbx.files.write_files(
        [
            WriteEntry(path="/home/user/corpus/a.txt", data="alpha"),
            WriteEntry(path="/home/user/corpus/b.txt", data=b"beta"),
        ]
    )
    assert [info.name for info in written] == ["a.txt", "b.txt"], written

    assert sbx.files.read("/home/user/corpus/a.txt") == "alpha"
    assert sbx.files.read("/home/user/corpus/b.txt", format="bytes") == b"beta"

    names = sorted(entry.name for entry in sbx.files.list("/home/user/corpus"))
    assert names == ["a.txt", "b.txt"], names

    assert sbx.files.exists("/home/user/corpus/a.txt") is True
    sbx.files.remove("/home/user/corpus/a.txt")
    assert sbx.files.exists("/home/user/corpus/a.txt") is False
finally:
    sbx.kill()
print("files ok")

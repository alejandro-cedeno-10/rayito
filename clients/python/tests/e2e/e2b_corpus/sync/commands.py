# Sigue https://docs.e2b.dev/commands y https://docs.e2b.dev/commands/background.
from rayito.e2b import Sandbox

sbx = Sandbox.create()
try:
    result = sbx.commands.run("echo hi")
    assert result.stdout == "hi\n", result
    assert result.exit_code == 0

    command = sbx.commands.run("sleep 1; echo done", True)
    running = [process.pid for process in sbx.commands.list()]
    assert command.pid in running, running

    lines: list[str] = []
    finished = command.wait(on_stdout=lines.append)
    assert finished.exit_code == 0, finished
    assert "".join(lines) == "done\n", lines
finally:
    sbx.kill()
print("commands ok")

# Sigue https://docs.e2b.dev/sandbox/pty ("Interactive terminal (PTY)").
from rayito.e2b import PtySize, Sandbox

MARKER = b"hola-2"

sbx = Sandbox.create()
try:
    terminal = sbx.pty.create(PtySize(24, 80))
    pid = terminal.pid
    terminal.disconnect()

    reattached = sbx.pty.connect(pid, timeout=30)
    sbx.pty.send_stdin(pid, b"echo hola-$((1+1))\n")
    output = b""
    for _, _, data in reattached:
        if data is not None:
            output += data
        if MARKER in output:
            break
    assert MARKER in output, output
    assert b"hola" in output
    assert sbx.pty.kill(pid) is True
finally:
    sbx.kill()
print("pty ok")

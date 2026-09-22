"""IPython configuration of every rayito kernel (loaded through ``--config``).

Plain-text tracebacks, unbounded ``repr`` of sequences, no history database in
the snapshot, C-level output captured into ``stream`` messages, and the four
startup scripts of ``startup/`` run in order inside the user namespace.
"""

import os

c = get_config()  # noqa: F821 - injected by traitlets

_STARTUP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "startup")

c.InteractiveShell.colors = "NoColor"
c.PlainTextFormatter.max_seq_length = 0
c.HistoryManager.enabled = False
c.IPKernelApp.capture_fd_output = True
c.InteractiveShellApp.exec_files = [
    os.path.join(_STARTUP_DIR, name)
    for name in ("0001_charts.py", "0002_data.py", "0003_images.py", "0004_warmup.py")
]

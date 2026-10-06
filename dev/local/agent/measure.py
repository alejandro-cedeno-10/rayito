"""Corre una orden dentro del sandbox y mide lo que el spike necesita, sólo
con la biblioteca estándar (lo lanza el `python3` del sistema).

    python3 measure.py <salida> <marca> -- <orden> [args...]

- `seconds`: tiempo de pared de la orden.
- `first_marker_seconds`: cuándo apareció en stdout la primera línea que
  contiene `<marca>` (con OpenCode y `--format json`, el primer evento de
  texto del modelo), o `null`.
- `peak_rss_kib`: el `VmHWM` de `/proc/<pid>/status` del proceso, leído por
  muestreo hasta que sale (el pico de memoria residente que llegó a tener).

stdout completo queda en `<salida>`, que el driver sólo inspecciona para
buscar marcas; nunca lo copia a un informe.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Final

SAMPLE_INTERVAL_SECONDS: Final = 0.05
SEPARATOR: Final = "--"


def read_hwm_kib(pid: int) -> int | None:
    try:
        status = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in status.splitlines():
        if line.startswith("VmHWM:"):
            return int(line.split()[1])
    return None


def main() -> int:
    out_path = Path(sys.argv[1])
    marker = sys.argv[2]
    command = sys.argv[sys.argv.index(SEPARATOR) + 1 :]
    started = time.monotonic()
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, text=True)
    peak = 0
    first_marker: float | None = None

    def sample() -> None:
        nonlocal peak
        while proc.poll() is None:
            peak = max(peak, read_hwm_kib(proc.pid) or 0)
            time.sleep(SAMPLE_INTERVAL_SECONDS)

    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    assert proc.stdout is not None
    with out_path.open("w", encoding="utf-8") as sink:
        for line in proc.stdout:
            if first_marker is None and marker in line:
                first_marker = time.monotonic() - started
            sink.write(line)
    code = proc.wait()
    sampler.join()
    print(
        json.dumps(
            {
                "exit_code": code,
                "seconds": round(time.monotonic() - started, 3),
                "first_marker_seconds": None
                if first_marker is None
                else round(first_marker, 3),
                "peak_rss_kib": peak,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

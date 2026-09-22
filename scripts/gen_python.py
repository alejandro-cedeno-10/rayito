"""Regenera el cliente Python (`clients/python/src/rayito/v1`) desde `proto/`.

Usa `grpc_tools.protoc` con la misma versión del plugin gRPC que fija
`buf.gen.yaml`, para que el resultado sea idéntico al de `buf generate` en
máquinas sin `buf`. Se ejecuta desde la raíz del repo:

    python scripts/gen_python.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

GRPCIO_TOOLS_VERSION = "1.84.0"
REPO_ROOT = Path(__file__).resolve().parent.parent
PROTO_ROOT = REPO_ROOT / "proto"
PYTHON_OUT = REPO_ROOT / "clients" / "python" / "src"


def proto_files() -> list[Path]:
    return sorted((PROTO_ROOT / "rayito" / "v1").glob("*.proto"))


def protoc_command(files: list[Path]) -> list[str]:
    return [
        "uv",
        "run",
        "--no-project",
        "--with",
        f"grpcio-tools=={GRPCIO_TOOLS_VERSION}",
        "python",
        "-m",
        "grpc_tools.protoc",
        f"-I{PROTO_ROOT}",
        f"--python_out={PYTHON_OUT}",
        f"--grpc_python_out={PYTHON_OUT}",
        f"--pyi_out={PYTHON_OUT}",
        *(str(path) for path in files),
    ]


def main() -> int:
    files = proto_files()
    if not files:
        print(f"No hay ficheros .proto en {PROTO_ROOT}", file=sys.stderr)
        return 1
    completed = subprocess.run(protoc_command(files), cwd=REPO_ROOT, check=False)
    if completed.returncode == 0:
        print(f"Generados {len(files)} .proto en {PYTHON_OUT / 'rayito' / 'v1'}")
    return completed.returncode


if __name__ == "__main__":
    sys.exit(main())

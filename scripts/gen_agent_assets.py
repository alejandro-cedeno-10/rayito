"""Render the TypeScript copy of the deepagents runner (single source).

The runner that runs inside the sandbox lives in
``clients/python/src/rayito/_agent/_runner/deepagents_runner.py`` and ships as
Python package data. The TypeScript SDK needs the same bytes to bake it into
an agent template, so this script renders them, with their sha256, into
``clients/typescript/src/agent/assets/deepagents-runner.ts``. Standard library
only.

Usage::

    python scripts/gen_agent_assets.py           # rewrite the TypeScript asset
    python scripts/gen_agent_assets.py --check   # exit 1 with a diff on drift
"""

from __future__ import annotations

import difflib
import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RUNNER_SOURCE = (
    REPO_ROOT
    / "clients"
    / "python"
    / "src"
    / "rayito"
    / "_agent"
    / "_runner"
    / "deepagents_runner.py"
)
TYPESCRIPT_TARGET = (
    REPO_ROOT
    / "clients"
    / "typescript"
    / "src"
    / "agent"
    / "assets"
    / "deepagents-runner.ts"
)


def render_typescript(source: bytes) -> str:
    text = source.decode("utf-8")
    digest = hashlib.sha256(source).hexdigest()
    relative = RUNNER_SOURCE.relative_to(REPO_ROOT).as_posix()
    return (
        f"// Generado por scripts/gen_agent_assets.py desde {relative}; no editar a mano.\n"
        "\n"
        "/** Código fuente del runner de deepagents que la plantilla de agente instala\n"
        " * en `DEEPAGENTS_RUNNER_PATH` (`ai-agent-deepagents`). */\n"
        f"export const DEEPAGENTS_RUNNER_SOURCE = {json.dumps(text, ensure_ascii=False)};\n"
        "\n"
        "/** sha256 de `DEEPAGENTS_RUNNER_SOURCE` en UTF-8 (manifiesto de la plantilla). */\n"
        f'export const DEEPAGENTS_RUNNER_SHA256 = "{digest}";\n'
    )


def main(argv: list[str]) -> int:
    rendered = render_typescript(RUNNER_SOURCE.read_bytes())
    relative = TYPESCRIPT_TARGET.relative_to(REPO_ROOT).as_posix()
    if "--check" in argv:
        current = (
            TYPESCRIPT_TARGET.read_text(encoding="utf-8")
            if TYPESCRIPT_TARGET.exists()
            else ""
        )
        if current == rendered:
            return 0
        diff = difflib.unified_diff(
            current.splitlines(keepends=True),
            rendered.splitlines(keepends=True),
            fromfile=relative,
            tofile=f"{relative} (rendered)",
        )
        sys.stderr.writelines(diff)
        sys.stderr.write(
            "gen_agent_assets: drift; run python scripts/gen_agent_assets.py\n"
        )
        return 1
    TYPESCRIPT_TARGET.parent.mkdir(parents=True, exist_ok=True)
    TYPESCRIPT_TARGET.write_bytes(rendered.encode("utf-8"))
    sys.stdout.write(f"gen_agent_assets: wrote {relative}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

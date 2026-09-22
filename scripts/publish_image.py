"""Shim of ``rayito image publish`` (M7, ``m7-cli``).

The publish pipeline (content-addressed upload, ``create``/``update-microvm-
image`` with the numeric ``baseImageVersion`` reuse rule, the three-state gate
and the build-log tail on failure) lives in ``rayito.cli._publish``; this file
only forwards ``argv`` to the typer application so ``make image-publish*``
and ``rayito image publish`` share one parser and cannot drift. Every option
is documented by ``rayito image publish --help``; ``--artifact``,
``--base-image-version`` and ``--bucket`` (or ``RAYITO_BUCKET``) are
required.

It needs the Python client environment (``typer`` and the SDK)::

    uv run --project clients/python python scripts/publish_image.py \
        --artifact image/rayito-image.zip --base-image-version 1 --bucket <bucket>

``make image-publish`` (and ``-slim``, ``-poly``, ``-caps``) runs exactly that
with ``BUCKET`` and ``BASE_IMAGE_VERSION``; ``AWS_PROFILE`` and ``AWS_REGION``
come from the environment. Without the client environment it exits 2 with the
command to use.
"""

from __future__ import annotations

import sys

SCRIPT = "publish_image.py"
HINT = f"ejecuta con: uv run --project clients/python python scripts/{SCRIPT} …"


def main(argv: list[str]) -> int:
    try:
        from rayito.cli.app import run_shim
    except ModuleNotFoundError as exc:
        print(f"{SCRIPT}: falta {exc.name}; {HINT}", file=sys.stderr)
        return 2
    return run_shim(["image", "publish", *argv])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

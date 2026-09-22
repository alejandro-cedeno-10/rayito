"""Shim of ``rayito image prune`` (M7, ``m7-cli``).

The prune logic (keep set, serialised deletes, 5/10/20/40/80 s backoff on
``ConflictException``, deactivate-then-delete of a refused ``ACTIVE``
version) lives in ``rayito.cli._prune``; this file forwards ``argv`` to the
typer application so ``make image-prune`` and ``rayito image prune`` share
one parser. Options: ``--image-name`` (default ``rayito-base``), ``--keep``
(5), ``--dry-run``, ``--wait-timeout`` (600). Exit code 1 when a candidate is
still present after its attempts.

It needs the Python client environment::

    uv run --project clients/python python scripts/image_prune.py \
        --image-name rayito-base --keep 5 --dry-run

``make image-prune`` runs exactly that with ``PRUNE_ARGS``. Without the
client environment it exits 2 with the command to use.
"""

from __future__ import annotations

import sys

SCRIPT = "image_prune.py"
HINT = f"ejecuta con: uv run --project clients/python python scripts/{SCRIPT} …"


def main(argv: list[str]) -> int:
    try:
        from rayito.cli.app import run_shim
    except ModuleNotFoundError as exc:
        print(f"{SCRIPT}: falta {exc.name}; {HINT}", file=sys.stderr)
        return 2
    return run_shim(["image", "prune", *argv])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

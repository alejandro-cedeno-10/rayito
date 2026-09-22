"""Shared fixtures: the package under ``src`` is importable without an
install, and the test loop is a plain asyncio loop."""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

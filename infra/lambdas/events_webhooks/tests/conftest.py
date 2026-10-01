"""Puts `infra/lambdas/events_webhooks/` on `sys.path` so its modules
(`domain`, `ports`, `adapters`, `handlers`) import the same way they do
inside the Lambda runtime (where they are the zip's own root), without a
package install step.
"""

from __future__ import annotations

import sys
from pathlib import Path

LAMBDA_ROOT = Path(__file__).resolve().parent.parent
if str(LAMBDA_ROOT) not in sys.path:
    sys.path.insert(0, str(LAMBDA_ROOT))

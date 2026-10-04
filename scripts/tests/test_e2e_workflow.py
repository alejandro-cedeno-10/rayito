"""``.github/workflows/e2e.yml`` parsed as data: every job that runs in the
``e2e`` environment (whose OIDC ``sub`` the CI role trusts, with no branch
component) is skipped unless ``vars.RAYITO_E2E_ROLE_ARN`` is set. Without
the role the nightly cannot assume anything and only fails; the gate keeps
it quiet until the maintainer deploys the role and limits the environment
to ``main`` with reviewers (``infra/README.md``)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "e2e.yml"
E2E_ENVIRONMENT = "e2e"
ROLE_GATE = "vars.RAYITO_E2E_ROLE_ARN != ''"


def jobs() -> dict[str, dict[str, Any]]:
    loaded: dict[str, dict[str, Any]] = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))[
        "jobs"
    ]
    return loaded


def test_e2e_jobs_are_skipped_until_the_role_is_configured() -> None:
    gated = {name: job for name, job in jobs().items() if job.get("environment") == E2E_ENVIRONMENT}

    assert gated
    for name, job in gated.items():
        assert ROLE_GATE in str(job.get("if", "")), name

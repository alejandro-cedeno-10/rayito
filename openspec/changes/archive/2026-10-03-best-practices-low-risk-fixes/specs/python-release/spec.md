## ADDED Requirements

### Requirement: The unit suite runs on every declared Python minor
CI SHALL run `uv run pytest tests/unit` in `clients/python` on every Python minor version the classifiers declare: the `check` job on the runner's interpreter (3.12) and a `python-versions` job, with the same hardening as every other job (harden-runner first, actions pinned to a commit SHA, `persist-credentials: false`), on 3.11 and 3.13 through `astral-sh/setup-uv`'s `python-version`. `CONTRIBUTING.md` SHALL give the local equivalent (`uv run --isolated --python <minor> pytest tests/unit`).

#### Scenario: a 3.12-only construct fails CI
- **WHEN** a change uses a standard-library API that exists only from Python 3.12
- **THEN** the `python-versions (3.11)` job fails even though `check` passes
